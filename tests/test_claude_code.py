import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chatgpt_pipeline import claude_code
from chatgpt_pipeline.cli import main

SESSION = "00000000-0000-4000-8000-00000000000a"
FORK = "00000000-0000-4000-8000-00000000000b"
AGENT = "00000000-0000-4000-8000-00000000000c"


def human(uuid, parent, text, at, session=SESSION, **extra):
    return {"type": "user", "uuid": uuid, "parentUuid": parent, "sessionId": session, "timestamp": at,
            "cwd": "/synthetic/project", "origin": {"kind": "human"}, "turnOrigin": "human",
            "message": {"role": "user", "content": text}, **extra}


def assistant(uuid, parent, text=None, labels=None, session=SESSION, at="2026-01-01T00:00:00Z"):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if labels is not None:
        content.append({"type": "tool_use", "id": "t-" + uuid, "name": "mcp__ccd_turn__wrap_up",
                        "input": {"recap": "Synthetic recap.", "actions": [{"label": l} for l in labels]}})
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent, "sessionId": session, "timestamp": at,
            "message": {"role": "assistant", "content": content}}


def other_user(uuid, parent, content, at="2026-01-01T00:00:00Z", session=SESSION, **extra):
    return {"type": "user", "uuid": uuid, "parentUuid": parent, "sessionId": session, "timestamp": at,
            "message": {"role": "user", "content": content}, **extra}


def queued(uuid, parent, prompt, at, origin=None, human_turn=True, session=SESSION):
    return {"type": "attachment", "uuid": uuid, "parentUuid": parent, "sessionId": session, "timestamp": at,
            "cwd": "/synthetic/project",
            "attachment": {"type": "queued_command", "prompt": prompt, "commandMode": "prompt",
                           "origin": origin or {"kind": "human"}, "humanTurn": human_turn,
                           "source_uuid": "src-" + uuid, "delivery_id": "d-" + uuid, "timestamp": at}}


def base_session():
    return [
        {"type": "queue-operation", "operation": "enqueue", "sessionId": SESSION, "timestamp": "2026-01-01T00:00:00Z"},
        {"type": "custom-title", "customTitle": "Synthetic title", "sessionId": SESSION},
        human("h1", None, "Plan the synthetic garden.", "2026-01-01T15:00:00Z"),
        assistant("a1", "h1", "Here is a first idea about tomatoes."),
        assistant("a2", "a1", "Final answer ends with the word basil.", labels=["Go ahead", "Not now"]),
        other_user("r1", "a2", [{"type": "tool_result", "tool_use_id": "t-a2", "content": "ok"}]),
        human("h2", "r1", "Go ahead", "2026-01-01T15:05:00Z"),
        assistant("a3", "h2", "Working on it."),
        queued("q1", "a3", "Also add carrots please.", "2026-01-01T15:06:00Z"),
        queued("q2", "a3", "peer forwarded text", "2026-01-01T15:06:30Z", origin={"kind": "peer", "from": "x"}),
        queued("q3", "a3", "not a human turn", "2026-01-01T15:06:40Z", human_turn=None),
        other_user("tn", "a3", "<task-notification>\nfinished\n</task-notification>",
                   origin={"kind": "task-notification", "producer": "session-task"}),
        other_user("pr", "a3", "peer says hello", origin={"kind": "peer", "from": "agent", "body": "hi"}),
        other_user("cs", "a3", "Summary of the earlier conversation.", isCompactSummary=True),
        other_user("sk", "a3", [{"type": "text", "text": "Base directory for this skill: synthetic"}], isMeta=True),
        other_user("hi", "a3", "<system-reminder>\nHost note.\n</system-reminder>"),
        other_user("ir", "a3", [{"type": "text", "text": "[Request interrupted by user]"}]),
        other_user("lc", "a3", "<local-command-stdout>done</local-command-stdout>"),
        other_user("sc", "a3", "sidechain text", origin={"kind": "human"}, isSidechain=True),
        human("h3", "a3", "<system-reminder>\nInjected context.\n</system-reminder>\nWater twice a week.",
              "2026-01-01T16:30:00Z"),
        human("h4", "h3", 'Look at this: <pasted_content id="p">copied log line</pasted_content id="p">\n',
              "2026-01-02T01:00:00Z"),
        human("h5", "h4", [{"type": "text", "text": "What plant is this?"},
                            {"type": "image", "source": {"type": "base64", "data": "AA=="}}],
              "2026-01-02T02:00:00Z"),
        human("h6", "h5", "<command-message>demo-skill</command-message>\n<command-name>/demo-skill</command-name>\n"
              "<command-args>check soil</command-args>", "2026-01-02T03:00:00Z"),
        human("h7", "h6", "<system-reminder>\nOnly a reminder.\n</system-reminder>", "2026-01-02T03:10:00Z"),
    ]


class SessionDirTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.sessions = self.base / "sessions"
        self.sessions.mkdir()
        self.out = self.base / "out"

    def write(self, name, records, trailing=""):
        path = self.sessions / f"{name}.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records) + trailing, encoding="utf-8")
        return path

    def extract(self, **kwargs):
        return claude_code.extract(self.sessions, **kwargs)

    def by_uuid(self, messages):
        return {m["message_uuid"]: m for m in messages}


class ClaudeCodeTests(SessionDirTest):
    def test_only_human_records_and_queued_messages_are_kept(self):
        self.write(SESSION, base_session())
        messages, summary = self.extract()
        found = self.by_uuid(messages)
        self.assertEqual(list(found), ["h1", "h2", "q1", "h3", "h4", "h5", "h6"])
        self.assertEqual(found["q1"]["kind"], "queued")
        self.assertEqual(found["h1"]["kind"], "record")
        excluded = summary["excluded_unique_user_records"]
        for reason in ("tool_result", "task_notification", "peer", "compact_summary", "meta_or_skill_injection",
                       "host_injected", "interrupt", "local_command", "sidechain", "empty_after_cleaning"):
            self.assertEqual(excluded.get(reason), 1, reason)
        self.assertEqual(summary["by_kind"], {"record": 6, "queued": 1})

    def test_fields_reminder_strip_paste_image_slash_and_context(self):
        self.write(SESSION, base_session())
        found = self.by_uuid(self.extract()[0])
        first = found["h1"]
        self.assertEqual(first["source"], "claude-code")
        self.assertEqual(first["source_id"], f"claude-code:{SESSION}:h1")
        self.assertEqual(first["session_title"], "Synthetic title")
        self.assertEqual(first["cwd"], "/synthetic/project")
        self.assertEqual(first["timestamp_utc"], "2026-01-01T15:00:00.000Z")
        self.assertEqual(first["date_shanghai"], "2026-01-01")
        self.assertIsNone(first["previous_assistant_tail"])
        self.assertEqual(found["h2"]["previous_assistant_tail"], "Final answer ends with the word basil.")
        self.assertEqual(found["h3"]["text"], "Water twice a week.")
        self.assertEqual(found["h3"]["date_shanghai"], "2026-01-02")
        import hashlib
        self.assertEqual(found["h3"]["text_sha256"], hashlib.sha256(b"Water twice a week.").hexdigest())
        paste = found["h4"]
        self.assertTrue(paste["is_paste"])
        span = paste["paste_spans"][0]
        self.assertTrue(span["closed"])
        self.assertTrue(paste["text"][span["start"]:span["end"]].startswith("<pasted_content"))
        self.assertTrue(paste["text"][span["start"]:span["end"]].endswith('</pasted_content id="p">'))
        self.assertEqual(paste["own_chars"], len("Look at this: "))
        self.assertFalse(found["h1"]["is_paste"])
        self.assertTrue(found["h5"]["has_image"])
        self.assertFalse(found["h1"]["has_image"])
        self.assertEqual(found["h6"]["text"], "/demo-skill check soil")
        self.assertEqual(found["h6"]["slash_command"], "/demo-skill")

    def test_button_reply_requires_exact_label_from_same_turn(self):
        records = base_session()
        records += [
            assistant("a9", "h6", "Choose.", labels=["Ship it"]),
            human("h8", "a9", "Ship it but rename the file first", "2026-01-02T04:00:00Z"),
            assistant("a10", "h8", "Done."),
            human("h9", "a10", "Ship it", "2026-01-02T05:00:00Z"),
        ]
        self.write(SESSION, records)
        found = self.by_uuid(self.extract()[0])
        self.assertTrue(found["h2"]["is_button_reply"])
        self.assertFalse(found["h1"]["is_button_reply"])
        self.assertFalse(found["h8"]["is_button_reply"])
        self.assertFalse(found["h9"]["is_button_reply"], "labels from an earlier turn must not match")

    def test_fork_copies_dedupe_by_uuid_and_keep_record_session(self):
        original = base_session()
        self.write(SESSION, original)
        copied = [dict(r) for r in original if r.get("type") != "custom-title"]
        copied.append({"type": "custom-title", "customTitle": "Fork copy title", "sessionId": SESSION})
        copied.append({"type": "custom-title", "customTitle": "Fork title", "sessionId": FORK})
        copied += [assistant("fa", "h6", "Forked reply.", session=FORK),
                   human("fh", "fa", "New fork message.", "2026-01-03T00:00:00Z", session=FORK)]
        self.write(FORK, copied)
        messages, summary = self.extract()
        uuids = [m["message_uuid"] for m in messages]
        self.assertEqual(len(uuids), len(set(uuids)))
        self.assertEqual(len(messages), 8)
        found = self.by_uuid(messages)
        self.assertEqual(found["h1"]["session_id"], SESSION)
        self.assertEqual(found["h1"]["session_title"], "Synthetic title", "owner file title wins over fork copy")
        self.assertEqual(found["fh"]["session_id"], FORK)
        self.assertEqual(found["fh"]["session_title"], "Fork title")
        self.assertGreater(summary["duplicate_copies_skipped"], 0)

    def test_agent_started_session_subagents_and_malformed_tail_are_skipped(self):
        self.write(AGENT, [
            other_user("ag1", None, "Instructions written by another agent.", turnOrigin="host_synthetic"),
            assistant("ag2", "ag1", "Ok."),
            other_user("ag3", "ag2", "<system-reminder>\nx\n</system-reminder>\nMore agent text.", turnOrigin="host_synthetic"),
            other_user("ag4", "ag3", "Relayed report.", isMeta=True, origin={"kind": "peer", "from": "agent"}),
        ], trailing='{"type": "user", "uuid": "partial')
        nested = self.sessions / AGENT / "subagents"
        nested.mkdir(parents=True)
        (nested / "agent-x.jsonl").write_text(json.dumps(human("sub", None, "subagent prompt", "2026-01-01T00:00:00Z")) + "\n")
        messages, summary = self.extract()
        self.assertEqual(messages, [])
        self.assertEqual(summary["excluded_unique_user_records"], {"host_synthetic_or_agent_start": 2, "peer": 1})
        self.assertEqual(summary["malformed_lines"], 1)
        self.assertEqual(summary["files_scanned"], 1)

    def test_unclosed_paste_ends_at_next_paste(self):
        text = '<pasted_content id="p">one</pasted_content id="p"> mid <pasted_content id="p">two <pasted_content id="p">three</pasted_content id="p"> end'
        spans = claude_code.paste_spans(text)
        self.assertEqual([s["closed"] for s in spans], [True, False, True])
        self.assertEqual(text[spans[1]["start"]:spans[1]["end"]], '<pasted_content id="p">two ')
        self.assertTrue(text.endswith(" end") and spans[2]["end"] == len(text) - 4)

    def test_time_bounds_use_shanghai_days(self):
        self.write(SESSION, base_session())
        since = claude_code.parse_bound("2026-01-02")
        until = claude_code.parse_bound("2026-01-02", upper=True)
        messages, summary = self.extract(since=since, until=until)
        self.assertEqual([m["message_uuid"] for m in messages], ["h3", "h4", "h5", "h6"])
        self.assertEqual(summary["messages_before_time_filter"], 7)
        aware = claude_code.parse_bound("2026-01-01T15:05:00Z")
        self.assertEqual(aware.isoformat(), "2026-01-01T15:05:00+00:00")
        naive = claude_code.parse_bound("2026-01-01T23:05:00")
        self.assertEqual(naive, aware)
        with self.assertRaises(ValueError):
            claude_code.parse_bound("yesterday")

    def test_cli_writes_new_files_segments_and_refuses_overwrite(self):
        self.write(SESSION, base_session())
        out = self.out / "messages.jsonl"
        segments = self.out / "segments.json"
        argv = ["chatgpt-pipeline", "claude-code-messages", "--dir", str(self.sessions), "--out", str(out),
                "--segments", str(segments), "--no-context"]
        stdout = io.StringIO()
        with patch("sys.argv", argv), patch("sys.stdout", stdout):
            main()
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["messages"], 7)
        lines = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 7)
        self.assertNotIn("previous_assistant_tail", lines[0])
        grouped = json.loads(segments.read_text(encoding="utf-8"))["segments"]
        self.assertEqual([(g["date_shanghai"], g["messages"]) for g in grouped], [("2026-01-01", 3), ("2026-01-02", 4)])
        self.assertEqual(grouped[0]["queued"], 1)
        self.assertEqual(grouped[0]["button_replies"], 1)
        self.assertEqual(grouped[1]["paste_messages"], 1)
        before = out.read_bytes()
        with patch("sys.argv", argv), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            main()
        self.assertEqual(out.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["messages.jsonl", "segments.json"])

    def test_json_format_and_inputs_are_untouched(self):
        path = self.write(SESSION, base_session())
        original = path.read_bytes()
        out = self.out / "messages.json"
        summary = claude_code.run(self.sessions, out, output_format="json", since="2026-01-01", until="2026-01-01")
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(data["messages"]), 3)
        self.assertEqual(data["summary"]["messages"], summary["messages"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.sessions.iterdir()), [f"{SESSION}.jsonl"])
        with self.assertRaises(ValueError):
            claude_code.run(self.sessions, self.sessions / "inside.jsonl")
        with self.assertRaises(ValueError):
            claude_code.run(self.sessions, self.out / "a.jsonl", since="2026-01-03", until="2026-01-01")


VAULT = claude_code.VAULT_ROOT
NOTE_A = "11111111-2222-4333-8444-555555555555"
NOTE_B = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
LONG_EXPERIENCE = "我小学的时候第一次玩这个游戏，" + "一直记得那种在夜里偷偷开机的紧张感觉和后来通关的满足感。" * 2


def tool_call(uuid, parent, name, data, at="2026-02-01T00:00:00Z", session=SESSION):
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent, "sessionId": session, "timestamp": at,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tu-" + uuid, "name": name,
                                                          "input": data}]}}


def ops(found):
    return [(o["via"], o["operation"], o["applied"]) for o in found]


class LibraryWriteHeuristicTests(SessionDirTest):
    def test_bash_cli_calls_aliases_and_previews(self):
        cli = "cd /synthetic/workflow && .venv/bin/markdown-library"
        found = claude_code.bash_library_writes(
            f"{cli} --vault {VAULT} create --title 'T' --path '30_日记/a b.md' - <<'EOF'\nbody\nEOF\necho done")
        self.assertEqual(ops(found), [("bash", "create", True)])
        self.assertEqual(found[0]["vault_paths"], ["30_日记/a b.md"])
        found = claude_code.bash_library_writes(
            f'M=".venv/bin/markdown-library --vault {VAULT}"; $M get {NOTE_A} | head; '
            f"$M edit {NOTE_A} --request /tmp/r.json --apply; $M organize --request /tmp/o.json")
        self.assertEqual(ops(found), [("bash", "edit", True), ("bash", "organize", False)])
        self.assertEqual(found[0]["note_ids"], [NOTE_A])
        self.assertIn("snippet", found[1])
        self.assertLessEqual(len(found[1]["snippet"]), claude_code.WRITE_SNIPPET_CHARS)
        found = claude_code.bash_library_writes(
            f"{cli} --vault {VAULT} organize --request - --apply --expected-token t <<'EOF'\n"
            f'{{"moves": [{{"id": "{NOTE_B}"}}]}}\nEOF')
        self.assertEqual(ops(found), [("bash", "organize", True)])
        self.assertEqual(found[0]["note_ids"], [NOTE_B])
        found = claude_code.bash_library_writes(
            f'ml() {{ .venv/bin/markdown-library --vault "{VAULT}" "$@"; }}\nml get {NOTE_A}\n'
            f"ml replace --expected-hash h {NOTE_A} /tmp/x.md && ml --json import --title T --source-id s f.md")
        self.assertEqual(ops(found), [("bash", "replace", True), ("bash", "import", True)])
        for command in (f"{cli} --vault {VAULT} organize --schema", f"{cli} --vault {VAULT} create --help",
                        f"{cli} --vault {VAULT} get {NOTE_A}", "grep -rn create src/markdown_library/core.py",
                        "sed -n 1,5p skills/markdown-library/SKILL.md; ls .markdown-library",
                        "systemctl --user status markdown-library-http.service", "xml() { echo; }; xml replace"):
            self.assertEqual(claude_code.bash_library_writes(command), [], command)

    def test_python_wrapper_and_helper_scripts(self):
        scripts = claude_code.ScriptRegistry()
        helper = ("cat > $W/fix.py <<'EOF'\nimport subprocess\n"
                  f"ML = ['.venv/bin/markdown-library', '--vault', '{VAULT}']\n"
                  "def ml(*a): return subprocess.run(ML + list(a))\n"
                  "def save(i): ml('get', i); ml('replace', i, 'f.md', '--expected-hash', 'h')\n"
                  f"save('{NOTE_A}')\nEOF\n.venv/bin/python $W/fix.py")
        found = claude_code.bash_library_writes(helper, scripts)
        self.assertEqual(ops(found), [("python", "replace", True)])
        self.assertEqual(found[0]["note_ids"], [NOTE_A])
        derived = (f"sed -n '1,4p' $W/fix.py > $W/more.py && cat >> $W/more.py <<'EOF'\nsave('{NOTE_B}')\nEOF\n"
                   ".venv/bin/python $W/more.py")
        found = claude_code.bash_library_writes(derived, scripts)
        self.assertEqual(ops(found), [("script", "replace", True)])
        self.assertEqual(found[0]["note_ids"], [NOTE_B])
        self.assertEqual(ops(claude_code.bash_library_writes(".venv/bin/python $W/more.py", scripts)),
                         [("script", "replace", True)])
        self.assertEqual(claude_code.bash_library_writes("cat $W/more.py", scripts), [])
        self.assertEqual(claude_code.bash_library_writes(".venv/bin/python other.py", scripts), [])

    def test_mcp_tool_names(self):
        block = {"type": "tool_use", "name": "mcp__abc__create_note",
                 "input": {"title": "T", "markdown": "body", "source_id": "s1"}}
        found = claude_code.tool_library_writes(block)
        self.assertEqual([(o["via"], o["operation"], o["tool"]) for o in found], [("mcp", "create_note", "mcp__abc__create_note")])
        self.assertIn("snippet", found[0])
        found = claude_code.tool_library_writes({"type": "tool_use", "name": "mcp__abc__edit_note",
                                                 "input": {"note_id": NOTE_A, "edits": [], "expected_hash": "h"}})
        self.assertEqual(found[0]["note_ids"], [NOTE_A])
        found = claude_code.tool_library_writes({"type": "tool_use", "name": "mcp__abc__move_note",
                                                 "input": {"note_id": NOTE_A, "destination": "20_项目", "expected_hash": "h"}})
        self.assertEqual(found[0]["vault_paths"], ["20_项目"])
        found = claude_code.tool_library_writes({"type": "tool_use", "name": "mcp__abc__merge_notes",
                                                 "input": {"target_id": NOTE_A, "source_ids": [NOTE_B], "markdown": "m",
                                                           "expected_hashes": {}}})
        self.assertEqual(found[0]["note_ids"], sorted([NOTE_A, NOTE_B]))
        preview = claude_code.tool_library_writes({"type": "tool_use", "name": "mcp__abc__organize_notes",
                                                   "input": {"request": {"moves": []}}})
        self.assertEqual(ops(preview), [("mcp", "organize_notes", False)])
        for name, data in (("mcp__abc__organize_notes", {"schema": True}), ("mcp__abc__search_notes", {"query": "x"}),
                           ("mcp__abc__get_note", {"note_id": NOTE_A})):
            self.assertEqual(claude_code.tool_library_writes({"type": "tool_use", "name": name, "input": data}), [])

    def test_vault_edit_and_write_paths(self):
        inside = claude_code.tool_library_writes({"type": "tool_use", "name": "Edit", "input": {
            "file_path": f"{VAULT}/10_个人记录/日记.md", "old_string": "a", "new_string": "b"}})
        self.assertEqual(inside, [{"via": "file", "operation": "edit", "applied": True, "note_ids": [],
                                   "vault_paths": ["10_个人记录/日记.md"]}])
        written = claude_code.tool_library_writes({"type": "tool_use", "name": "Write", "input": {
            "file_path": f"{VAULT}/05_Inbox/新.md", "content": "x"}})
        self.assertEqual(written[0]["vault_paths"], ["05_Inbox/新.md"])
        for path in (f"{VAULT}-backup/a.md", "/synthetic/other/a.md", VAULT):
            self.assertEqual(claude_code.tool_library_writes({"type": "tool_use", "name": "Write",
                                                              "input": {"file_path": path, "content": "x"}}), [], path)

    def test_next_human_message_is_the_boundary(self):
        cli = f".venv/bin/markdown-library --vault {VAULT}"
        records = [
            human("w1", None, LONG_EXPERIENCE, "2026-02-01T01:00:00Z"),
            tool_call("t1", "w1", "Bash", {"command": f"{cli} create --title T --path 10_个人记录/x.md /tmp/x.md"}),
            other_user("r1", "t1", [{"type": "tool_result", "tool_use_id": "tu-t1", "content": "ok"}]),
            tool_call("t2", "r1", "Edit", {"file_path": f"{VAULT}/10_个人记录/x.md", "old_string": "a", "new_string": "b"}),
            assistant("a1", "t2", "Saved."),
            human("w2", "a1", "Thanks, now look at the build logs please.", "2026-02-01T02:00:00Z"),
            tool_call("t3", "w2", "Bash", {"command": "ls /synthetic"}),
            queued("w3", "t3", "Also fix the typo in that note.", "2026-02-01T02:01:00Z"),
            tool_call("t4", "w3", "mcp__lib__replace_note", {"note_id": NOTE_A, "markdown": "m", "expected_hash": "h"}),
            # After a compaction the chain breaks; file order attributes the call to the latest human message.
            {"type": "system", "subtype": "compact_boundary", "uuid": "cb", "parentUuid": None, "sessionId": SESSION},
            tool_call("t5", "cb", "mcp__lib__edit_note", {"note_id": NOTE_B, "edits": [], "expected_hash": "h"}),
            human("w4", "t5", LONG_EXPERIENCE, "2026-02-02T03:00:00Z"),
            tool_call("t6", "w4", "Bash", {"command": f"{cli} organize --request /tmp/o.json"}),
        ]
        self.write(SESSION, records)
        found = self.by_uuid(self.extract(library_writes=True)[0])
        self.assertEqual([(o["via"], o["operation"]) for o in found["w1"]["library_writes_heuristic"]],
                         [("bash", "create"), ("file", "edit")])
        self.assertEqual(found["w1"]["library_writes_heuristic"][0]["vault_paths"], ["10_个人记录/x.md"])
        self.assertTrue(found["w1"]["library_write_applied_heuristic"])
        self.assertEqual(found["w2"]["library_writes_heuristic"], [])
        self.assertEqual([(o["operation"], o["attribution"]) for o in found["w3"]["library_writes_heuristic"]],
                         [("replace_note", "parent_chain"), ("edit_note", "file_order")])
        self.assertEqual(ops(found["w4"]["library_writes_heuristic"]), [("bash", "organize", False)])
        self.assertFalse(found["w4"]["library_write_applied_heuristic"])

    def test_fork_copies_do_not_double_count_writes(self):
        records = [human("w1", None, "Save it.", "2026-02-01T01:00:00Z"),
                   tool_call("t1", "w1", "Write", {"file_path": f"{VAULT}/a.md", "content": "x"})]
        self.write(SESSION, records)
        self.write(FORK, records)
        messages, summary = self.extract(library_writes=True)
        self.assertEqual(len(messages[0]["library_writes_heuristic"]), 1)
        self.assertEqual(summary["library_writes_heuristic"]["operations"], 1)


class DictationCandidateTests(SessionDirTest):
    def message(self, text, **overrides):
        cleaned, spans, slash = claude_code.clean_text(text)
        base = {"text": cleaned, "paste_spans": spans, "is_button_reply": False, "slash_command": slash}
        return dict(base, **overrides)

    def test_rule(self):
        check = lambda text, **o: claude_code.dictation_check(self.message(text, **o))[0]
        self.assertTrue(check(LONG_EXPERIENCE))
        self.assertFalse(check("我小学的时候第一次玩这个游戏。"), "too short")
        plain = "请把部署脚本里的端口改成配置项，然后补充单元测试，再检查一遍日志输出格式是否一致，最后运行全部测试并汇报结果给我看一下。" * 2
        self.assertFalse(check(plain.replace("补充", "加上")), "no marker")
        self.assertTrue(check(plain), "save-intent marker 补充")
        self.assertFalse(check(LONG_EXPERIENCE, is_button_reply=True))
        self.assertFalse(check(f"<command-message>x</command-message>\n<command-name>/x</command-name>\n"
                               f"<command-args>{LONG_EXPERIENCE}</command-args>"))
        self.assertFalse(check(f'记下：<pasted_content id="p">{LONG_EXPERIENCE}</pasted_content id="p">'),
                         "pasted text does not count")
        self.assertFalse(check("记下这些链接 " + " ".join(f"https://example.com/{i}/abcdefghij" for i in range(5))),
                         "URLs do not count")
        candidate, markers, counted, _ = claude_code.dictation_check(self.message(LONG_EXPERIENCE))
        self.assertGreaterEqual(counted, claude_code.DICTATION_MIN_CHARS)
        self.assertIn("小学", markers["experience"])

    def test_cli_uncovered_output(self):
        cli = f".venv/bin/markdown-library --vault {VAULT}"
        self.write(SESSION, [
            human("d1", None, LONG_EXPERIENCE, "2026-02-01T01:00:00Z"),
            tool_call("t1", "d1", "Bash", {"command": f"{cli} create --title T /tmp/x.md"}),
            human("d2", "t1", "第二段：" + LONG_EXPERIENCE, "2026-02-01T17:00:00Z"),
            tool_call("t2", "d2", "Bash", {"command": f"{cli} edit {NOTE_A} --request /tmp/r.json"}),
            human("d3", "t2", "ok", "2026-02-01T17:05:00Z"),
        ])
        out = self.out / "m.jsonl"
        uncovered = self.out / "uncovered.json"
        argv = ["chatgpt-pipeline", "claude-code-messages", "--dir", str(self.sessions), "--out", str(out),
                "--library-writes", "--dictation", "--uncovered-dictation", str(uncovered)]
        stdout = io.StringIO()
        with patch("sys.argv", argv), patch("sys.stdout", stdout):
            main()
        summary = json.loads(stdout.getvalue())
        self.assertEqual(summary["dictation_candidates"], 2)
        self.assertEqual(summary["uncovered_dictation"], 1)
        lines = {m["message_uuid"]: m for m in map(json.loads, out.read_text(encoding="utf-8").splitlines())}
        self.assertTrue(lines["d1"]["dictation_candidate"] and lines["d1"]["library_write_applied_heuristic"])
        self.assertFalse(lines["d3"]["dictation_candidate"])
        data = json.loads(uncovered.read_text(encoding="utf-8"))
        self.assertTrue(data["heuristic"])
        self.assertEqual(data["rule"]["min_chars_without_pastes_or_whitespace"], claude_code.DICTATION_MIN_CHARS)
        [group] = data["groups"]
        self.assertEqual((group["session_id"], group["date_shanghai"], group["count"]), (SESSION, "2026-02-02", 1))
        [item] = group["messages"]
        self.assertEqual(set(item), {"message_uuid", "date_shanghai", "chars", "preview", "preview_only_writes"})
        self.assertEqual(item["message_uuid"], "d2")
        self.assertTrue(item["preview_only_writes"])
        self.assertLessEqual(len(item["preview"]), claude_code.DICTATION_PREVIEW_CHARS)
        self.assertEqual(uncovered.stat().st_mode & 0o777, 0o600)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
