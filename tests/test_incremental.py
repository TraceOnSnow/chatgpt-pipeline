"""Synthetic-only tests for the read-only incremental plan."""
from contextlib import closing, redirect_stdout
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from chatgpt_pipeline import bulk, cli, notes
from chatgpt_pipeline.incremental import plan, summary_text
from chatgpt_pipeline.verify import encoded, mapping_hash, sha


def message(identity, role, text, created, metadata=None, voice=False):
    content = ({"content_type": "multimodal_text",
                "parts": [{"content_type": "audio_transcription", "text": text},
                          {"content_type": "real_time_user_audio_video_asset_pointer"}]}
               if voice else {"content_type": "text", "parts": [text]})
    return {"id": identity, "author": {"role": role}, "content": content, "recipient": "all",
            "metadata": metadata or {}, "create_time": created}


def package(conversation_id, messages):
    mapping, parent = {}, None
    for item in messages:
        node = "n-" + item["id"]
        mapping[node] = {"parent": parent, "message": item}
        parent = node
    raw = {"conversation_id": conversation_id, "title": "synthetic", "current_node": parent, "mapping": mapping}
    return {"schema_version": 1, "source": "chatgpt", "untrusted_data": True, "conversation_id": conversation_id,
            "url": "https://example.invalid/" + conversation_id, "raw": raw,
            "raw_sha256": sha(encoded(raw)), "content_sha256": mapping_hash(mapping),
            "readable_status": "complete_current_branch",
            "messages": [{"id": m["id"], "role": m["author"]["role"], "created": m["create_time"],
                          "metadata": {}, "content": json.loads(json.dumps(m["content"]))} for m in messages]}


def publish(root, conversations, *, live=True, bodiless=()):
    """Write objects, a catalog and its commit exactly like the uploader layout."""
    entries, objects = [], []
    for conversation_id in bodiless:
        entries.append({"id": conversation_id, "title": "title " + conversation_id, "body_status": "not_fetched",
                        "created": 1, "updated": 1})
    for conversation_id, messages in conversations.items():
        body = package(conversation_id, messages)
        data = encoded(body)
        name = f"objects/{conversation_id}/{body['raw_sha256']}.json"
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
        objects.append({"path": name, "size": len(data), "sha256": sha(data)})
        entries.append({"id": conversation_id, "title": "title " + conversation_id, "body_status": "current",
                        "object": name, "raw_sha256": body["raw_sha256"], "content_sha256": body["content_sha256"],
                        "created": messages[0]["create_time"], "updated": messages[-1]["create_time"]})
    catalog = {"schema_version": 1, "account_key": root.name, "listing_complete": True,
               "bodies_complete": not bodiless, "body_count": len(entries) - len(bodiless), "conversations": entries}
    catalog_data = encoded(catalog)
    catalog_path = f"catalogs/{sha(catalog_data)}.json"
    (root / "catalogs").mkdir(exist_ok=True)
    (root / catalog_path).write_bytes(catalog_data)
    if live:
        (root / "catalog.json").write_bytes(catalog_data)
    commit = encoded({"schema_version": 1, "type": "chatgpt-upload-commit", "account_key": root.name,
                      "catalog": catalog_path, "catalog_sha256": sha(catalog_data), "object_count": len(objects),
                      "objects": objects})
    (root / "commits").mkdir(exist_ok=True)
    path = root / "commits" / (sha(commit) + ".commit.json")
    path.write_bytes(commit)
    return path, root / catalog_path


def texts(messages):
    return [{"id": m["id"], "role": m["author"]["role"],
             "text": m["content"]["parts"][0] if m["content"]["content_type"] == "text"
             else m["content"]["parts"][0]["text"]} for m in messages]


def tree_digest(directory):
    digest = hashlib.sha256()
    for path in sorted(Path(directory).rglob("*")):
        digest.update(str(path.relative_to(directory)).encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


BASE = {
    "conv-a": [message("a1", "user", "first synthetic question", 100),
               message("a2", "assistant", "first synthetic answer", 101)],
    "conv-b": [message("b1", "user", "stable synthetic note", 50),
               message("b2", "assistant", "stable synthetic reply", 51)],
}
LATEST = {
    "conv-a": BASE["conv-a"] + [message("a3", "user", "follow-up 继续", 500, {"dictation": True}),
                                message("a4", "assistant", "follow-up answer", 501)],
    "conv-b": BASE["conv-b"],
    "conv-c": [message("c1", "user", "spoken synthetic idea", 600, {"voice_mode_message": True}, voice=True),
               message("c2", "assistant", "spoken reply", 601),
               message("c3", "user", "typed synthetic detail", 700)],
}


class IncrementalPlanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "exports" / "account-x"
        self.root.mkdir(parents=True)
        self.base_commit, self.base_catalog = publish(self.root, BASE, live=False)
        self.latest_commit, _ = publish(self.root, LATEST)

    def workspace(self, *, receipt=True, journal=False, queued=None, scope=None):
        """A registered batch whose jobs queued ``queued``: conversation -> (context, primary) messages."""
        if queued is None:
            queued = {cid: ([], BASE[cid]) for cid in BASE}
        batch, store = self.base / "batch", self.base / "store"
        batch.mkdir()
        store.mkdir()
        entries = []
        for conversation_id, (context, primary) in queued.items():
            source = {"account_key": self.root.name, "conversation_id": conversation_id}
            entries.append({"source": source, "jobs": [bulk.ensure_job(store, source, texts(context), texts(primary))]})
        manifest = {"snapshots": [{"account_key": self.root.name, "catalog_sha256": sha(self.base_catalog.read_bytes()),
                                   "commit_sha256": self.base_commit.name.split(".")[0], "source_root": str(self.root)}],
                    "store": str(store), "conversations": entries}
        if scope:
            manifest["scope"] = scope
        bulk.save(batch / "manifest.json", manifest)
        bulk.save(batch / "manifest-seal.json", {"sha256": bulk.digest(manifest)})
        bulk.save(batch / "progress.json", {"actual_cost_usd": "0.25"})
        workspace = self.base / "workspace"
        workspace.mkdir()
        bulk.save(workspace / "config.json", {"budget_usd": "2"})
        with closing(sqlite3.connect(workspace / "state.sqlite3")) as db, db:
            db.executescript("""
                CREATE TABLE items(id TEXT PRIMARY KEY,ordinal INTEGER,title TEXT,summary TEXT,
                    candidate TEXT,origin TEXT,decision TEXT NOT NULL DEFAULT 'todo',job_id TEXT);
                CREATE TABLE jobs(id TEXT PRIMARY KEY,item_id TEXT,state TEXT,error TEXT,created_at TEXT,updated_at TEXT);
                CREATE TABLE calls(job_id TEXT,stage TEXT,request_hash TEXT,reservation TEXT,
                    cost TEXT,response_id TEXT,started_at TEXT,PRIMARY KEY(job_id,stage));
                CREATE TABLE imports(selection_hash TEXT PRIMARY KEY,origin TEXT,created_at TEXT);""")
            origin = {"batch": str(batch), "manifest_sha256": bulk.digest(manifest)}
            db.execute("INSERT INTO imports VALUES('s1',?,'t')", (json.dumps(origin),))
            candidate = {"source": {"account_key": self.root.name, "conversation_id": "conv-a"}}
            db.execute("INSERT INTO items(id,ordinal,candidate,origin,decision,job_id) VALUES('item-a',1,?,?,'todo','job-a')",
                       (json.dumps(candidate), json.dumps(origin)))
            db.execute("INSERT INTO jobs VALUES('job-a','item-a','done',NULL,'t','t')")
            db.execute("INSERT INTO calls VALUES('job-a','draft','h','0.5','0.1',NULL,'t')")
            db.execute("INSERT INTO calls VALUES('job-a','review','h','0.2',NULL,NULL,'t')")
        if receipt:
            (workspace / "vault-imports" / "vaulthash").mkdir(parents=True)
            bulk.save(workspace / "vault-imports" / "vaulthash" / "item-a.json", {"id": "note"})
        if journal:
            (workspace / "state.sqlite3-journal").write_bytes(b"x")
        return workspace

    def vault(self):
        vault = self.base / "vault"
        (vault / ".markdown-library").mkdir(parents=True)
        bulk.save(vault / ".markdown-library" / "state.json", {"sources": {
            "chatgpt-workbench:item-a": {"id": "n1", "source": {"type": "t", "item_id": "item-a"}},
            "manual-v1:x": {"id": "n2", "source": {"account_key": self.root.name, "conversation_id": "conv-c",
                                                    "source_message_ids": [{"source_message_id": "c1"}]}}}})
        return vault

    def by_id(self, result):
        return {row["conversation_id"]: row for row in result["conversations"]}

    def test_explicit_baseline_finds_new_and_continued_conversations(self):
        before = tree_digest(self.root)
        result = plan(self.root, baseline_catalogs=[self.base_catalog], since="1970-01-01T00:11:00+00:00")
        self.assertEqual(tree_digest(self.root), before)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["counts"]["new_conversations"], 1)
        self.assertEqual(result["counts"]["continuations"], 1)
        self.assertEqual(result["counts"]["unchanged_conversations"], 1)
        rows = self.by_id(result)
        self.assertNotIn("conv-b", rows)
        self.assertEqual(rows["conv-a"]["change"], "continuation")
        self.assertEqual(rows["conv-a"]["baseline_covered_messages"], 2)
        self.assertEqual(rows["conv-a"]["stats"]["user_messages"], 1)
        self.assertEqual(rows["conv-a"]["stats"]["user_characters"], len("follow-up 继续"))
        self.assertEqual(rows["conv-a"]["stats"]["dictation_messages"], 1)
        self.assertEqual(rows["conv-a"]["screen_jobs"][0]["context_messages"], 2)
        self.assertEqual(rows["conv-c"]["change"], "new_conversation")
        self.assertEqual(rows["conv-c"]["stats"]["user_messages"], 2)
        self.assertEqual(rows["conv-c"]["stats"]["voice_mode_messages"], 1)
        self.assertEqual(rows["conv-c"]["stats"]["user_messages_since"], 1)
        self.assertEqual(result["counts"]["user_messages_since"], 1)
        estimate = result["estimate"]
        self.assertEqual(estimate["screen_calls"], 2)
        self.assertEqual(estimate["screen_review_calls_max"], 2)
        self.assertGreater(Decimal(estimate["screening_usd_cap_total"]), 0)
        self.assertGreater(Decimal(estimate["note_usd_per_target_cap_max"]), 0)
        self.assertEqual(estimate["note_targets_max_by_validator"], 12)
        self.assertIsNone(estimate["notes_ledger"])
        self.assertIn("conv-c", summary_text(result))

    def test_screen_estimate_matches_existing_job_request_and_reservation(self):
        result = plan(self.root, baseline_catalogs=[self.base_catalog])
        job = self.by_id(result)["conv-c"]["screen_jobs"][0]
        texts = [{"id": m["id"], "role": m["author"]["role"], "text": m["content"]["parts"][0]
                  if m["content"]["content_type"] == "text" else m["content"]["parts"][0]["text"]}
                 for m in LATEST["conv-c"]]
        _, request = bulk.build_request([], texts)
        self.assertEqual(job["screen_request_bytes"], len(encoded(request)))
        self.assertEqual(job["screen_reservation_usd"], str(bulk.reservation_usd(request)))
        store = self.base / "store"
        store.mkdir()
        source = {"account_key": "a", "conversation_id": "c"}
        job_id = bulk.ensure_job(store, source, [], texts)
        self.assertEqual(bulk.read(store / job_id / "request.json"), request)

    def test_changed_text_with_same_id_is_a_continuation(self):
        changed = dict(LATEST)
        changed["conv-b"] = [message("b1", "user", "stable synthetic note, edited", 50), BASE["conv-b"][1]]
        commit, _ = publish(self.root, changed)
        result = plan(self.root, commit, baseline_catalogs=[self.base_catalog])
        row = self.by_id(result)["conv-b"]
        self.assertEqual(row["change"], "continuation")
        self.assertEqual(row["changed_existing_message_ids"], 1)

    def test_workspace_baseline_receipts_library_and_ledger_are_read_only(self):
        workspace, vault = self.workspace(), self.vault()
        before = (tree_digest(workspace), tree_digest(vault), tree_digest(self.root))
        result = plan(self.root, workspace=workspace, vault=vault)
        self.assertEqual((tree_digest(workspace), tree_digest(vault), tree_digest(self.root)), before)
        self.assertFalse((workspace / "state.sqlite3-journal").exists())
        self.assertEqual(result["baseline"]["sources"][0]["origin"], "workspace_registered_batch")
        self.assertEqual(result["baseline"]["treated_as_processed"], "queued_primary_messages")
        self.assertEqual(result["counts"]["new_conversations"], 1)
        rows = self.by_id(result)
        self.assertEqual(rows["conv-a"]["workbench"], {"items": 1, "decisions": {"todo": 1}, "vault_import_receipts": 1})
        self.assertEqual(rows["conv-a"]["library"]["item_sources"], 1)
        self.assertEqual(rows["conv-c"]["library"]["conversation_sources"], 1)
        self.assertEqual(rows["conv-c"]["library"]["unprocessed_messages_already_referenced"], 1)
        self.assertEqual(rows["conv-c"]["library"]["covered_by_library"], 0)
        ledger = result["estimate"]["notes_ledger"]
        self.assertEqual(ledger["remaining_usd"], "1.7")
        self.assertEqual(ledger["unknown_cost_calls"], 1)
        self.assertEqual(result["estimate"]["screening_ledger_prior_actual_usd"], "0.25")

    def test_baseline_must_be_derivable_or_explicit_and_verified(self):
        with self.assertRaisesRegex(ValueError, "baseline"):
            plan(self.root)
        unpublished = self.base / "copy.json"
        catalog = json.loads(self.base_catalog.read_bytes())
        catalog["conversations"] = catalog["conversations"][:1]
        unpublished.write_bytes(encoded(catalog))
        with self.assertRaisesRegex(ValueError, "no published commit"):
            plan(self.root, baseline_catalogs=[unpublished])
        with self.assertRaisesRegex(ValueError, "rollback journal"):
            plan(self.root, workspace=self.workspace(journal=True))

    def test_cli_writes_new_private_files_and_never_replaces(self):
        out, summary = self.base / "plan" / "plan.json", self.base / "plan" / "summary.md"
        argv = ["chatgpt-pipeline", "incremental-plan", str(self.root), "--baseline-catalog", str(self.base_catalog),
                "--out", str(out), "--summary", str(summary)]
        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as printed:
            cli.main()
        self.assertEqual(json.loads(printed.getvalue())["counts"]["new_conversations"], 1)
        self.assertEqual(json.loads(out.read_bytes())["schema"], "chatgpt-incremental-plan-v2")
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        self.assertTrue(summary.read_text().startswith("# Incremental plan"))
        with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                cli.main()

    def test_explicit_catalog_is_labelled_whole_catalog(self):
        result = plan(self.root, baseline_catalogs=[self.base_catalog])
        self.assertEqual(result["baseline"]["treated_as_processed"], "whole_catalog")
        self.assertEqual(result["baseline"]["sources"][0]["treated_as_processed"], "whole_catalog")
        self.assertIn("整份 catalog 视为已处理", summary_text(result))

    def test_date_bounded_batch_does_not_mark_earlier_or_skipped_messages_processed(self):
        # The batch saw the whole BASE snapshot but only queued a2 as primary (a1 was old context)
        # and skipped conv-b entirely.
        workspace = self.workspace(queued={"conv-a": (BASE["conv-a"][:1], BASE["conv-a"][1:])},
                                   scope={"type": "one-authorized-date-bounded-incremental"})
        result = plan(self.root, workspace=workspace)
        rows = self.by_id(result)
        self.assertEqual(rows["conv-a"]["change"], "continuation")
        self.assertEqual(rows["conv-a"]["baseline_covered_messages"], 1)
        self.assertEqual(rows["conv-a"]["stats"]["user_messages"], 2)
        self.assertEqual(rows["conv-b"]["change"], "new_conversation")
        self.assertEqual(rows["conv-b"]["unprocessed_messages"], 2)
        self.assertEqual(result["counts"]["new_conversations"], 2)
        self.assertNotIn("unchanged_conversations", result["counts"])
        batch = result["baseline"]["registered_batches"][0]
        self.assertEqual((batch["account_jobs"], batch["account_primary_refs"]), (1, 1))

    def test_exact_library_evidence_is_deducted_from_primary_and_cost(self):
        without = plan(self.root, baseline_catalogs=[self.base_catalog])
        vault = self.base / "vault"
        (vault / ".markdown-library").mkdir(parents=True)
        c1, c3 = texts(LATEST["conv-c"])[0], texts(LATEST["conv-c"])[2]
        a3 = texts(LATEST["conv-a"])[2]
        bulk.save(vault / ".markdown-library" / "state.json", {"sources": {
            "chatgpt-source-first-covered-v1:x": {"id": "n1", "source": {
                "account_key": self.root.name, "conversation_id": "conv-c", "evidence": [
                    {"id": "m001", "primary": True, "role": "user", "source_message_id": "c1",
                     "text_sha256": sha(c1["text"].encode())},
                    {"id": "m002", "primary": True, "role": "user", "source_message_id": "c3",
                     "text_sha256": sha(b"different text")}]}},
            "chatgpt-memory-delivery-v1:y": {"id": "n2", "source": {
                "account_key": self.root.name, "conversation_id": "conv-a",
                "evidence": [], "input_context": [{"account_key": self.root.name, "conversation_id": "conv-a",
                                                   "message_id": "a3", "role": "user",
                                                   "text_sha256": sha(a3["text"].encode())}]}},
            "chatgpt-pending-manual-v1:z": {"id": "n3", "source": {"account_key": self.root.name,
                                                                   "conversation_id": "conv-a"}}}})
        result = plan(self.root, baseline_catalogs=[self.base_catalog], vault=vault)
        rows = self.by_id(result)
        self.assertEqual(rows["conv-c"]["covered_by_library"], 1)
        self.assertEqual(rows["conv-c"]["covered_by_library_types"], {"chatgpt-source-first-covered-v1": 1})
        self.assertEqual(rows["conv-c"]["stats"]["user_messages"], 1)
        self.assertEqual(rows["conv-c"]["unprocessed_messages"], 2)
        self.assertEqual(rows["conv-a"]["covered_by_library"], 0)  # context and conversation-only refs are hints
        self.assertEqual(rows["conv-a"]["library"]["conversation_sources"], 2)
        self.assertEqual(result["counts"]["covered_by_library"], 1)
        self.assertEqual(result["counts"]["unprocessed_user_messages"],
                         without["counts"]["unprocessed_user_messages"] - 1)
        self.assertEqual(rows["conv-c"]["screen_jobs"][0]["primary_messages"], 2)
        # The covered message stays available as context but is no longer screened as primary.
        self.assertEqual(rows["conv-c"]["screen_jobs"][0]["context_messages"], 1)
        self.assertEqual(rows["conv-c"]["screen_jobs"][0]["primary_characters"],
                         self.by_id(without)["conv-c"]["screen_jobs"][0]["primary_characters"] - len(c1["text"]))

    def test_baseline_conversation_without_current_body_is_a_gap(self):
        commit, _ = publish(self.root, {"conv-a": LATEST["conv-a"]}, bodiless=["conv-b"])
        result = plan(self.root, commit, baseline_catalogs=[self.base_catalog])
        self.assertIn({"conversation_id": "conv-b", "body_status": "not_fetched",
                       "reason": "baseline_conversation_without_current_body"}, result["gaps"])

    def test_bodiless_baseline_conversation_is_not_a_continuation(self):
        _, catalog = publish(self.root, BASE, live=False, bodiless=["conv-c"])
        result = plan(self.root, self.latest_commit, baseline_catalogs=[catalog])
        self.assertEqual(self.by_id(result)["conv-c"]["change"], "new_conversation")

    def test_cli_refuses_same_out_and_summary_before_writing(self):
        target = self.base / "plan" / "same.json"
        argv = ["chatgpt-pipeline", "incremental-plan", str(self.root), "--baseline-catalog", str(self.base_catalog),
                "--out", str(target), "--summary", str(self.base / "plan" / ".." / "plan" / "same.json")]
        with mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()) as error:
                cli.main()
        self.assertIn("different files", error.getvalue())
        self.assertFalse((self.base / "plan").exists())

    def test_note_reservation_helper_matches_existing_formula(self):
        payload = {"max_tokens": 8000, "messages": [{"content": "中" * 10}]}
        expected = (Decimal(len(encoded(payload))) * Decimal("0.3") + 8000 * Decimal("1.2")) / Decimal(1000000) * Decimal("1.1")
        self.assertEqual(notes.call_reservation(payload), expected)


if __name__ == "__main__":
    unittest.main()
