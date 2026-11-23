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


class ClaudeCodeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
