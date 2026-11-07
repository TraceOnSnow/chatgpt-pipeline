"""Offline official ChatGPT ZIP adapter; raw archives and existing indexes stay read-only."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import sqlite3
import zipfile

from . import bulk, notes
from .index import now
from .normalize import normalize, VERSION
from .verify import encoded, loads, mapping_hash, require, sha

ADAPTER = "official-chatgpt-zip-v1"
SEGMENTATION = "official-current-branch-20k-segments-v1"
SEGMENT_CHARS = 20000


def file_sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def package(raw):
    """Build the existing uploader package contract without changing the official raw object."""
    require(isinstance(raw, dict), "Invalid official conversation")
    identity = raw.get("conversation_id", raw.get("id"))
    require(isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identity),
            "Invalid official conversation ID")
    require(raw.get("id", identity) == identity, "Official conversation IDs disagree")
    mapping, node, seen, messages = raw.get("mapping"), raw.get("current_node"), set(), []
    complete = isinstance(mapping, dict) and node is not None
    while node is not None and complete:
        if not isinstance(node, str) or node in seen or node not in mapping:
            complete = False
            break
        seen.add(node)
        item = mapping[node]
        if not isinstance(item, dict) or "parent" not in item:
            complete = False
            break
        message = item.get("message")
        if isinstance(message, dict) and isinstance(message.get("author"), dict):
            role = message["author"].get("role")
            if role in ("user", "assistant"):
                messages.append({"id": message.get("id"), "role": role,
                                 "created": message.get("create_time"), "content": message.get("content"),
                                 "metadata": message.get("metadata")})
        node = item.get("parent")
    messages.reverse()
    return {"schema_version": 1, "source": "chatgpt", "untrusted_data": True,
            "conversation_id": identity, "url": "https://chatgpt.com/c/" + identity,
            "raw": raw, "raw_sha256": sha(encoded(raw)), "content_sha256": mapping_hash(mapping),
            "readable_status": "complete_current_branch" if complete else "incomplete_current_branch",
            "messages": messages}


def analysis_messages(normalized):
    """Keep readable current-branch text, record all structural/media gaps separately."""
    messages, segments = [], []
    for message in normalized["analysis_messages"]:
        text = message["text"]
        if len(text) <= bulk.CHUNK_CHARS:
            messages.append(message)
            continue
        for start in range(0, len(text), SEGMENT_CHARS):
            end = min(len(text), start + SEGMENT_CHARS)
            identity = f"{message['id']}~chars-{start}-{end}"
            messages.append({**message, "id": identity, "text": text[start:end]})
            segments.append({"id": identity, "source_message_id": message["id"], "start": start, "end": end,
                             "text_sha256": sha(text[start:end].encode()), "message_sha256": sha(text.encode())})
    return messages, segments


def text_fingerprint(messages):
    return bulk.digest([{k: m[k] for k in ("role", "text")} for m in messages])


def index_snapshot(path):
    """Read historical analysis inputs; no old DB schema or state writes."""
    if path is None:
        return {}, []
    result, accounts = {}, []
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        accounts = [r[0] for r in db.execute("SELECT account_key FROM accounts ORDER BY account_key")]
        for account, conversation, fingerprint, messages in db.execute(
                "SELECT account_key,conversation_id,input_fingerprint,messages_json FROM analysis_inputs WHERE normalization_version=?",
                (VERSION,)):
            result.setdefault(conversation, []).append({"account_key": account, "input_fingerprint": fingerprint,
                                                        "text_fingerprint": text_fingerprint(loads(messages))})
    return result, accounts


def date(value):
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat() if isinstance(value, (int, float)) else None
    except (ValueError, OSError, OverflowError):
        return None


def prepare(zip_path, output, profile_path, account_key, index=None, skip_existing=True, *,
            source_policy_revision=bulk.SOURCE_POLICY_REVISION):
    bulk.source_policy_prompt("", source_policy_revision)
    output = bulk.runtime_path(output)
    require(not output.exists(), "Memory batch already exists; resume with run/export")
    require(isinstance(account_key, str) and re.fullmatch(r"[A-Za-z0-9_.@-]{1,150}", account_key), "Explicit account key required")
    zip_path = Path(zip_path).resolve()
    zip_hash = file_sha(zip_path)
    profile_source_sha256 = file_sha(profile_path)
    profile = bulk.load_profile(profile_path)
    require(file_sha(profile_path) == profile_source_sha256, "Profile changed during load")
    previous, accounts = index_snapshot(index)
    output.mkdir(parents=True, mode=0o700)
    store = output / "selection-jobs"
    store.mkdir(mode=0o700)
    (output / "packages").mkdir(mode=0o700)
    bulk.save(output / "selection-profile.json", profile)
    entries, gaps, inventory, members, duplicates, matches = [], [], [], [], [], []
    seen, created, months, counts = {}, [], Counter(), Counter()
    with zipfile.ZipFile(zip_path) as archive:
        filenames = archive.namelist()
        require(len(filenames) == len(set(filenames)), "Duplicate ZIP member names")
        names = sorted(n for n in filenames if re.fullmatch(r"(?:.*/)?conversations(?:-\d+)?\.json", n))
        require(bool(names), "No official conversation JSON members")
        for member in names:
            member_bytes = archive.read(member)
            member_hash = sha(member_bytes)
            values = loads(member_bytes)
            require(isinstance(values, list), "Official conversation member must be an array")
            members.append({"member": member, "sha256": member_hash, "bytes": len(member_bytes), "conversations": len(values)})
            for position, raw in enumerate(values):
                obj = package(raw)
                identity, raw_hash = obj["conversation_id"], obj["raw_sha256"]
                provenance = {"zip_path": str(zip_path), "zip_sha256": zip_hash, "member": member,
                              "member_sha256": member_hash, "member_position": position,
                              "conversation_id": identity, "raw_sha256": raw_hash}
                if identity in seen:
                    require(seen[identity] == raw_hash, "Conflicting duplicate official conversation ID")
                    duplicates.append(provenance)
                    continue
                seen[identity] = raw_hash
                normalized = normalize(obj)
                messages, segments = analysis_messages(normalized)
                created_at, updated_at = date(raw.get("create_time")), date(raw.get("update_time"))
                if created_at:
                    created.append(created_at)
                    months[created_at[:7]] += 1
                fp = text_fingerprint(normalized["analysis_messages"])
                overlaps = [{**r, "same_messages": r["input_fingerprint"] == normalized["input_fingerprint"],
                             "same_role_text": r["text_fingerprint"] == fp} for r in previous.get(identity, [])]
                matches.extend({"conversation_id": identity, **r} for r in overlaps)
                same = [r for r in overlaps if r["account_key"] == account_key and r["same_role_text"]]
                record = {**provenance, "created_at": created_at, "updated_at": updated_at,
                          "input_fingerprint": normalized["input_fingerprint"], "role_text_fingerprint": fp,
                          "normalized_state": normalized["state"], "analysis_messages": len(normalized["analysis_messages"]),
                          "segmented_messages": len(messages), "analysis_characters": sum(len(m["text"]) for m in messages),
                          "overlap": overlaps, "jobs": []}
                inventory.append(record)
                if same and skip_existing:
                    record["selection_state"] = "existing_same_account_content"
                    counts[record["selection_state"]] += 1
                    continue
                structural = [i for i in normalized["issues"] if not any(i.startswith(m["id"] + ":") for m in normalized["messages"])]
                exclusions = Counter(m["exclusion"] for m in normalized["messages"] if m["exclusion"])
                attachments = sum(len(m["attachments"]) for m in normalized["messages"])
                if normalized["issues"] or attachments or exclusions:
                    gaps.append({"account_key": account_key, "conversation_id": identity,
                                 "reason": "partial_source_coverage" if messages and not structural else "source_structure_or_empty",
                                 "issues": normalized["issues"], "excluded_messages": dict(exclusions),
                                 "attachment_references": attachments, "text_retained": bool(messages and not structural)})
                if structural or not messages:
                    record["selection_state"] = "blocked" if structural else "empty"
                    counts[record["selection_state"]] += 1
                    continue
                object_path = f"packages/{identity}-{raw_hash}.json"
                bulk.save(output / object_path, obj)
                source = {"account_key": account_key, "conversation_id": identity,
                          "source_path": str(output / object_path), "object_path": object_path,
                          "byte_sha256": sha((output / object_path).read_bytes()), "raw_sha256": raw_hash,
                          "content_sha256": obj["content_sha256"], "normalization_version": VERSION,
                          "input_fingerprint": normalized["input_fingerprint"], "normalization_issues": normalized["issues"],
                          "branch": "current", "current_node": normalized["current_node"],
                          "url": obj["url"], "title": raw.get("title") or "", "adapter": ADAPTER,
                          "zip_provenance": provenance, "segmentation_version": SEGMENTATION, "segments": segments}
                jobs = [bulk.ensure_job(store, source, context, primary, profile=profile,
                                        source_policy_revision=source_policy_revision)
                        for context, primary in bulk.chunk_messages(messages)]
                entries.append({"source": source, "jobs": jobs, "message_count": len(messages),
                                "source_characters": sum(len(m["text"]) for m in messages)})
                record.update(jobs=jobs, selection_state="prepared")
                counts["prepared"] += 1
    require(file_sha(zip_path) == zip_hash, "ZIP changed during preparation")
    jobs = [j for e in entries for j in e["jobs"]]
    require(len(inventory) <= bulk.MAX_CONVERSATIONS and len(jobs) <= bulk.MAX_JOBS and len(set(jobs)) == len(jobs),
            "Memory batch bounds or job uniqueness exceeded")
    account_report = {"explicit_account_key": account_key, "existing_accounts": accounts,
                      "explicit_account_exists": account_key in accounts, "matches": matches,
                      "cross_account_matches": sum(r["account_key"] != account_key for r in matches),
                      "identity_policy": "Only explicit same-account conversation+role/text equality can skip; never infer account from ZIP folder."}
    scope = {"zip_sha256": zip_hash, "zip_members": len(filenames), "conversation_members": members,
             "conversations": len(inventory), "duplicate_records": duplicates,
             "created_min": min(created) if created else None, "created_max": max(created) if created else None,
             "created_month_counts": dict(sorted(months.items())), "selection_states": dict(counts),
             "analysis_characters": sum(r["analysis_characters"] for r in inventory), "screen_jobs": len(jobs)}
    manifest = {"version": bulk.POLICY, "adapter": ADAPTER, "created_at": now(), "snapshots": members,
                "store": str(store), "model": bulk.MODEL, "endpoint": bulk.ENDPOINT,
                "max_calls": len(jobs) * 2, "screen_jobs": len(jobs),
                "source_characters": sum(e["source_characters"] for e in entries), "conversations": entries,
                "gaps": gaps, "selection_profile": profile, "selection_profile_source_sha256": profile_source_sha256,
                "scope": scope}
    bulk.save(output / "manifest.json", manifest)
    bulk.save(output / "manifest-seal.json", {"sha256": bulk.digest(manifest)})
    bulk.save(output / "inventory.json", inventory)
    bulk.save(output / "scope.json", scope)
    bulk.save(output / "account-matches.json", account_report)
    bulk.save(output / "gaps.json", gaps)
    return {"scope": scope, "progress": bulk.export(output), "account_matching": account_report}


def initialize(output, workspace):
    return notes.initialize(output, workspace)


def repair_missing_related(output):
    """Offline, batch-scoped recovery for one observed optional empty-reference omission."""
    output, manifest, store = bulk.batch(output)
    require(manifest.get("adapter") == ADAPTER and
            manifest.get("selection_profile", {}).get("version") == "memory-personal-v1",
            "Recovery is limited to the official memory-personal-v1 batch")
    members, _ = bulk.member_jobs(manifest, store)
    report = {"version": "memory-empty-related-v1", "new_api_calls": 0, "repaired": [], "unchanged": []}
    expected_keys = {"title", "summary", "user_message_ids", "related_message_ids"}
    for job_id in members:
        directory, definition, input_data, request = bulk.load_job(store, job_id)
        prior = bulk.read(directory / "state.json")
        if prior.get("state") != "validation_failed" or prior.get("error") != "Invalid item fields":
            continue
        require(definition["identity"].get("selection_profile") == manifest["selection_profile"] and
                input_data["source"].get("adapter") == ADAPTER, "Recovery profile/source differs")
        response_bytes = (directory / "response.raw").read_bytes()
        response = loads(response_bytes)
        choice = response.get("choices", [{}])[0]
        if (bulk.read(directory / "receipt.json").get("http_status") != 200 or response.get("model") != bulk.MODEL
                or "error" in response or choice.get("finish_reason") != "stop"):
            report["unchanged"].append({"job_id": job_id, "reason": "Unsupported provider response"})
            continue
        original = loads(choice.get("message", {}).get("content", "null"))
        if not (isinstance(original, dict) and set(original) == {"disposition", "reason", "items"}
                and isinstance(original["items"], list) and original["items"]
                and all(isinstance(item, dict) and set(item) in (expected_keys, expected_keys - {"related_message_ids"})
                        for item in original["items"])
                and any("related_message_ids" not in item for item in original["items"])):
            report["unchanged"].append({"job_id": job_id, "reason": "Not exactly the optional empty-reference omission"})
            continue
        adapted = {**original, "items": [{"related_message_ids": [], **item} for item in original["items"]]}
        try:
            bulk.verify_refs(input_data, bulk.source_messages(input_data["source"]))
            bulk.validate(adapted, input_data["messages"])
        except ValueError as error:
            report["unchanged"].append({"job_id": job_id, "reason": str(error)})
            continue
        evidence = {"version": report["version"], "recovered_at": now(), "prior_state": prior,
                    "job_id": job_id, "profile_sha256": manifest["selection_profile"]["content_sha256"],
                    "request_sha256": bulk.digest(request), "raw_response_sha256": sha(response_bytes),
                    "original_result_sha256": bulk.digest(original), "adapted_result_sha256": bulk.digest(adapted),
                    "change": "Add only omitted related_message_ids as an empty list; all existing fields unchanged",
                    "new_api_calls": 0, "semantic_rewrite": False}
        require(bulk.read(directory / "state.json") == prior, "Job state changed during offline recovery")
        require(sha((directory / "response.raw").read_bytes()) == evidence["raw_response_sha256"], "Raw response changed")
        bulk.save(directory / "memory-empty-related-v1.json", evidence)
        bulk.save(directory / "result.json", adapted)
        bulk.save(directory / "state.json", {**prior, "state": "complete", "disposition": adapted["disposition"],
                  "error": None, "finished_at": now(), "format_recovery": report["version"],
                  "semantic_review": "model review only; not verified external facts"})
        report["repaired"].append({"job_id": job_id, "stage": definition["identity"]["stage"],
                                   "disposition": adapted["disposition"]})
    bulk.save(output / "memory-empty-related-recovery.json", report)
    return report
