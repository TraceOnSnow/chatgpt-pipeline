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
# --- Library-write heuristic -------------------------------------------------
# Tool calls an assistant made after one human message (until the next human
# message) that look like writes to the content library.  This is a heuristic:
# it sees only top-level session files (not subagent transcripts) and only the
# three shapes below, so "no write" never proves a message was not saved.
VAULT_ROOT = "/home/trace/projects/personal-records-vault"
LIBRARY_CLI_WRITE_COMMANDS = ("create", "replace", "edit", "import", "organize", "move", "merge",
                              "publish-article", "cover-source")
# Subcommands that only preview unless --apply is given.
LIBRARY_CLI_PREVIEW_DEFAULT = ("edit", "organize", "move", "merge", "publish-article", "cover-source")
LIBRARY_CLI_ALL_COMMANDS = LIBRARY_CLI_WRITE_COMMANDS + (
    "init", "list", "get", "unpublish", "publication", "search", "check", "time-report", "sources",
    "source-messages", "navigation", "prepare-work-review", "register", "track-existing", "restore-original",
    "tracker-summary", "history", "restore", "publish", "delete", "recover", "confirm-missing-delete",
    "publications", "sync-plan", "sync", "bootstrap", "adopt", "map-imported", "relations", "sync-review",
    "sync-resolve", "lifecycle-review", "lifecycle-resolve")
LIBRARY_MCP_WRITE_TOOLS = ("create_note", "replace_note", "edit_note", "merge_notes", "move_note", "organize_notes")
FILE_WRITE_TOOLS = ("Edit", "Write", "MultiEdit")
WRITE_SNIPPET_CHARS = 200
WRITE_SEGMENT_CHARS = 4000
HUMAN_SEARCH_LIMIT = 50000

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
# The executable name, not src/markdown_library, .markdown-library/, skills/markdown-library/ or *-http.service.
_LIBRARY_CLI = re.compile(r"(?<![\w.-])(?:[^\s'\"\[(=]*/)?markdown-library(?![\w/.:-])")
_ALIAS = re.compile(r"(?<![\w$])([A-Za-z_][A-Za-z0-9_]*)=[\"']?(?:\$\()?[^\s\"';|]*markdown-library(?![\w/.:-])")
_FUNCTION_ALIAS = re.compile(r"(?<![\w$])([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{[^}]*?markdown-library(?![\w/.:-])")
# Python wrappers: NAME = ['.venv/bin/markdown-library', ...] then ml('replace', ...) or NAME + ['create', ...].
_PY_LIST_ALIAS = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\s*=\s*\[[^\]\n]*markdown-library(?![\w/.:-])")
_PY_SUBCOMMAND = re.compile(r"[(\[,+]\s*['\"](" + "|".join(re.escape(c) for c in LIBRARY_CLI_WRITE_COMMANDS) + r")['\"]")
_SCRIPT_TARGET = re.compile(r"(?:>>?|\btee\s+(?:-a\s+)?)\s*['\"]?([^\s'\";|&>]+\.py)\b")
MAX_NOTE_IDS = 50
_TOKEN_SPLIT = re.compile(r"[\s,\[\]()\"'=]+")
_HEREDOC = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
_URL = re.compile(r"https?://\S+")
_LINE_END = re.compile(r"\n|;|&&|\|\||(?<!\|)\|(?!\|)")

# --- Dictation candidate rule -------------------------------------------------
# Deterministic and explainable: long own text (pastes and URLs removed,
# whitespace not counted), not a button reply or slash command, and either an
# experience or feeling marker or an explicit save intent.  Tune these
# constants, not code.  ("刚刚"/"刚才" are left out: they mostly point back at
# the assistant's previous reply rather than at the user's own experience.)
DICTATION_MIN_CHARS = 60
DICTATION_EXPERIENCE_MARKERS = (
    "今天", "昨天", "前天", "上周", "当时", "那时", "以前", "小时候", "小学", "初中", "高中", "记得", "经历",
    "体验", "感受", "感觉", "觉得", "印象", "心情", "喜欢", "讨厌", "开心", "难过", "焦虑", "后悔", "面试",
    "笔试", "玩了", "玩过", "看了", "看过", "读了", "读过", "听了", "去了", "通关", "我发现", "想起")
DICTATION_SAVE_MARKERS = (
    "记下", "记一下", "记录", "写进", "写入", "存进", "存到", "收进", "入库", "整理", "感想", "面经", "补充",
    "备忘", "日记")
DICTATION_PREVIEW_CHARS = 80

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

    def nearest_human(self, parent):
        """uuid of the closest human ancestor, or None when the chain breaks (e.g. at a compaction)."""
        seen = set()
        while isinstance(parent, str) and parent in self.nodes and parent not in seen and len(seen) < HUMAN_SEARCH_LIMIT:
            seen.add(parent)
            node = self.nodes[parent]
            if node[1] == "human":
                return parent
            parent = node[0]
        return None


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


def _snippet(text):
    return re.sub(r"\s+", " ", str(text)).strip()[:WRITE_SNIPPET_CHARS]


def _vault_relative(path):
    text = str(path or "")
    prefix = VAULT_ROOT.rstrip("/") + "/"
    return text[len(prefix):] if text.startswith(prefix) else None


def _ids(text):
    return sorted(set(_UUID.findall(text)))


def _cli_segment(command, start, stop):
    """Text of one invocation: to the end of its shell line, or through its heredoc body."""
    rest = command[start:stop]
    first_line = rest.split("\n", 1)[0]
    heredoc = _HEREDOC.search(first_line)
    if heredoc:
        ending = re.search(rf"^\s*{re.escape(heredoc.group(1))}\s*$", rest, re.M)
        rest = rest[:ending.start()] if ending else rest
    else:
        ending = _LINE_END.search(rest)
        rest = rest[:ending.start()] if ending else rest
    return rest[:WRITE_SEGMENT_CHARS]


def _python_library_writes(command):
    """Writes through a Python list wrapper around the CLI; IDs are every UUID in the command."""
    if not _PY_LIST_ALIAS.search(command):
        return []
    found = sorted({m.group(1) for m in _PY_SUBCOMMAND.finditer(command)})
    applied = bool(re.search(r"['\"]--apply['\"]", command))
    ids = _ids(command)[:MAX_NOTE_IDS]
    return [{"via": "python", "operation": name, "applied": applied if name in LIBRARY_CLI_PREVIEW_DEFAULT else True,
             "note_ids": ids, "vault_paths": [], **({} if ids else {"snippet": _snippet(command)})}
            for name in found]


class ScriptRegistry:
    """Per-session record of helper scripts whose text calls library write subcommands.

    A later Bash command that mentions such a script by file name and runs Python counts as
    the same writes (``via: script``); scripts copied from it with ``>``/``>>`` inherit them.
    """

    def __init__(self):
        self.scripts = {}

    def remember(self, path, operations):
        if path and operations:
            self.scripts[Path(str(path)).name] = sorted({o["operation"] for o in operations})

    def runs(self, command):
        if "python" not in command:
            return []
        names = sorted(n for n in self.scripts if re.search(rf"(?<![\w.-]){re.escape(n)}\b", command))
        if not names:
            return []
        operations = sorted({o for n in names for o in self.scripts[n]})
        for target in _SCRIPT_TARGET.findall(command):
            self.scripts.setdefault(Path(target).name, operations)
        ids = _ids(command)[:MAX_NOTE_IDS]
        return [{"via": "script", "operation": o, "applied": o not in LIBRARY_CLI_PREVIEW_DEFAULT or "--apply" in command,
                 "script": names, "note_ids": ids, "vault_paths": [],
                 **({} if ids else {"snippet": _snippet(command)})} for o in operations]


def bash_library_writes(command, scripts=None):
    """Heuristically find markdown-library write subcommands in one Bash command string."""
    if not isinstance(command, str):
        return []
    if "markdown-library" not in command:
        return scripts.runs(command) if scripts else []
    python = _python_library_writes(command)
    if scripts is not None:
        for target in _SCRIPT_TARGET.findall(command):
            scripts.remember(target, python)
    starts = [m.end() for m in _LIBRARY_CLI.finditer(command)]
    aliases = {m.group(1) for m in _ALIAS.finditer(command)}
    for alias in aliases:
        starts += [m.end() for m in re.finditer(rf'"?\$(?:\{{{alias}\}}|{alias}(?![A-Za-z0-9_]))"?', command)]
    for function in {m.group(1) for m in _FUNCTION_ALIAS.finditer(command)}:
        # Calls at a command position: line start or after ; & | ( { $( or a backtick.
        starts += [m.end() for m in re.finditer(rf"(?:^|(?<=[;&|({{`\n]))\s*{function}(?=\s)", command, re.M)]
    starts = sorted(set(starts))
    operations = []
    for position, start in enumerate(starts):
        stop = starts[position + 1] if position + 1 < len(starts) else len(command)
        segment = _cli_segment(command, start, stop)
        tokens = [t for t in _TOKEN_SPLIT.split(segment) if t]
        subcommand = next((t for t in tokens if t in LIBRARY_CLI_ALL_COMMANDS), None)
        if subcommand not in LIBRARY_CLI_WRITE_COMMANDS:
            continue
        if any(t in ("--schema", "--help", "-h") for t in tokens):
            continue
        after = segment[segment.find(subcommand) + len(subcommand):]
        paths = []
        path_arg = re.search(r"--path(?:\s+|=)(?:'([^']*)'|\"([^\"]*)\"|(\S+))", after)
        if path_arg:
            paths.append(next(g for g in path_arg.groups() if g is not None))
        paths += [p for p in (_vault_relative(m) for m in re.findall(r"/home/trace/projects/personal-records-vault/[^\s'\"]+", after)) if p]
        operation = {
            "via": "bash",
            "operation": subcommand,
            "applied": "--apply" in tokens if subcommand in LIBRARY_CLI_PREVIEW_DEFAULT else True,
            "note_ids": _ids(after),
            "vault_paths": sorted(set(paths)),
        }
        if not operation["note_ids"] and not operation["vault_paths"]:
            operation["snippet"] = _snippet(subcommand + after)
        operations.append(operation)
    return operations + python


def tool_library_writes(block, scripts=None):
    """Library writes implied by one assistant tool_use block (Bash CLI, library MCP or Vault Edit/Write)."""
    name = str(block.get("name", ""))
    data = block.get("input") if isinstance(block.get("input"), dict) else {}
    if name == "Bash":
        return bash_library_writes(data.get("command"), scripts)
    if name == "Write" and scripts is not None and str(data.get("file_path", "")).endswith(".py"):
        content = data.get("content") or ""
        if "markdown-library" in content:
            scripts.remember(data["file_path"], _python_library_writes(content))
    if name.startswith("mcp__"):
        short = name.rsplit("__", 1)[-1]
        tool = next((t for t in LIBRARY_MCP_WRITE_TOOLS if t in short), None)
        if not tool or data.get("schema"):
            return []
        dumped = json.dumps(data, ensure_ascii=False)
        operation = {"via": "mcp", "operation": tool, "tool": name,
                     "applied": bool(data.get("apply")) if tool == "organize_notes" else True,
                     "note_ids": _ids(dumped),
                     "vault_paths": [str(data["destination"])] if isinstance(data.get("destination"), str) else []}
        if not operation["note_ids"] and not operation["vault_paths"]:
            operation["snippet"] = _snippet(dumped)
        return [operation]
    if name in FILE_WRITE_TOOLS:
        relative = _vault_relative(data.get("file_path"))
        if relative is None:
            return []
        return [{"via": "file", "operation": name.lower(), "applied": True, "note_ids": [], "vault_paths": [relative]}]
    return []


def dictation_check(message):
    """Return (is_candidate, matched markers, counted chars) for one extracted message."""
    text = message["text"]
    own = "".join(text[a:b] for a, b in _outside(message["paste_spans"], len(text)))
    counted = len(re.sub(r"\s+", "", _URL.sub("", own)))
    experience = [m for m in DICTATION_EXPERIENCE_MARKERS if m in own]
    save = [m for m in DICTATION_SAVE_MARKERS if m in own]
    candidate = (counted >= DICTATION_MIN_CHARS and not message["is_button_reply"]
                 and not message["slash_command"] and bool(experience or save))
    return candidate, {"experience": experience, "save_intent": save}, counted, own


def _outside(spans, length):
    position = 0
    for span in spans:
        if span["start"] > position:
            yield position, span["start"]
        position = max(position, span["end"])
    if position < length:
        yield position, length


def _scan_file(path, stats, find_writes=False):
    """Return one session file's index, titles, candidate messages and heuristic library writes."""
    index = _Index()
    titles = {}
    candidates = []
    writes = []
    last_human = None
    scripts = ScriptRegistry()
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
                if find_writes:
                    for block in blocks:
                        if block.get("type") != "tool_use":
                            continue
                        for operation in tool_library_writes(block, scripts):
                            writes.append({"assistant_uuid": record.get("uuid"), "tool_use_id": block.get("id"),
                                           "parent": record.get("parentUuid"), "file_order_human": last_human,
                                           "timestamp": record.get("timestamp"), "operation": operation})
                continue
            if kind == "user":
                reason, _ = _classify_user(record)
                if reason:
                    index.add(record, "other")
                    candidates.append(("excluded", reason, record))
                    continue
                index.add(record, "human")
                last_human = record.get("uuid")
                candidates.append(("record", None, record))
                continue
            if kind == "attachment" and _queued_human(record):
                index.add(record, "human")
                last_human = record.get("uuid")
                candidates.append(("queued", None, record))
                continue
            index.add(record, "other")
    return index, titles, candidates, writes


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


def extract(directory, since=None, until=None, include_context=True, library_writes=False, dictation=False):
    """Scan top-level session files and return (messages, summary). Read only.

    ``library_writes`` adds ``library_writes_heuristic`` per message; ``dictation`` adds the
    deterministic ``dictation_candidate`` flag (see the DICTATION_* constants).
    """
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
    writes_by_human = {}
    seen_writes = set()
    for path in files:
        index, titles, candidates, writes = _scan_file(path, stats, library_writes)
        for write in writes:
            key = (write["assistant_uuid"], write["tool_use_id"], json.dumps(write["operation"], sort_keys=True))
            if key in seen_writes:
                continue
            seen_writes.add(key)
            owner = index.nearest_human(write["parent"])
            method = "parent_chain"
            if owner is None:
                owner, method = write["file_order_human"], "file_order"
            if owner is None:
                stats["library_writes_unattributed"] += 1
                continue
            moment = _parse_time(write["timestamp"])
            writes_by_human.setdefault(owner, []).append(dict(
                write["operation"], assistant_uuid=write["assistant_uuid"], attribution=method,
                timestamp_utc=_utc_text(moment) if moment else None))
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
            if library_writes:
                found = writes_by_human.get(uuid, [])
                message["library_writes_heuristic"] = found
                message["library_write_applied_heuristic"] = any(w["applied"] for w in found)
            if dictation:
                candidate, markers, counted, _ = dictation_check(message)
                message["dictation_candidate"] = candidate
                message["dictation_chars"] = counted
                message["dictation_markers"] = markers
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
    if library_writes:
        attributed = {m["message_uuid"] for m in result}
        summary["library_writes_heuristic"] = {
            "operations": sum(len(m["library_writes_heuristic"]) for m in result),
            "applied_operations": sum(w["applied"] for m in result for w in m["library_writes_heuristic"]),
            "messages_with_applied_write": sum(m["library_write_applied_heuristic"] for m in result),
            "messages_with_preview_only": sum(bool(m["library_writes_heuristic"]) and not m["library_write_applied_heuristic"]
                                              for m in result),
            "by_operation": dict(sorted(Counter(f'{w["via"]}:{w["operation"]}' for m in result
                                               for w in m["library_writes_heuristic"]).items())),
            "by_attribution": dict(Counter(w["attribution"] for m in result for w in m["library_writes_heuristic"])),
            "operations_outside_output": sum(len(v) for k, v in writes_by_human.items() if k not in attributed),
            "unattributed_operations": stats["library_writes_unattributed"],
        }
    if dictation:
        summary["dictation_candidates"] = sum(m["dictation_candidate"] for m in result)
    return result, summary


def uncovered_dictation(messages):
    """Dictation candidates with no applied library write afterwards, grouped by (session, Shanghai date)."""
    groups = {}
    for message in messages:
        if not message["dictation_candidate"] or message["library_write_applied_heuristic"]:
            continue
        _, _, counted, own = dictation_check(message)
        key = (message["session_id"], message["date_shanghai"])
        group = groups.setdefault(key, {"session_id": key[0], "session_title": message["session_title"],
                                        "date_shanghai": key[1], "messages": []})
        group["messages"].append({"message_uuid": message["message_uuid"], "date_shanghai": message["date_shanghai"],
                                  "chars": counted,
                                  "preview": re.sub(r"\s+", " ", own).strip()[:DICTATION_PREVIEW_CHARS],
                                  "preview_only_writes": bool(message["library_writes_heuristic"])})
    ordered = sorted(groups.values(), key=lambda g: (g["date_shanghai"], g["session_id"]))
    for group in ordered:
        group["count"] = len(group["messages"])
    return ordered


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


def run(directory, out, output_format="jsonl", since=None, until=None, segments_out=None, include_context=True,
        library_writes=False, dictation=False, uncovered_out=None):
    targets = [Path(out)] + ([Path(segments_out)] if segments_out else []) + ([Path(uncovered_out)] if uncovered_out else [])
    if len({t.resolve() for t in targets}) != len(targets):
        raise ValueError("Messages, segments and uncovered-dictation outputs must be different files")
    root = Path(directory).resolve()
    for target in targets:
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"Refusing to overwrite existing file: {target}")
        if root == target.resolve().parent:
            raise ValueError("Write outputs outside the scanned session directory")
    start, end = parse_bound(since), parse_bound(until, upper=True)
    if start and end and start >= end:
        raise ValueError("--since must be earlier than --until")
    messages, summary = extract(directory, start, end, include_context, library_writes or bool(uncovered_out),
                                dictation or bool(uncovered_out))
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
    if uncovered_out:
        grouped = uncovered_dictation(messages)
        rule = {"min_chars_without_pastes_or_whitespace": DICTATION_MIN_CHARS,
                "excludes": ["button_reply", "slash_command"],
                "experience_markers": list(DICTATION_EXPERIENCE_MARKERS), "save_markers": list(DICTATION_SAVE_MARKERS),
                "covered_means": "an applied library write heuristically attributed to the message; previews do not count"}
        write_new(targets[-1], (json.dumps({"timezone": "Asia/Shanghai", "since_utc": summary["since_utc"],
                                            "until_utc": summary["until_utc"], "heuristic": True, "rule": rule,
                                            "messages": sum(g["count"] for g in grouped), "groups": grouped},
                                           ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        summary["uncovered_dictation"] = sum(g["count"] for g in grouped)
        summary["uncovered_dictation_out"] = str(targets[-1])
    return summary
