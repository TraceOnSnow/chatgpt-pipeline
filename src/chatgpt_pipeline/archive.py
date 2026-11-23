"""Read-only archive lookup and covered-source evidence over one verified commit.

No model call, no workspace or Vault access. Message text and ``text_sha256``
come from the same current-branch normalization the pipeline uses everywhere
(``sha(text.encode())`` over the normalized message text), so evidence produced
here matches what screening, notes and the Library compare against.
"""
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .incremental import _voice, latest_commit
from .index import now
from .normalize import VERSION, normalize
from .verify import require, sha, verify

SEARCH_SCHEMA = "chatgpt-archive-search-v1"
READ_SCHEMA = "chatgpt-archive-conversation-v1"
COVERED_TYPE = "chatgpt-source-first-covered-v1"
GENERATOR = "chatgpt-pipeline covered-evidence"
MAX_EVIDENCE = 200  # markdown-library cover-source accepts 1..200 references
MAX_DECISION = 2000
HITS_PER_CONVERSATION = 3


def _zone(name):
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError) as error:
        raise ValueError("Unknown timezone: " + str(name)) from error


def _bound(value, zone, *, end):
    """Plain dates cover the whole local day (``until`` inclusive); datetimes need a timezone."""
    if value is None:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        moment = datetime.fromisoformat(value)
        require(moment.tzinfo is not None, "Date-times need an explicit timezone; plain dates use --tz")
        return moment.timestamp()
    return datetime.combine(day + timedelta(days=1 if end else 0), time(), zone).timestamp()


def _stamp(value, zone):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None, None
    try:
        moment = datetime.fromtimestamp(value, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None, None
    return moment.isoformat(), moment.astimezone(zone).isoformat()


def _cut(text, limit):
    return text if len(text) <= limit else text[:limit] + "…"


def _snippet(text, needle, width):
    """A short window around the first hit; never the whole message."""
    flat = " ".join(text.split())
    at = flat.casefold().find(needle.casefold()) if needle else -1
    if at < 0:
        return _cut(flat, width)
    start = max(0, at - width // 2)
    piece = flat[start:start + width]
    return ("…" if start else "") + piece + ("…" if start + width < len(flat) else "")


def _messages(package):
    """Normalized current-branch messages with voice flags and text fingerprints."""
    normalized = normalize(package)
    mapping = package["raw"].get("mapping") if isinstance(package["raw"].get("mapping"), dict) else {}
    rows = []
    for message in normalized["messages"]:
        raw = (mapping.get(message["node_id"]) or {}).get("message") if message["node_id"] else None
        voice, dictation, audio = _voice(raw, message)
        rows.append({"id": message["id"], "role": message["role"], "created": message["created"],
                     "text": message["text"], "text_sha256": sha(message["text"].encode()),
                     "exclusion": message["exclusion"], "voice": voice, "dictation": dictation, "audio": audio,
                     "attachments": len(message["attachments"])})
    return normalized, rows


def _resolve(root, commit):
    root = Path(root).absolute()
    return root, (Path(commit).absolute() if commit else latest_commit(root))


def _source(verified, entry):
    return {"account_key": verified["account_key"], "commit_sha256": verified["commit_sha256"],
            "catalog_sha256": verified["catalog_sha256"], "conversation_id": entry["id"],
            "body_status": entry["body_status"], "object_path": entry.get("object"),
            "raw_sha256": entry.get("raw_sha256"), "content_sha256": entry.get("content_sha256"),
            "normalization_version": VERSION}


def _find(catalog, identity):
    """Exact conversation ID, or a unique prefix of at least 8 characters."""
    entries = catalog["conversations"]
    exact = [e for e in entries if e["id"] == identity]
    if exact:
        return exact[0]
    require(isinstance(identity, str) and len(identity) >= 8, "Conversation not in this commit: " + str(identity))
    matches = [e for e in entries if e["id"].startswith(identity)]
    require(len(matches) == 1, ("Ambiguous" if matches else "Unknown") + " conversation ID prefix: " + identity)
    return matches[0]


def _load(root, commit, identity):
    """Verify the whole commit, retaining only the requested conversation's normalized messages."""
    root, commit_path = _resolve(root, commit)

    def retain(package):
        cid = package["conversation_id"]
        if cid == identity or (len(identity) >= 8 and cid.startswith(identity)):
            normalized, rows = _messages(package)
            return {"rows": rows, "state": normalized["state"], "issues": normalized["issues"][:20],
                    "issue_count": len(normalized["issues"]), "input_fingerprint": normalized["input_fingerprint"]}
        return None

    verified = verify(root, commit_path, retain=retain)
    entry = _find(verified["catalog"], identity)
    require(entry.get("object"), "Conversation body was never fetched; nothing verified to read")
    return verified, entry, verified["packages"][entry["object"]]


def search(account_root, commit=None, *, since=None, until=None, tz="UTC", keywords=(), any_keyword=False,
           conversation_ids=(), voice=None, limit=30, snippet_chars=60):
    """Candidate conversations by date of own messages, own-text keywords, ID and voice use."""
    zone = _zone(tz)
    start, end = _bound(since, zone, end=False), _bound(until, zone, end=True)
    keywords = [k for k in keywords if k.strip()]
    require(limit >= 1 and snippet_chars >= 10, "--limit must be >= 1 and --snippet-chars >= 10")
    root, commit_path = _resolve(account_root, commit)

    def in_range(stamp):
        if start is None and end is None:
            return True
        if not isinstance(stamp, (int, float)):
            return False
        return (start is None or stamp >= start) and (end is None or stamp < end)

    def retain(package):
        normalized, rows = _messages(package)
        users = [r for r in rows if r["role"] == "user"]
        scoped = [r for r in users if in_range(r["created"])]
        hits, found = [], set()
        for row in scoped:
            if row["exclusion"]:
                continue
            folded = row["text"].casefold()
            matched = [k for k in keywords if k.casefold() in folded]
            if matched:
                found.update(matched)
                if len(hits) < HITS_PER_CONVERSATION:
                    hits.append({"message_id": row["id"], "created": row["created"], "keywords": matched,
                                 "snippet": _snippet(row["text"], matched[0], snippet_chars)})
        stamps = [r["created"] for r in users if isinstance(r["created"], (int, float))]
        return {"state": normalized["state"], "user_messages": sum(not r["exclusion"] for r in users),
                "user_messages_without_text": sum(bool(r["exclusion"]) for r in users),
                "user_messages_in_range": len(scoped), "voice_messages": sum(r["voice"] for r in users),
                "dictation_messages": sum(r["dictation"] for r in users),
                "user_characters": sum(len(r["text"]) for r in users if not r["exclusion"]),
                "first_user": min(stamps) if stamps else None, "last_user": max(stamps) if stamps else None,
                "hits": hits, "hit_messages": sum(1 for r in scoped if not r["exclusion"] and any(
                    k.casefold() in r["text"].casefold() for k in keywords)), "found": sorted(found)}

    verified = verify(root, commit_path, retain=retain)
    entries = verified["catalog"]["conversations"]
    if conversation_ids:
        chosen = {_find(verified["catalog"], identity)["id"] for identity in conversation_ids}
        entries = [e for e in entries if e["id"] in chosen]
    rows, skipped = [], 0
    for entry in entries:
        if not entry.get("object"):
            skipped += 1
            continue
        info = verified["packages"][entry["object"]]["package"]
        if (start is not None or end is not None) and not info["user_messages_in_range"]:
            continue
        if voice is True and not info["voice_messages"] or voice is False and info["voice_messages"]:
            continue
        title = entry.get("title") or ""
        title_hits = [k for k in keywords if k.casefold() in title.casefold()]
        matched = set(info["found"]) | set(title_hits)
        if keywords and not (matched if any_keyword else matched == set(keywords)):
            continue
        first, last = _stamp(info["first_user"], zone)[1], _stamp(info["last_user"], zone)[1]
        rows.append({"conversation_id": entry["id"], "title": title, "body_status": entry["body_status"],
                     "state": info["state"], "first_user_message": first, "last_user_message": last,
                     "user_messages": info["user_messages"], "user_messages_in_range": info["user_messages_in_range"],
                     "user_messages_without_text": info["user_messages_without_text"],
                     "user_characters": info["user_characters"], "voice_messages": info["voice_messages"],
                     "dictation_messages": info["dictation_messages"], "title_keyword_hits": title_hits,
                     "keyword_hit_messages": info["hit_messages"],
                     "hits": [{**h, "created": _stamp(h["created"], zone)[1]} for h in info["hits"]],
                     "_sort": info["last_user"] or 0})
    rows.sort(key=lambda r: (-r["_sort"], r["conversation_id"]))
    for row in rows:
        del row["_sort"]
    return {"schema": SEARCH_SCHEMA, "generated_at": now(), "read_only": True, "model_calls": 0,
            "source": {"account_key": verified["account_key"], "commit_sha256": verified["commit_sha256"],
                       "catalog_sha256": verified["catalog_sha256"], "normalization_version": VERSION,
                       "conversations": len(verified["catalog"]["conversations"])},
            "query": {"since": since, "until": until, "tz": tz, "keywords": keywords,
                      "keyword_mode": "any" if any_keyword else "all", "conversation_ids": list(conversation_ids),
                      "voice": voice, "limit": limit},
            "matched": len(rows), "returned": min(len(rows), limit), "without_verified_body": skipped,
            "conversations": rows[:limit],
            "notes": ["Keywords match your own message text (case-insensitive) or the title; with a date range "
                      "only your messages in that range are searched.",
                      "Voice counts come from voice_mode_message metadata and audio parts; absence does not prove "
                      "typed input. Snippets are truncated; read a conversation for full text."]}


def read(account_root, commit=None, *, conversation, tz="UTC", context=False, context_chars=200):
    """Your normalized messages in one conversation, each with ID, time and text_sha256."""
    zone = _zone(tz)
    require(context_chars >= 10, "--context-chars must be >= 10")
    verified, entry, loaded = _load(account_root, commit, conversation)
    body = loaded["package"]
    rows = body["rows"]
    messages, gaps, previous = [], [], None
    for row in rows:
        if row["role"] == "assistant" and not row["exclusion"]:
            previous = row
        if row["role"] != "user":
            continue
        utc, local = _stamp(row["created"], zone)
        if row["exclusion"]:
            gaps.append({"id": row["id"], "created": local, "exclusion": row["exclusion"],
                         "voice": row["voice"], "audio": row["audio"], "attachments": row["attachments"]})
            continue
        item = {"id": row["id"], "created_utc": utc, "created": local, "text": row["text"],
                "characters": len(row["text"]), "text_sha256": row["text_sha256"], "voice": row["voice"],
                "dictation": row["dictation"], "attachments": row["attachments"]}
        if context:
            item["previous_assistant"] = None if previous is None else {
                "id": previous["id"], "text": _cut(previous["text"], context_chars),
                "truncated": len(previous["text"]) > context_chars, "text_sha256": previous["text_sha256"]}
        messages.append(item)
        previous = None
    return {"schema": READ_SCHEMA, "generated_at": now(), "read_only": True, "model_calls": 0,
            "source": {**_source(verified, entry), "byte_sha256": loaded["byte_sha256"],
                       "input_fingerprint": body["input_fingerprint"]},
            "title": entry.get("title") or "", "tz": tz, "state": body["state"], "issues": body["issues"],
            "issue_count": body["issue_count"], "user_messages": len(messages),
            "voice_messages": sum(m["voice"] for m in messages), "messages": messages,
            "user_messages_without_text": gaps,
            "notes": ["Text is the pipeline's normalized current-branch text; text_sha256 = sha256 of its UTF-8 bytes.",
                      "previous_assistant is truncated context only, not your words."
                      if context else "Assistant replies omitted; pass --context for truncated preceding replies."]}


def covered_evidence(account_root, commit=None, *, conversation, message_ids=(), decision=None, actor=None,
                     allow_stale=False):
    """A ``chatgpt-source-first-covered-v1`` record for markdown-library cover-source --source."""
    require(decision is None or (isinstance(decision, str) and decision.strip() and len(decision) <= MAX_DECISION
                                 and "\x00" not in decision), f"decision must be nonempty text up to {MAX_DECISION} characters")
    require(actor is None or (actor.strip() and len(actor) <= 100 and not any(c in actor for c in "\r\n\x00")),
            "actor must be nonempty single-line text up to 100 characters")
    verified, entry, loaded = _load(account_root, commit, conversation)
    require(entry["body_status"] == "current" or allow_stale,
            f"Conversation body is {entry['body_status']}; refresh the archive or pass --allow-stale")
    body = loaded["package"]
    require(body["state"] == "ready", f"Conversation normalization is {body['state']}; inspect issues with archive-read")
    readable = [r for r in body["rows"] if not r["exclusion"]]
    if message_ids:
        wanted = list(dict.fromkeys(message_ids))
        by_id = {r["id"]: r for r in readable}
        missing = [m for m in wanted if m not in by_id]
        require(not missing, "Not readable messages on the current branch: " + ", ".join(missing[:10]))
        chosen = [r for r in readable if r["id"] in set(wanted)]
        selection = "explicit_message_ids"
    else:
        chosen = [r for r in readable if r["role"] == "user"]
        selection = "all_readable_user_messages"
    require(any(r["role"] == "user" for r in chosen), "Evidence needs at least one of your own (user) messages")
    require(len(chosen) <= MAX_EVIDENCE,
            f"{len(chosen)} messages exceed the {MAX_EVIDENCE}-reference limit; pass explicit --message IDs")
    record = {"type": COVERED_TYPE, "account_key": verified["account_key"], "conversation_id": entry["id"],
              "evidence": [{"source_message_id": r["id"], "role": r["role"], "text_sha256": r["text_sha256"]}
                           for r in chosen],
              "input_source": {**_source(verified, entry), "byte_sha256": loaded["byte_sha256"],
                               "input_fingerprint": body["input_fingerprint"], "generator": GENERATOR,
                               "selection": selection,
                               "user_messages_without_text": sum(
                                   1 for r in body["rows"] if r["role"] == "user" and r["exclusion"])}}
    if decision is not None:
        record["decision"] = decision
    if actor is not None:
        record["actor"] = actor
    return record


def search_markdown(result):
    lines = [f"# Archive search ({result['generated_at']})", "",
             f"Commit {result['source']['commit_sha256'][:12]}; matched {result['matched']}, "
             f"showing {result['returned']}; model calls: 0.", "",
             "| conversation | last own message | own msgs | voice | title | hits |", "|---|---|---|---|---|---|"]
    for row in result["conversations"]:
        hits = " / ".join(h["snippet"] for h in row["hits"]).replace("|", "/")
        lines.append(f"| {row['conversation_id']} | {(row['last_user_message'] or '')[:16]} | {row['user_messages']} | "
                     f"{row['voice_messages']} | {row['title'].replace('|', '/')[:40]} | {hits} |")
    return "\n".join(lines) + "\n"


def read_markdown(result):
    source = result["source"]
    lines = [f"# {result['title'] or source['conversation_id']}", "",
             f"- Conversation: `{source['conversation_id']}` ({source['body_status']}, {result['state']})",
             f"- Commit: `{source['commit_sha256']}`",
             f"- Own messages: {result['user_messages']} (voice {result['voice_messages']}); "
             f"without text: {len(result['user_messages_without_text'])}", ""]
    for message in result["messages"]:
        previous = message.get("previous_assistant")
        if previous:
            lines += [f"> Assistant `{previous['id']}` (truncated context): " + " ".join(previous["text"].split()), ""]
        flags = " · voice" if message["voice"] else " · dictation" if message["dictation"] else ""
        lines += [f"## `{message['id']}` · {message['created'] or 'no time'}{flags}",
                  f"<!-- text_sha256 {message['text_sha256']} -->", "", message["text"], ""]
    for gap in result["user_messages_without_text"]:
        lines.append(f"- No text: `{gap['id']}` ({gap['exclusion']}, {gap['created'] or 'no time'})")
    return "\n".join(lines) + "\n"
