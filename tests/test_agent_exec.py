"""Synthetic-only tests: agent executor for sealed jobs and the incremental batch preparation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chatgpt_pipeline import agent_exec, bulk
from chatgpt_pipeline.incremental import prepare_batch
from chatgpt_pipeline.index import ingest
from test_incremental import BASE, LATEST, publish
from test_ingestion import package, write_batch

EXECUTOR = "claude-code:synthetic-model"


class AgentExecutorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        module = patch.object(bulk, "__file__", str(self.base / "src/chatgpt_pipeline/bulk.py"))
        module.start()
        self.addCleanup(module.stop)
        root = self.base / "exports/account-a"
        root.mkdir(parents=True)
        database = self.base / "index.sqlite3"
        self.output = self.base / "runtime/batches/test"
        write_batch(root, package("My experiment failed twice; I compared three hypotheses."))
        ingest(root.parent, database)
        bulk.prepare(database, self.output)
        _, manifest, self.store = bulk.batch(self.output)
        self.job = manifest["conversations"][0]["jobs"][0]
        self.tasks = self.base / "tasks"

    def answer(self, disposition="keep", name="answer.json"):
        result = {"disposition": disposition, "reason": "Personal experiment evidence.", "items": [] if disposition == "skip"
                  else [{"title": "An experiment", "summary": "The user compared hypotheses; outcome open.",
                         "user_message_ids": ["m001"], "related_message_ids": []}]}
        path = self.base / name
        path.write_text(json.dumps(result))
        return path

    def test_tasks_carry_the_frozen_request_unchanged(self):
        summary = agent_exec.tasks(self.output, self.tasks)
        self.assertEqual((summary["tasks"], summary["screen"], summary["review"]), (1, 1, 0))
        task = bulk.read(self.tasks / f"{self.job}.json")
        request = bulk.read(self.store / self.job / "request.json")
        self.assertEqual(task["system"], request["messages"][0]["content"])
        self.assertEqual(task["user"], request["messages"][1]["content"])
        self.assertEqual(task["request_sha256"], bulk.digest(request))

    def test_check_validates_without_writing_and_record_saves_once(self):
        state_before = bulk.read(self.store / self.job / "state.json")
        checked = agent_exec.record(self.output, self.job, self.answer(), EXECUTOR, check_only=True)
        self.assertFalse(checked["recorded"])
        self.assertEqual(bulk.read(self.store / self.job / "state.json"), state_before)
        self.assertFalse((self.store / self.job / "result.json").exists())
        recorded = agent_exec.record(self.output, self.job, self.answer(), EXECUTOR)
        self.assertTrue(recorded["recorded"])
        state = bulk.read(self.store / self.job / "state.json")
        self.assertEqual((state["state"], state["disposition"], state["executor"]), ("complete", "keep", EXECUTOR))
        receipt = bulk.read(self.store / self.job / agent_exec.RECEIPT)
        self.assertEqual(receipt["pipeline_network_requests"], 0)
        self.assertFalse((self.store / self.job / "attempt.json").exists())
        with self.assertRaisesRegex(ValueError, "never replaced"):
            agent_exec.record(self.output, self.job, self.answer("skip", "other.json"), EXECUTOR)
        progress = bulk.export(self.output)
        self.assertEqual((progress["agent_executed_jobs"], progress["attempted_calls"]), (1, 0))
        self.assertEqual(progress["unpriced_or_ambiguous_attempts"], 0)

    def test_invalid_answers_and_foreign_jobs_are_refused_without_writes(self):
        bad = self.base / "bad.json"
        bad.write_text(json.dumps({"disposition": "keep", "reason": "x", "items": [
            {"title": "t", "summary": "s", "user_message_ids": ["m999"], "related_message_ids": []}]}))
        with self.assertRaisesRegex(ValueError, "Unknown message reference"):
            agent_exec.record(self.output, self.job, bad, EXECUTOR)
        self.assertEqual(bulk.read(self.store / self.job / "state.json")["state"], "prepared")
        with self.assertRaisesRegex(ValueError, "not a member"):
            agent_exec.record(self.output, "0" * 64, self.answer(), EXECUTOR)
        with self.assertRaisesRegex(ValueError, "Executor"):
            agent_exec.record(self.output, self.job, self.answer(), "has spaces")

    def test_reviews_follow_kept_screens_and_reach_the_selection(self):
        agent_exec.record(self.output, self.job, self.answer(), EXECUTOR)
        summary = agent_exec.tasks(self.output, self.tasks)
        self.assertEqual((summary["screen"], summary["review"]), (0, 1))
        review = next(t["job_id"] for t in bulk.read(self.tasks / "tasks.json")["tasks"])
        self.assertIn("待复核的不可信候选", bulk.read(self.tasks / f"{review}.json")["user"])
        agent_exec.record(self.output, review, self.answer(), EXECUTOR)
        progress = bulk.export(self.output)
        self.assertEqual((progress["kept_items"], progress["review_jobs"], progress["agent_executed_jobs"]), (1, 1, 2))
        self.assertEqual(agent_exec.tasks(self.output, self.tasks)["tasks"], 0)


class IncrementalPrepareTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        module = patch.object(bulk, "__file__", str(self.base / "src/chatgpt_pipeline/bulk.py"))
        module.start()
        self.addCleanup(module.stop)
        self.root = self.base / "exports" / "account-x"
        self.root.mkdir(parents=True)
        _, self.base_catalog = publish(self.root, BASE, live=False)
        publish(self.root, LATEST)

    def test_batch_covers_exactly_the_unprocessed_messages_with_prior_context(self):
        output = self.base / "runtime/batches/incremental"
        result = prepare_batch(self.root, output, baseline_catalogs=[self.base_catalog])
        self.assertEqual((result["conversations"], result["screen_jobs"], result["model_calls"]), (2, 2, 0))
        _, manifest, store = bulk.batch(output)
        primaries, contexts = {}, {}
        for entry in manifest["conversations"]:
            for job in entry["jobs"]:
                refs = bulk.read(store / job / "input.json")["messages"]
                primaries[entry["source"]["conversation_id"]] = [r["source_message_id"] for r in refs if r["primary"]]
                contexts[entry["source"]["conversation_id"]] = [r["source_message_id"] for r in refs if not r["primary"]]
        self.assertEqual(primaries, {"conv-a": ["a3", "a4"], "conv-c": ["c1", "c2", "c3"]})
        self.assertEqual(contexts, {"conv-a": ["a1", "a2"], "conv-c": []})
        self.assertEqual(manifest["scope"]["type"], "incremental-plan")
        self.assertEqual(manifest["max_calls"], 4)
        source = manifest["conversations"][0]["source"]
        self.assertTrue(source["incremental_scope"]["old_context_only"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            prepare_batch(self.root, output, baseline_catalogs=[self.base_catalog])


if __name__ == "__main__":
    unittest.main()
