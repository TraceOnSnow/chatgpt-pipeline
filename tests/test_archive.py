"""Synthetic-only tests for read-only archive lookup and covered-source evidence."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from chatgpt_pipeline import bulk, cli
from chatgpt_pipeline.archive import covered_evidence, read, read_markdown, search, search_markdown
from chatgpt_pipeline.normalize import normalize
from chatgpt_pipeline.verify import sha
from test_incremental import message, publish, tree_digest

# 1970-01-02 00:00 UTC = 86400; 1970-01-03 00:00 UTC = 172800.
ARCHIVE = {
    "conv-voice-0001": [
        message("v1", "user", "synthetic greeting", 90000, {"voice_mode_message": True}, voice=True),
        message("v2", "assistant", "synthetic hello back " + "x" * 400, 90001),
        message("v3", "user", "I want to record the lantern plan", 90010, {"voice_mode_message": True}, voice=True),
        message("v4", "assistant", "noted", 90011),
    ],
    "conv-typed-0002": [
        message("t1", "user", "typed note about Lantern budgets", 180000),
        message("t2", "assistant", "the lantern answer only lives here", 180001),
    ],
    "conv-other-0003": [
        message("o1", "user", "unrelated synthetic chat", 95000, {"dictation": True}),
        message("o2", "assistant", "lantern mentioned by assistant only", 95001),
    ],
}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "exports" / "account-x"
        self.root.mkdir(parents=True)
        self.commit, _ = publish(self.root, ARCHIVE)

    def ids(self, result):
        return [row["conversation_id"] for row in result["conversations"]]

    def test_keyword_matches_own_text_not_assistant_or_title_only(self):
        before = tree_digest(self.root)
        result = search(self.root, keywords=["LANTERN"])
        self.assertEqual(tree_digest(self.root), before)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(self.ids(result), ["conv-typed-0002", "conv-voice-0001"])
        voice = {r["conversation_id"]: r for r in result["conversations"]}["conv-voice-0001"]
        self.assertEqual(voice["user_messages"], 2)
        self.assertEqual(voice["voice_messages"], 2)
        self.assertEqual(voice["hits"][0]["message_id"], "v3")
        self.assertIn("lantern", voice["hits"][0]["snippet"])
        titled = search(self.root, keywords=["title conv-other"])
        self.assertEqual(self.ids(titled), ["conv-other-0003"])
        self.assertEqual(titled["conversations"][0]["title_keyword_hits"], ["title conv-other"])
        self.assertEqual(search(self.root, keywords=["lantern", "budgets"])["matched"], 1)
        self.assertEqual(search(self.root, keywords=["lantern", "greeting"], any_keyword=True)["matched"], 2)

    def test_date_range_voice_and_id_filters(self):
        day_one = search(self.root, since="1970-01-02", until="1970-01-02")
        self.assertEqual(set(self.ids(day_one)), {"conv-voice-0001", "conv-other-0003"})
        self.assertEqual(self.ids(search(self.root, since="1970-01-03")), ["conv-typed-0002"])
        # A plain date follows --tz: in UTC-8 the 1970-01-02 local day is [115200, 201600).
        shifted = search(self.root, since="1970-01-02", until="1970-01-02", tz="America/Los_Angeles")
        self.assertEqual(self.ids(shifted), ["conv-typed-0002"])
        self.assertEqual(self.ids(search(self.root, voice=True)), ["conv-voice-0001"])
        self.assertNotIn("conv-voice-0001", self.ids(search(self.root, voice=False)))
        self.assertEqual(self.ids(search(self.root, conversation_ids=["conv-typ"[:8] + "ed-0"])), ["conv-typed-0002"])
        with self.assertRaisesRegex(ValueError, "timezone"):
            search(self.root, since="1970-01-02T00:00:00")
        with self.assertRaisesRegex(ValueError, "Unknown conversation"):
            search(self.root, conversation_ids=["missing-conversation"])

    def test_snippets_are_truncated(self):
        long_text = "a" * 300 + " lantern " + "b" * 300
        archive = {"conv-long-0001": [message("l1", "user", long_text, 100)]}
        root = self.base / "exports" / "account-y"
        root.mkdir()
        publish(root, archive)
        hit = search(root, keywords=["lantern"], snippet_chars=40)["conversations"][0]["hits"][0]
        self.assertLessEqual(len(hit["snippet"]), 42)
        self.assertIn("lantern", hit["snippet"])

    def test_read_returns_own_messages_with_pipeline_text_sha(self):
        result = read(self.root, conversation="conv-voice-0001", context=True, context_chars=50)
        self.assertEqual([m["id"] for m in result["messages"]], ["v1", "v3"])
        self.assertEqual(result["source"]["commit_sha256"], self.commit.name.split(".")[0])
        first, second = result["messages"]
        self.assertIsNone(first["previous_assistant"])
        self.assertEqual(second["previous_assistant"]["id"], "v2")
        self.assertTrue(second["previous_assistant"]["truncated"])
        self.assertLessEqual(len(second["previous_assistant"]["text"]), 51)
        self.assertTrue(second["voice"])
        self.assertEqual(second["created_utc"], "1970-01-02T01:00:10+00:00")
        # Same digest the selection stage stores in job input references (bulk.py).
        body = json.loads((self.root / result["source"]["object_path"]).read_bytes())
        normalized = {m["id"]: m for m in normalize(body)["analysis_messages"]}
        self.assertEqual(second["text_sha256"], sha(normalized["v3"]["text"].encode()))
        refs, _ = bulk.build_request([], normalize(body)["analysis_messages"])
        stored = {r["source_message_id"]: r["text_sha256"] for r in refs}
        self.assertEqual(stored["v3"], second["text_sha256"])
        plain = read(self.root, conversation="conv-voice-0001")
        self.assertNotIn("previous_assistant", plain["messages"][0])
        self.assertIn("## `v3`", read_markdown(result))
        self.assertIn("conv-voice-0001", search_markdown(search(self.root)))

    def test_covered_evidence_matches_cover_source_contract(self):
        record = covered_evidence(self.root, conversation="conv-voice-0001", decision="synthetic decision")
        self.assertEqual(record["type"], "chatgpt-source-first-covered-v1")
        self.assertEqual(record["account_key"], "account-x")
        self.assertEqual([e["source_message_id"] for e in record["evidence"]], ["v1", "v3"])
        self.assertEqual(set(record["evidence"][0]), {"source_message_id", "role", "text_sha256"})
        self.assertEqual(record["evidence"][1]["text_sha256"], sha("I want to record the lantern plan".encode()))
        self.assertEqual(record["decision"], "synthetic decision")
        self.assertEqual(record["input_source"]["conversation_id"], "conv-voice-0001")
        self.assertEqual(record["input_source"]["selection"], "all_readable_user_messages")
        self.assertLessEqual(set(record), {"type", "account_key", "conversation_id", "evidence", "decision",
                                           "actor", "input_source"})
        chosen = covered_evidence(self.root, conversation="conv-voice-0001", message_ids=["v3", "v2"])
        self.assertEqual([(e["source_message_id"], e["role"]) for e in chosen["evidence"]],
                         [("v2", "assistant"), ("v3", "user")])
        self.assertNotIn("decision", chosen)
        with self.assertRaisesRegex(ValueError, "Not readable"):
            covered_evidence(self.root, conversation="conv-voice-0001", message_ids=["nope"])
        with self.assertRaisesRegex(ValueError, "own"):
            covered_evidence(self.root, conversation="conv-voice-0001", message_ids=["v2"])
        with self.assertRaisesRegex(ValueError, "decision"):
            covered_evidence(self.root, conversation="conv-voice-0001", decision="x" * 2001)

    def test_cover_source_validator_accepts_record_when_available(self):
        try:
            from markdown_library.source_coverage import normalize_source
        except ImportError:
            self.skipTest("markdown-library cover-source not installed")
        record = covered_evidence(self.root, conversation="conv-typed-0002", decision="synthetic")
        self.assertEqual(len(normalize_source(record)["evidence"]), 1)

    def test_cli_writes_new_private_file_without_printing_text(self):
        out = self.base / "evidence" / "covered.json"
        argv = ["chatgpt-pipeline", "covered-evidence", str(self.root), "--conversation", "conv-voice-0001",
                "--decision", "synthetic", "--out", str(out)]
        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as printed:
            cli.main()
        summary = json.loads(printed.getvalue())
        self.assertEqual(summary["evidence"], 2)
        self.assertNotIn("lantern", printed.getvalue())
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(out.read_bytes())["conversation_id"], "conv-voice-0001")
        with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                cli.main()
        page = self.base / "read.md"
        argv = ["chatgpt-pipeline", "archive-read", str(self.root), "--conversation", "conv-v",
                "--format", "markdown", "--out", str(page)]
        with self.assertRaises(SystemExit), mock.patch.object(sys, "argv", argv), \
                redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            cli.main()  # prefix shorter than 8 characters that is not exact is refused
        argv[4] = "conv-voice-0"
        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as printed:
            cli.main()
        self.assertEqual(json.loads(printed.getvalue())["user_messages"], 2)
        self.assertTrue(page.read_text().startswith("# title conv-voice-0001"))


if __name__ == "__main__":
    unittest.main()
