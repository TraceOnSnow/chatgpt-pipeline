"""Read-only incremental plan: verified commit versus an already-processed baseline.

No model call, no Vault write and no workspace write. Every count comes from
verified source bytes; the cost figures reuse the reservation formulas of the
existing screening (bulk) and note (notes/note_content) stages.
"""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3

from . import bulk, note_content, notes
from .index import now
from .normalize import VERSION, normalize
from .verify import encoded, loads, require, sha, verify

SCHEMA = "chatgpt-incremental-plan-v2"
VOICE_PARTS = ("audio_transcription", "audio_asset_pointer", "real_time_user_audio_video_asset_pointer")
# Largest screening candidate the validator accepts; 3-byte characters approximate CJK text.
WIDE = "中"


def _utc(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _message_key(message):
    return (message["id"], message["role"], sha(message["text"].encode()))


def _digest_summary(package):
    """Baseline retention: message identities and text fingerprints only."""
    normalized = normalize(package)
    return {"keys": [_message_key(m) for m in normalized["analysis_messages"]],
            "all_ids": [m["id"] for m in normalized["messages"]],
            "input_fingerprint": normalized["input_fingerprint"]}


def latest_commit(account_root):
    """The single published commit whose catalog equals the live catalog pointer."""
    root = Path(account_root).absolute()
    pointer = sha((root / "catalog.json").read_bytes())
    matches = [p for p in sorted((root / "commits").glob("*.commit.json"))
               if loads(p.read_bytes()).get("catalog_sha256") == pointer]
    require(len(matches) == 1, "Need exactly one published commit matching catalog.json; pass --commit")
    return matches[0]


def commits_for_catalog(account_root, catalog_sha256):
    root = Path(account_root).absolute()
    return [p for p in sorted((root / "commits").glob("*.commit.json"))
            if loads(p.read_bytes()).get("catalog_sha256") == catalog_sha256]


def _read_workspace(workspace):
    """Read-only connection (mode=ro) inside one read transaction, so WAL content is included and the
    three tables come from one consistent snapshot; refuses a hot rollback journal."""
    workspace = Path(workspace).absolute()
    database = workspace / "state.sqlite3"
    require(database.is_file() and not database.is_symlink(), "Workspace database missing")
    require(not (workspace / "state.sqlite3-journal").exists(),
            "Workspace database has a rollback journal (write in progress); run again after it finishes")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as db:
        db.execute("BEGIN")
        imports = [loads(row[0]) for row in db.execute("SELECT origin FROM imports ORDER BY created_at")]
        items = db.execute("""SELECT i.id, i.candidate, i.decision, j.state FROM items i
                              LEFT JOIN jobs j ON j.id = i.job_id""").fetchall()
        calls = db.execute("SELECT cost, reservation FROM calls").fetchall()
        db.execute("ROLLBACK")
    config = loads((workspace / "config.json").read_bytes())
    actual = sum((Decimal(c[0]) for c in calls if c[0] is not None), Decimal(0))
    reserved = sum((Decimal(c[1]) for c in calls if c[0] is None), Decimal(0))
    budget = Decimal(str(config.get("budget_usd", notes.BUDGET)))
    ledger = {"calls": len(calls), "actual_usd": str(actual), "unknown_cost_reserved_usd": str(reserved),
              "unknown_cost_calls": sum(c[0] is None for c in calls), "budget_usd": str(budget),
              "remaining_usd": str(budget - actual - reserved)}
    by_conversation = {}
    for item_id, candidate, decision, state in items:
        candidate = loads(candidate)
        key = (candidate["source"]["account_key"], candidate["source"]["conversation_id"])
        by_conversation.setdefault(key, []).append({"item_id": item_id, "decision": decision, "job_state": state})
    receipts = set()
    imports_dir = workspace / "vault-imports"
    if imports_dir.is_dir():
        for vault_dir in imports_dir.iterdir():
            if vault_dir.is_dir():
                receipts.update(p.stem for p in vault_dir.glob("*.json"))
    return {"imports": imports, "items": by_conversation, "receipts": receipts, "ledger": ledger}


def _new_record():
    return {"keys": set(), "ids": set(), "all_ids": set(), "snapshots": 0}


def registered_baselines(workspace_state, account_key):
    """Primary messages actually queued by the sealed selection batches registered in the workspace.

    Returns (queued keys per conversation, snapshots referenced, batch descriptions). Only the
    ``primary=true`` refs of each queued screen job's input count as processed; messages before a
    date bound, context-only messages and conversations a batch skipped are not processed.
    """
    queued, found, batches = {}, {}, []
    for origin in workspace_state["imports"]:
        batch = Path(origin["batch"])
        manifest = bulk.read(batch / "manifest.json")
        require(bulk.read(batch / "manifest-seal.json")["sha256"] == bulk.digest(manifest)
                == origin.get("manifest_sha256", bulk.digest(manifest)),
                "Registered batch manifest changed: " + str(batch))
        snapshots = [s for s in manifest["snapshots"] if s.get("account_key") == account_key]
        progress = batch / "progress.json"
        cost = bulk.read(progress).get("actual_cost_usd") if progress.exists() else None
        store = Path(manifest["store"]) if manifest.get("store") else None
        entries = [e for e in manifest["conversations"] if e["source"].get("account_key") == account_key]
        jobs = refs = 0
        for entry in entries:
            keys = queued.setdefault(entry["source"]["conversation_id"], set())
            for job in entry["jobs"]:
                require(store is not None, "Registered batch has jobs but no store: " + str(batch))
                data = bulk.read(store / job / "input.json")
                definition = bulk.read(store / job / "definition.json")
                require(definition["input_sha256"] == bulk.digest(data)
                        and data["source"]["conversation_id"] == entry["source"]["conversation_id"],
                        "Queued job input changed: " + job)
                jobs += 1
                for ref in data["messages"]:
                    if ref["primary"] is True:
                        keys.add((ref["source_message_id"], ref["role"], ref["text_sha256"]))
                        refs += 1
        scope = manifest.get("scope") or {}
        batches.append({"batch": str(batch), "manifest_sha256": bulk.digest(manifest),
                        "account_snapshots": len(snapshots), "screening_actual_cost_usd": cost,
                        "scope_type": scope.get("type", "full_snapshot"),
                        "scope_start_inclusive_utc": scope.get("start_inclusive_utc"),
                        "account_conversations_queued": len(entries), "account_jobs": jobs,
                        "account_primary_refs": refs})
        for snapshot in snapshots:
            found.setdefault(snapshot["commit_sha256"], {**snapshot, "batches": []})["batches"].append(str(batch))
    return queued, list(found.values()), batches


def _verify_baseline_snapshot(root, source):
    verified = verify(root, Path(source["commit_path"]), retain=_digest_summary)
    require(verified["catalog_sha256"] == source["catalog_sha256"], "Baseline commit/catalog mismatch")
    return verified


def load_baselines(account_root, *, workspace_state=None, baseline_catalogs=()):
    """Order-free processed baseline.

    Workspace mode: the union of primary messages queued by registered batches; the referenced
    snapshots only tell which message ids already existed (for unreadable-message counting).
    Explicit mode (--baseline-catalog): every message of every given catalog is treated as processed.
    """
    root = Path(account_root).absolute()
    conversations, described, batches = {}, [], []
    if baseline_catalogs:
        for path in baseline_catalogs:
            data = Path(path).read_bytes()
            catalog = loads(data)
            require(catalog.get("schema_version") == 1 and catalog.get("account_key") == root.name,
                    "Baseline catalog belongs to another account or schema")
            commits = commits_for_catalog(root, sha(data))
            require(commits, "Baseline catalog bytes match no published commit; cannot verify its bodies")
            source = {"origin": "explicit_catalog", "treated_as_processed": "whole_catalog",
                      "path": str(Path(path).absolute()), "catalog_sha256": sha(data), "commit_path": str(commits[0])}
            verified = _verify_baseline_snapshot(root, source)
            entries = verified["catalog"]["conversations"]
            for entry in entries:
                record = conversations.setdefault(entry["id"], _new_record())
                record["snapshots"] += 1
                if entry.get("object"):
                    package = verified["packages"][entry["object"]]["package"]
                    record["keys"].update(package["keys"])
                    record["ids"].update(k[0] for k in package["keys"])
                    record["all_ids"].update(package["all_ids"])
            described.append({**{k: v for k, v in source.items() if k != "commit_path"},
                              "commit_sha256": verified["commit_sha256"], "conversations": len(entries),
                              "listing_complete": verified["catalog"]["listing_complete"],
                              "bodies_complete": verified["catalog"]["bodies_complete"]})
            del verified
    elif workspace_state is not None:
        queued, snapshots, batches = registered_baselines(workspace_state, root.name)
        require(any(queued.values()), "No processed baseline could be derived; pass --baseline-catalog explicitly")
        for conversation_id, keys in queued.items():
            record = conversations.setdefault(conversation_id, _new_record())
            record["keys"].update(keys)
            record["ids"].update(k[0] for k in keys)
        for snapshot in snapshots:
            source = {"origin": "workspace_registered_batch", "treated_as_processed": "queued_primary_messages",
                      "batches": snapshot["batches"], "catalog_sha256": snapshot["catalog_sha256"],
                      "commit_path": str(root / "commits" / (snapshot["commit_sha256"] + ".commit.json"))}
            verified = _verify_baseline_snapshot(root, source)
            entries = verified["catalog"]["conversations"]
            for entry in entries:
                if entry.get("object") and entry["id"] in conversations:
                    record = conversations[entry["id"]]
                    record["snapshots"] += 1
                    record["all_ids"].update(verified["packages"][entry["object"]]["package"]["all_ids"])
            described.append({**{k: v for k, v in source.items() if k != "commit_path"},
                              "commit_sha256": verified["commit_sha256"], "conversations": len(entries),
                              "conversations_with_queued_messages": sum(bool(queued.get(e["id"])) for e in entries),
                              "listing_complete": verified["catalog"]["listing_complete"],
                              "bodies_complete": verified["catalog"]["bodies_complete"]})
            del verified
        for record in conversations.values():
            record["all_ids"] |= record["ids"]
    require(conversations and any(r["keys"] for r in conversations.values()),
            "No processed baseline could be derived; pass --baseline-catalog explicitly")
    return conversations, described, batches


EVIDENCE_HASH_FIELDS = ("text_sha256", "source_text_sha256")


def _library_index(vault):
    """Conversation, item and exact message-evidence references in the Library source registry (read-only).

    Evidence is a dict carrying a message id (source_message_id or message_id), role and text
    sha256, scoped to the nearest conversation_id; ``input_context`` lists and refs marked
    ``primary: false`` are context, never coverage.
    """
    state = loads((Path(vault) / ".markdown-library" / "state.json").read_bytes())
    by_conversation, by_item, evidence = {}, {}, {}

    def walk(value, conversations, messages, scope, keys):
        if isinstance(value, dict):
            if isinstance(value.get("conversation_id"), str):
                scope = (value.get("account_key", scope[0]), value["conversation_id"])
                conversations.add(scope)
            elif isinstance(value.get("account_key"), str):
                scope = (value["account_key"], scope[1])
            identity = next((value[f] for f in ("source_message_id", "message_id") if isinstance(value.get(f), str)), None)
            if identity is not None:
                messages.add(identity)
                digest = next((value[f] for f in EVIDENCE_HASH_FIELDS if isinstance(value.get(f), str)), None)
                if (digest and isinstance(value.get("role"), str) and scope[1] is not None
                        and value.get("primary", True) is not False):
                    keys.add((scope, (identity, value["role"], digest)))
            for field, child in value.items():
                if field != "input_context":
                    walk(child, conversations, messages, scope, keys)
        elif isinstance(value, list):
            for child in value:
                walk(child, conversations, messages, scope, keys)

    for source_id, record in state.get("sources", {}).items():
        kind = source_id.split(":", 1)[0]
        source = record.get("source") if isinstance(record, dict) else None
        conversations, messages, keys = set(), set(), set()
        walk(source, conversations, messages, (None, None), keys)
        if isinstance(source, dict) and isinstance(source.get("item_id"), str):
            by_item.setdefault(source["item_id"], []).append(kind)
        for account_key, conversation_id in conversations:
            slot = by_conversation.setdefault(conversation_id, [])
            slot.append({"type": kind, "account_key": account_key, "message_ids": messages,
                         "evidence_keys": len(keys)})
        for (account_key, conversation_id), key in keys:
            evidence.setdefault(conversation_id, {}).setdefault(key, set()).add((account_key, kind))
    return by_conversation, by_item, evidence


def _voice(raw_message, normalized_message):
    metadata = raw_message.get("metadata") if isinstance(raw_message, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    parts = {a.get("content_type") for a in normalized_message["attachments"] if isinstance(a, dict)}
    voice_mode = metadata.get("voice_mode_message") is True or bool(parts & set(VOICE_PARTS))
    return voice_mode, metadata.get("dictation") is True, bool(parts & {"audio_asset_pointer",
                                                                         "real_time_user_audio_video_asset_pointer"})


def _max_screen_candidate():
    item = {"title": WIDE * 200, "summary": WIDE * 1800,
            "user_message_ids": ["m999"] * 12, "related_message_ids": ["m999"] * 12}
    return {"disposition": "keep", "reason": WIDE * 1600, "items": [item] * 6}


def _screen_estimate(eligible, primary, profile):
    """Chunk the unprocessed messages exactly as the 11-07 incremental intake did."""
    positions = {m["id"]: i for i, m in enumerate(eligible)}
    candidate = _max_screen_candidate()
    jobs = []
    for _context, part in bulk.chunk_messages(primary):
        prior = eligible[:positions[part[0]["id"]]]
        context = []
        old_user = next((m for m in reversed(prior) if m["role"] == "user"), None)
        if old_user:
            context.append(old_user)
        if prior and (not context or context[-1]["id"] != prior[-1]["id"]):
            context.append(prior[-1])
        _, screen = bulk.build_request(context, part, "screen", profile=profile)
        try:
            _, review = bulk.build_request(context, part, "review", candidate, profile=profile)
            review_bytes, review_cap = len(encoded(review)), bulk.reservation_usd(review)
        except ValueError:
            # A maximal candidate would exceed the request bound; bound it by the request limit.
            review_bytes = bulk.MAX_REQUEST_CHARS * 3
            review_cap = _bytes_reservation(review_bytes + 2048, bulk.MAX_OUTPUT, Decimal("0.01"))
        jobs.append({"primary_messages": len(part), "context_messages": len(context),
                     "primary_characters": sum(len(m["text"]) for m in part),
                     "screen_request_bytes": len(encoded(screen)), "screen_reservation_usd": bulk.reservation_usd(screen),
                     "review_request_bytes_max": review_bytes, "review_reservation_usd_max": review_cap})
    return jobs


def _note_target_cap(eligible):
    """Two calls (draft, review) for one target drawn from this conversation."""
    whole = sum(len(m["text"]) for m in eligible) <= notes.MAX_SOURCE
    if whole:
        messages = [{"id": f"m{i:03d}", "role": m["role"], "text": m["text"]} for i, m in enumerate(eligible, 1)]
    else:
        messages = [{"id": "m001", "role": "user", "text": WIDE * notes.MAX_SOURCE}]
    material = {"scope": "planning", "candidate": {"title": WIDE * 200, "summary": WIDE * 1800},
                "personal_evidence_ids": ["m001"], "messages": messages,
                "source_policy_revision": bulk.SOURCE_POLICY_REVISION}
    bound = {"max_tokens": note_content.MAX_OUTPUT, "_bytes": notes.MAX_REQUEST * 3}
    caps = {}
    for stage, draft in (("draft", None), ("review", WIDE * note_content.MAX_MARKDOWN)):
        try:
            payload = note_content.payload(stage, material, draft)
            caps[stage] = {"request_bytes": len(encoded(payload)), "reservation_usd": notes.call_reservation(payload),
                           "at_request_bound": False}
        except ValueError:
            caps[stage] = {"request_bytes": bound["_bytes"], "at_request_bound": True,
                           "reservation_usd": _bytes_reservation(bound["_bytes"], bound["max_tokens"])}
    return {"whole_conversation_fits_source_bound": whole, "stages": caps,
            "per_target_usd": caps["draft"]["reservation_usd"] + caps["review"]["reservation_usd"]}


def _bytes_reservation(size, max_tokens, floor=Decimal(0)):
    """Same formula as bulk/notes reservations, for a request known only by its size bound."""
    cap = bulk.PROVIDER["max_price"]
    value = (Decimal(size) * Decimal(str(cap["prompt"])) + max_tokens * Decimal(str(cap["completion"]))) \
        / Decimal(1000000) * Decimal("1.1")
    return max(floor, value)


def plan(account_root, commit=None, *, workspace=None, baseline_catalogs=(), vault=None,
         profile_path=None, since=None):
    root = Path(account_root).absolute()
    commit_path = Path(commit).absolute() if commit else latest_commit(root)
    profile = bulk.load_profile(profile_path) if profile_path else None
    since_ts = datetime.fromisoformat(since).timestamp() if since else None
    require(since is None or datetime.fromisoformat(since).tzinfo is not None, "--since needs an explicit timezone")
    workspace_state = _read_workspace(workspace) if workspace else None
    baseline, baseline_sources, batches = load_baselines(
        root, workspace_state=workspace_state, baseline_catalogs=baseline_catalogs)
    library, library_items, evidence = _library_index(vault) if vault else (None, None, {})
    pointer_before = sha((root / "catalog.json").read_bytes())

    def summarize(package):
        normalized = normalize(package)
        mapping = package["raw"].get("mapping") if isinstance(package["raw"].get("mapping"), dict) else {}
        prior = baseline.get(package["conversation_id"])
        processed = prior["keys"] if prior else set()
        processed_ids = prior["ids"] if prior else set()
        seen_ids = prior["all_ids"] if prior else set()
        eligible = normalized["analysis_messages"]
        unqueued = [m for m in eligible if _message_key(m) not in processed]
        exact = evidence.get(package["conversation_id"], {})
        covered = [m for m in unqueued if m["role"] == "user" and any(
            account in (None, root.name) for account, _kind in exact.get(_message_key(m), ()))]
        covered_ids = {m["id"] for m in covered}
        primary = [m for m in unqueued if m["id"] not in covered_ids]
        primary_ids = {m["id"] for m in primary}
        changed = [m["id"] for m in primary if m["id"] in processed_ids]
        by_id = {m["id"]: m for m in normalized["messages"]}
        stats = Counter()
        created = []
        for message in normalized["messages"]:
            if message["role"] != "user":
                continue
            raw = (mapping.get(message["node_id"]) or {}).get("message") if message["node_id"] else None
            voice, dictation, audio = _voice(raw, message)
            in_primary = message["id"] in primary_ids
            new_unreadable = message["exclusion"] and message["id"] not in seen_ids
            if in_primary:
                stats.update(user_messages=1, user_characters=len(message["text"]),
                             voice_mode_messages=int(voice), dictation_messages=int(dictation),
                             audio_attachment_messages=int(audio))
                if since_ts is not None and isinstance(message["created"], (int, float)) and message["created"] >= since_ts:
                    stats.update(user_messages_since=1, user_characters_since=len(message["text"]))
            elif new_unreadable:
                stats.update(user_messages_without_text=1, audio_without_text=int(audio))
        for identity in primary_ids:
            stamp = by_id[identity]["created"]
            if isinstance(stamp, (int, float)) and _utc(stamp):
                created.append(stamp)
            else:
                stats.update(primary_messages_without_date=1)
        excluded = Counter(m["exclusion"] for m in normalized["messages"]
                           if m["exclusion"] and m["id"] not in seen_ids)
        result = {"state": normalized["state"], "issues": normalized["issues"][:20],
                  "issue_count": len(normalized["issues"]), "input_fingerprint": normalized["input_fingerprint"],
                  "analysis_messages": len(eligible), "baseline_covered_messages": len(eligible) - len(primary),
                  "unprocessed_messages": len(primary), "changed_existing_message_ids": len(changed),
                  "covered_by_library": len(covered),
                  "covered_by_library_types": dict(Counter(kind for m in covered
                                                           for kind in sorted({k for _a, k in exact[_message_key(m)]}))),
                  "removed_from_current_branch": len(processed_ids - {m["id"] for m in eligible}),
                  "unprocessed_characters": sum(len(m["text"]) for m in primary),
                  "unprocessed_date_range": [_utc(min(created)), _utc(max(created))] if created else [None, None],
                  "new_excluded_messages": dict(excluded),
                  "new_attachment_references": sum(len(by_id[i]["attachments"]) for i in primary_ids),
                  "stats": dict(stats), "primary_ids": [m["id"] for m in primary]}
        if primary and normalized["state"] == "ready":
            try:
                result["screen_jobs"] = _screen_estimate(eligible, primary, profile)
            except ValueError as error:
                result["estimate_gap"] = str(error)
            result["note_target"] = _note_target_cap(eligible)
        return result

    verified = verify(root, commit_path, retain=summarize)
    rows, totals, gaps = [], Counter(), []
    money = {k: Decimal(0) for k in ("screen", "screen_review", "note_per_target_max", "note_all_targets_max")}
    for entry in verified["catalog"]["conversations"]:
        prior = baseline.get(entry["id"])
        if not entry.get("object"):
            gaps.append({"conversation_id": entry["id"], "body_status": entry["body_status"],
                         "reason": "baseline_conversation_without_current_body" if prior else "no_verified_body"})
            continue
        summary = verified["packages"][entry["object"]]["package"]
        totals.update(covered_by_library=summary["covered_by_library"])
        if not summary["unprocessed_messages"] and not summary["new_excluded_messages"]:
            totals.update({"covered_by_library_only_conversations" if summary["covered_by_library"]
                           else "unchanged_conversations": 1})
            continue
        # A conversation counts as continued only when some of its messages were actually processed.
        change = ("new_conversation" if not (prior and prior["keys"]) else "continuation"
                  if summary["unprocessed_messages"] else "new_unreadable_only")
        primary_ids = set(summary.pop("primary_ids"))
        row = {"account_key": root.name, "conversation_id": entry["id"], "title": entry.get("title", ""),
               "change": change, "created": _utc(entry.get("created")), "updated": _utc(entry.get("updated")),
               "body_status": entry["body_status"], "object_path": entry["object"],
               "byte_sha256": verified["packages"][entry["object"]]["byte_sha256"], **summary}
        if entry["body_status"] != "current":
            row["body_gap"] = "Body is not current; refresh before any processing"
        if workspace_state is not None:
            items = workspace_state["items"].get((root.name, entry["id"]), [])
            row["workbench"] = {"items": len(items), "decisions": dict(Counter(i["decision"] for i in items)),
                                "vault_import_receipts": sum(i["item_id"] in workspace_state["receipts"] for i in items)}
        if library is not None:
            refs = [r for r in library.get(entry["id"], []) if r["account_key"] in (None, root.name)]
            items = workspace_state["items"].get((root.name, entry["id"]), []) if workspace_state else []
            item_refs = [kind for i in items for kind in library_items.get(i["item_id"], [])]
            referenced = set().union(*(r["message_ids"] for r in refs)) if refs else set()
            row["library"] = {"conversation_sources": len(refs), "item_sources": len(item_refs),
                              "source_types": dict(Counter([r["type"] for r in refs] + item_refs)),
                              "covered_by_library": row["covered_by_library"],
                              "unprocessed_messages_already_referenced": len(primary_ids & referenced)}
        jobs = row.get("screen_jobs", [])
        screen = sum((j["screen_reservation_usd"] for j in jobs), Decimal(0))
        review = sum((j["review_reservation_usd_max"] for j in jobs), Decimal(0))
        per_target = row["note_target"]["per_target_usd"] if "note_target" in row else Decimal(0)
        max_targets = 6 * len(jobs)
        row["estimate"] = {"screen_calls": len(jobs), "screen_review_calls_max": len(jobs),
                           "screen_usd_cap": str(screen), "screen_review_usd_cap": str(review),
                           "note_targets_max_by_validator": max_targets, "note_calls_per_target": 2,
                           "note_usd_per_target_cap": str(per_target),
                           "note_usd_cap_if_every_item_became_a_target": str(per_target * max_targets)}
        for job in jobs:
            for key in ("screen_reservation_usd", "review_reservation_usd_max"):
                job[key] = str(job[key])
        if "note_target" in row:
            for stage in row["note_target"]["stages"].values():
                stage["reservation_usd"] = str(stage["reservation_usd"])
            row["note_target"]["per_target_usd"] = str(per_target)
        if "estimate_gap" in row or row["state"] != "ready":
            gaps.append({"conversation_id": entry["id"], "reason": row.get("estimate_gap", row["state"])})
        money["screen"] += screen
        money["screen_review"] += review
        money["note_per_target_max"] = max(money["note_per_target_max"], per_target)
        money["note_all_targets_max"] += per_target * max_targets
        stats = row["stats"]
        totals.update({{"new_conversation": "new_conversations", "continuation": "continuations",
                        "new_unreadable_only": "unreadable_only_changes"}[change]: 1,
                       "unprocessed_messages": row["unprocessed_messages"],
                       "unprocessed_user_messages": stats.get("user_messages", 0),
                       "unprocessed_user_characters": stats.get("user_characters", 0),
                       "voice_mode_user_messages": stats.get("voice_mode_messages", 0),
                       "dictation_user_messages": stats.get("dictation_messages", 0),
                       "screen_calls": len(jobs), "screen_review_calls_max": len(jobs),
                       "note_targets_max_by_validator": max_targets})
        if since_ts is not None:
            totals.update(user_messages_since=stats.get("user_messages_since", 0),
                          user_characters_since=stats.get("user_characters_since", 0))
        if row.get("workbench", {}).get("items"):
            totals.update(conversations_with_workbench_items=1)
        if row.get("library", {}).get("conversation_sources") or row.get("library", {}).get("item_sources"):
            totals.update(conversations_with_library_sources=1)
        rows.append(row)
    absent = sorted(set(baseline) - {e["id"] for e in verified["catalog"]["conversations"]})
    require(sha((root / "catalog.json").read_bytes()) == pointer_before or commit,
            "Live catalog moved during planning; run again")
    rows.sort(key=lambda r: (r["change"] != "new_conversation", r["updated"] or "", r["conversation_id"]))
    return {
        "schema": SCHEMA, "generated_at": now(), "read_only": True, "model_calls": 0,
        "source": {"account_key": root.name, "commit_sha256": verified["commit_sha256"],
                   "catalog_sha256": verified["catalog_sha256"], "conversations": len(verified["catalog"]["conversations"]),
                   "listing_complete": verified["catalog"]["listing_complete"],
                   "bodies_complete": verified["catalog"]["bodies_complete"],
                   "is_live_catalog": verified["catalog_sha256"] == pointer_before,
                   "normalization_version": VERSION},
        "baseline": {"rule": ("Explicit --baseline-catalog: the whole catalog is treated as processed (every message "
                              "on the current branch of each given snapshot, unioned)." if baseline_catalogs else
                              "A message is processed when its (id, role, text sha256) was queued as a primary "
                              "message by a registered batch job (input.json primary=true); messages before a date "
                              "bound, context-only messages and skipped conversations are not processed."),
                     "treated_as_processed": "whole_catalog" if baseline_catalogs else "queued_primary_messages",
                     "library_coverage_rule": "User messages whose (id, role, text sha256) match Library evidence "
                                              "exactly are removed from primary and counted as covered_by_library; "
                                              "conversation-only references are hints and are not deducted.",
                     "sources": baseline_sources, "registered_batches": batches,
                     "conversations": len(baseline), "absent_from_latest": len(absent)},
        "since": since, "counts": dict(totals),
        "estimate": {
            "screen_calls": totals["screen_calls"], "screen_review_calls_max": totals["screen_review_calls_max"],
            "screen_usd_cap": str(money["screen"]), "screen_review_usd_cap": str(money["screen_review"]),
            "screening_usd_cap_total": str(money["screen"] + money["screen_review"]),
            "note_calls_per_target": 2, "note_targets_max_by_validator": totals["note_targets_max_by_validator"],
            "note_usd_per_target_cap_max": str(money["note_per_target_max"]),
            "note_usd_cap_if_every_item_became_a_target": str(money["note_all_targets_max"]),
            "basis": {
                "screen": "bulk.chunk_messages (whole messages, 24k characters) over unprocessed messages, context = "
                          "last earlier user message + last earlier message; bulk.reservation_usd per request.",
                "screen_review": "At most one review per non-skip screen job; candidate sized at validator limits "
                                 "(6 items, title 200, summary 1800, reason 1600) with 3-byte characters.",
                "notes": "notes.call_reservation per draft/review call; whole conversation if it fits 60k source "
                         "characters, otherwise the 60k bound; review includes a 45k-character draft. The number of "
                         "note targets is decided by screening, review and editorial bundling, so only the validator "
                         "limit of 6 items per screen job bounds it.",
                "not_a_price_quote": "Reservations use the frozen provider max_price cap with the existing 1.1 "
                                     "margin; actual provider cost is normally far lower and is not predicted here."},
            "notes_ledger": workspace_state["ledger"] if workspace_state else None,
            "screening_ledger_prior_actual_usd": str(sum(
                (Decimal(b["screening_actual_cost_usd"]) for b in batches if b["screening_actual_cost_usd"]), Decimal(0))),
        },
        "gaps": gaps, "conversations": rows,
        "limitations": [
            "Read-only; no selection job, workspace row, receipt or Vault file is created.",
            "Voice flags come from raw message metadata (voice_mode_message, dictation) and audio parts; "
            "absence of a flag does not prove typed input.",
            "Library conversation/item references without per-message evidence are only hints; only exact "
            "(id, role, text sha256) evidence of user messages is deducted.",
            "Removed-from-branch counts never imply deletion; nothing is propagated.",
        ]}


BASELINE_LABELS = {"whole_catalog": "整份 catalog 视为已处理 (whole catalog, explicit --baseline-catalog)",
                   "queued_primary_messages": "已登记批次实际排队的 primary 消息 (queued primary messages)"}


def summary_text(result):
    counts, estimate = result["counts"], result["estimate"]
    lines = [f"# Incremental plan ({result['generated_at']})", "",
             f"Commit {result['source']['commit_sha256'][:12]} / catalog {result['source']['catalog_sha256'][:12]}, "
             f"{result['source']['conversations']} conversations; model calls made: 0.",
             f"Baseline: {len(result['baseline']['sources'])} snapshot(s), "
             f"{result['baseline']['conversations']} conversations; treated as processed: "
             f"{BASELINE_LABELS[result['baseline']['treated_as_processed']]}.", "",
             f"- New conversations: {counts.get('new_conversations', 0)}",
             f"- Continuations: {counts.get('continuations', 0)}",
             f"- Only new unreadable messages (media/tool/reasoning): {counts.get('unreadable_only_changes', 0)}",
             f"- Unprocessed user messages: {counts.get('unprocessed_user_messages', 0)} "
             f"({counts.get('unprocessed_user_characters', 0)} characters; voice {counts.get('voice_mode_user_messages', 0)}, "
             f"dictation {counts.get('dictation_user_messages', 0)})",
             f"- Removed as covered by Library evidence: {counts.get('covered_by_library', 0)} user messages",
             f"- Screening calls: {estimate['screen_calls']} + review <= {estimate['screen_review_calls_max']}, "
             f"cap {estimate['screening_usd_cap_total']} USD",
             f"- Notes: 2 calls per target, <= {estimate['note_usd_per_target_cap_max']} USD per target; "
             f"targets <= {estimate['note_targets_max_by_validator']} by validator only", ""]
    if estimate.get("notes_ledger"):
        lines.append(f"Notes ledger remaining: {estimate['notes_ledger']['remaining_usd']} USD "
                     f"of {estimate['notes_ledger']['budget_usd']}.")
        lines.append("")
    lines.append("| change | updated | user msgs | chars | voice | screen jobs | workbench items | library refs | title |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for row in result["conversations"]:
        stats = row["stats"]
        title = row["title"].replace("|", "/")[:60]
        lines.append(f"| {row['change']} | {(row['updated'] or '')[:10]} | {stats.get('user_messages', 0)} | "
                     f"{stats.get('user_characters', 0)} | {stats.get('voice_mode_messages', 0)} | "
                     f"{row['estimate']['screen_calls']} | {row.get('workbench', {}).get('items', '-')} | "
                     f"{row.get('library', {}).get('conversation_sources', '-')} | {title} |")
    if result["gaps"]:
        lines += ["", f"Gaps: {len(result['gaps'])} (see JSON)."]
    return "\n".join(lines) + "\n"


def write_private(path, data):
    path = Path(path).absolute()
    require(not path.exists(), "Output exists; choose a new path, existing files are never replaced")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    return str(path)


def dumps(result):
    return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
