"""Read-only extraction of the user's own messages from Claude Code session logs.

Only top-level ``*.jsonl`` files of one project directory are scanned; subagent
transcripts live in subdirectories and are never read.  Nothing here calls a
model, writes a Vault or workbench database, or copies the raw session files.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

SOURCE = "claude-code"
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")  # no DST since 1991
CONTEXT_CHARS = 200
CHAIN_LIMIT = 2000

_REMINDER = re.compile(r"\A\s*<system-reminder>.*?</system-reminder>\s*", re.S)
_TRAILING_REMINDER = re.compile(r"\s*<system-reminder>(?:(?!<system-reminder>).)*?</system-reminder>\s*\Z", re.S)
_PASTE_OPEN = re.compile(r'<pasted_content(?:\s+id="[^"]*")?\s*>')
_PASTE_CLOSE = re.compile(r'</pasted_content(?:\s+id="[^"]*")?\s*>')
_SLASH = re.compile(r"\A\s*<command-message>.*?</command-message>\s*<command-name>(?P<name>.*?)</command-name>"
                    r"\s*(?:<command-args>(?P<args>.*?)</command-args>)?\s*\Z", re.S)
_NON_HUMAN_PREFIXES = (
    ("<task-notification>", "task_notification"),
    ("<local-command-", "local_command"),
    ("<command-name>", "local_command"),
    ("[Request interrupted", "interrupt"),
)


def _blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def _text(content):
    return "\n".join(b.get("text", "") for b in _blocks(content) if b.get("type") == "text"
                     and isinstance(b.get("text"), str))


def _parse_time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _utc_text(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def parse_bound(value, upper=False):
    """Date-only values are Asia/Shanghai calendar days; --until of a date includes that day.

    Datetimes without an offset are read as Asia/Shanghai as well.
    """
    if value is None:
        return None
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=SHANGHAI)
        return (day + timedelta(days=1) if upper else day).astimezone(timezone.utc)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Invalid time bound {value!r}; use YYYY-MM-DD or ISO 8601") from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=SHANGHAI)
    return moment.astimezone(timezone.utc)


def clean_text(raw):
    """Strip host-added system reminders and locate paste spans in the cleaned text."""
    text = raw
    while True:
        stripped = _REMINDER.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    while True:
        stripped = _TRAILING_REMINDER.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    slash = None
    match = _SLASH.match(text)
    if match:
        slash = match.group("name").strip()
        args = (match.group("args") or "").strip()
        text = f"{slash} {args}".strip()
    text = text.strip()
    return text, paste_spans(text), slash


def paste_spans(text):
    """Return [start, end) offsets of each pasted block, tags included.

    Closing tags carry the same id as other pastes, so pairing is sequential:
    an opening tag followed by another opening tag before any close is treated
    as unclosed and ends where the next paste starts (``closed`` false).
    """
    spans = []
    position = 0
    while True:
        opening = _PASTE_OPEN.search(text, position)
        if not opening:
            return spans
        closing = _PASTE_CLOSE.search(text, opening.end())
        following = _PASTE_OPEN.search(text, opening.end())
        if closing and (not following or closing.start() < following.start()):
            spans.append({"start": opening.start(), "end": closing.end(), "closed": True})
            position = closing.end()
        else:
            end = following.start() if following else len(text)
            spans.append({"start": opening.start(), "end": end, "closed": False})
            position = end


def _norm_label(value):
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


class _Index:
    """Per-file parent links with only the tails needed for context and button checks."""

    def __init__(self):
        self.nodes = {}

    def add(self, record, kind, payload=None):
        uuid = record.get("uuid")
        if isinstance(uuid, str) and uuid not in self.nodes:
            self.nodes[uuid] = (record.get("parentUuid"), kind, payload)

    def walk(self, parent):
        seen = set()
        steps = 0
        while isinstance(parent, str) and parent in self.nodes and parent not in seen and steps < CHAIN_LIMIT:
            seen.add(parent)
            steps += 1
            node = self.nodes[parent]
            yield node
            parent = node[0]


def _classify_user(record):
    """Return (reason, None) to exclude a user record, or (None, origin_kind) for a human record."""
    origin = record.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    content = (record.get("message") or {}).get("content")
    blocks = _blocks(content)
    if any(b.get("type") == "tool_result" for b in blocks) or record.get("toolUseResult") is not None:
        return "tool_result", kind
    if record.get("isSidechain"):
        return "sidechain", kind
    if record.get("isCompactSummary"):
        return "compact_summary", kind
    if kind == "task-notification":
        return "task_notification", kind
    if kind == "peer":
        return "peer", kind
    if record.get("turnOrigin") not in (None, "human"):
        return "host_synthetic_or_agent_start", kind
    if record.get("isMeta"):
        return "meta_or_skill_injection", kind
    if kind != "human":
        text = _text(content).lstrip()
        if text.startswith("<system-reminder>"):
            return "host_injected", kind
        for prefix, reason in _NON_HUMAN_PREFIXES:
            if text.startswith(prefix):
                return reason, kind
        return "non_human_origin", kind
    text = _text(content).lstrip()
    for prefix, reason in _NON_HUMAN_PREFIXES:
        if text.startswith(prefix):
            return reason, kind
    return None, kind


def _queued_human(record):
    attachment = record.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
        return False
    origin = attachment.get("origin")
    human_turn = attachment.get("humanTurn", record.get("humanTurn"))
    return (isinstance(origin, dict) and origin.get("kind") == "human" and human_turn is True
            and not attachment.get("isMeta") and attachment.get("commandMode", "prompt") == "prompt")


def _scan_file(path, stats):
    """Yield candidate messages from one session file along with its index and titles."""
    index = _Index()
    titles = {}
    candidates = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                stats["malformed_lines"] += 1
                continue
            if not isinstance(record, dict):
                stats["malformed_lines"] += 1
                continue
            kind = record.get("type")
            if kind == "custom-title" and isinstance(record.get("customTitle"), str):
                titles[record.get("sessionId")] = record["customTitle"]
                continue
            if kind == "assistant":
                blocks = _blocks((record.get("message") or {}).get("content"))
                text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text"
                               and isinstance(b.get("text"), str)).strip()
                labels = None
                for block in blocks:
                    if block.get("type") == "tool_use" and str(block.get("name", "")).endswith("wrap_up"):
                        actions = (block.get("input") or {}).get("actions") or []
                        if isinstance(actions, str):
                            try:
                                actions = json.loads(actions)
                            except json.JSONDecodeError:
                                actions = []
                        labels = [_norm_label(a["label"]) for a in actions
                                  if isinstance(a, dict) and isinstance(a.get("label"), str) and a["label"].strip()]
                index.add(record, "assistant", {"text_tail": text[-CONTEXT_CHARS:] if text else None,
                                                "labels": labels})
                continue
            if kind == "user":
                reason, _ = _classify_user(record)
                if reason:
                    index.add(record, "other")
                    candidates.append(("excluded", reason, record))
                    continue
                index.add(record, "human")
                candidates.append(("record", None, record))
                continue
            if kind == "attachment" and _queued_human(record):
                index.add(record, "human")
                candidates.append(("queued", None, record))
                continue
            index.add(record, "other")
    return index, titles, candidates


def _context(index, parent):
    """Nearest preceding assistant text tail and wrap-up labels within the same turn."""
    text_tail = None
    labels = None
    for _, kind, payload in index.walk(parent):
        if kind == "human":
            break
        if kind != "assistant":
            continue
        if text_tail is None and payload["text_tail"]:
            text_tail = payload["text_tail"]
        if labels is None and payload["labels"] is not None:
            labels = payload["labels"]
        if text_tail is not None and labels is not None:
            break
    return text_tail, labels


def extract(directory, since=None, until=None, include_context=True):
    """Scan top-level session files and return (messages, summary). Read only."""
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")
    files = sorted(p for p in root.iterdir() if p.is_file() and p.suffix == ".jsonl")
    stats = Counter()
    excluded = Counter()
    seen = set()
    titles_by_owner = {}
    titles_any = {}
    messages = []
    for path in files:
        index, titles, candidates = _scan_file(path, stats)
        for session_id, title in titles.items():
            titles_any[session_id] = title
            if session_id == path.stem:
                titles_by_owner[session_id] = title
        for kind, reason, record in candidates:
            uuid = record.get("uuid")
            if not isinstance(uuid, str) or not uuid:
                stats["records_without_uuid"] += 1
                continue
            if uuid in seen:
                stats["duplicate_copies"] += 1
                continue
            seen.add(uuid)
            if kind == "excluded":
                excluded[reason] += 1
                continue
            if kind == "queued":
                attachment = record["attachment"]
                prompt = attachment.get("prompt")
                raw = _text(prompt)
                images = any(b.get("type") == "image" for b in _blocks(prompt)) if not isinstance(prompt, str) else False
                stamp = attachment.get("timestamp") or record.get("timestamp")
            else:
                content = (record.get("message") or {}).get("content")
                raw = _text(content)
                images = (any(b.get("type") == "image" for b in _blocks(content))
                          or bool(record.get("imagePasteIds")))
                stamp = record.get("timestamp")
            moment = _parse_time(stamp)
            if moment is None:
                excluded["invalid_timestamp"] += 1
                continue
            text, spans, slash = clean_text(raw)
            if not text and not images:
                excluded["empty_after_cleaning"] += 1
                continue
            session_id = record.get("sessionId") or path.stem
            tail, labels = _context(index, record.get("parentUuid"))
            pasted = sum(s["end"] - s["start"] for s in spans)
            message = {
                "source": SOURCE,
                "source_id": f"{SOURCE}:{session_id}:{uuid}",
                "session_id": session_id,
                "message_uuid": uuid,
                "timestamp_utc": _utc_text(moment),
                "date_shanghai": moment.astimezone(SHANGHAI).date().isoformat(),
                "kind": kind,
                "cwd": record.get("cwd"),
                "session_title": None,
                "source_file": path.name,
                "text": text,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "chars": len(text),
                "own_chars": len(text) - pasted,
                "is_paste": bool(spans),
                "paste_spans": spans,
                "has_image": images,
                "slash_command": slash,
                "is_button_reply": bool(labels) and _norm_label(text) in labels,
            }
            if include_context:
                message["previous_assistant_tail"] = tail
            messages.append((moment, message))
    for _, message in messages:
        sid = message["session_id"]
        message["session_title"] = titles_by_owner.get(sid, titles_any.get(sid))
    total = len(messages)
    messages = [(m, msg) for m, msg in messages if (since is None or m >= since) and (until is None or m < until)]
    messages.sort(key=lambda item: (item[0], item[1]["session_id"], item[1]["message_uuid"]))
    result = [msg for _, msg in messages]
    summary = {
        "files_scanned": len(files),
        "messages_before_time_filter": total,
        "messages": len(result),
        "by_kind": dict(Counter(m["kind"] for m in result)),
        "chars": sum(m["chars"] for m in result),
        "own_chars": sum(m["own_chars"] for m in result),
        "paste_messages": sum(1 for m in result if m["is_paste"]),
        "image_messages": sum(1 for m in result if m["has_image"]),
        "button_replies": sum(1 for m in result if m["is_button_reply"]),
        "slash_commands": sum(1 for m in result if m["slash_command"]),
        "sessions": len({m["session_id"] for m in result}),
        "duplicate_copies_skipped": stats["duplicate_copies"],
        "malformed_lines": stats["malformed_lines"],
        "records_without_uuid": stats["records_without_uuid"],
        "excluded_unique_user_records": dict(sorted(excluded.items())),
    }
    return result, summary


def segments(messages):
    """Summarise messages by (session, Asia/Shanghai date)."""
    groups = {}
    for message in messages:
        key = (message["session_id"], message["date_shanghai"])
        group = groups.setdefault(key, {
            "session_id": key[0], "date_shanghai": key[1], "session_title": message["session_title"],
            "first_utc": message["timestamp_utc"], "last_utc": message["timestamp_utc"],
            "messages": 0, "queued": 0, "chars": 0, "own_chars": 0, "paste_messages": 0,
            "image_messages": 0, "button_replies": 0, "cwds": [], "first_uuid": message["message_uuid"],
            "last_uuid": message["message_uuid"]})
        group["messages"] += 1
        group["queued"] += message["kind"] == "queued"
        group["chars"] += message["chars"]
        group["own_chars"] += message["own_chars"]
        group["paste_messages"] += message["is_paste"]
        group["image_messages"] += message["has_image"]
        group["button_replies"] += message["is_button_reply"]
        if message["cwd"] and message["cwd"] not in group["cwds"]:
            group["cwds"].append(message["cwd"])
        if message["timestamp_utc"] < group["first_utc"]:
            group["first_utc"], group["first_uuid"] = message["timestamp_utc"], message["message_uuid"]
        if message["timestamp_utc"] >= group["last_utc"]:
            group["last_utc"], group["last_uuid"] = message["timestamp_utc"], message["message_uuid"]
    return sorted(groups.values(), key=lambda g: (g["date_shanghai"], g["first_utc"], g["session_id"]))


def write_new(path, data):
    """Write bytes to a path that must not exist yet; never overwrites, never leaves a partial file."""
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Refusing to overwrite existing file: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    finally:
        os.unlink(temporary)


def run(directory, out, output_format="jsonl", since=None, until=None, segments_out=None, include_context=True):
    targets = [Path(out)] + ([Path(segments_out)] if segments_out else [])
    if len({t.resolve() for t in targets}) != len(targets):
        raise ValueError("Messages and segments outputs must be different files")
    root = Path(directory).resolve()
    for target in targets:
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Refusing to overwrite existing file: {target}")
        if root == target.resolve().parent:
            raise ValueError("Write outputs outside the scanned session directory")
    start, end = parse_bound(since), parse_bound(until, upper=True)
    if start and end and start >= end:
        raise ValueError("--since must be earlier than --until")
    messages, summary = extract(directory, start, end, include_context)
    if output_format == "json":
        payload = json.dumps({"summary": summary, "messages": messages}, ensure_ascii=False, indent=2) + "\n"
    else:
        payload = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages)
    write_new(targets[0], payload.encode("utf-8"))
    summary["out"] = str(targets[0])
    summary["since_utc"] = _utc_text(start) if start else None
    summary["until_utc"] = _utc_text(end) if end else None
    if segments_out:
        grouped = segments(messages)
        write_new(targets[1], (json.dumps({"timezone": "Asia/Shanghai", "since_utc": summary["since_utc"],
                                           "until_utc": summary["until_utc"], "segments": grouped},
                                          ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        summary["segments"] = len(grouped)
        summary["segments_out"] = str(targets[1])
    return summary
