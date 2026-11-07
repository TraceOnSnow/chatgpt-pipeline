import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from chatgpt_pipeline import bulk
from chatgpt_pipeline.index import ingest
from test_ingestion import package, write_batch


class BulkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.module = patch.object(bulk, "__file__", str(self.base / "src/chatgpt_pipeline/bulk.py"))
        self.module.start()
        self.addCleanup(self.module.stop)
        self.root = self.base / "exports/account-a"
        self.root.mkdir(parents=True)
        self.database = self.base / "index.sqlite3"
        self.output = self.base / "runtime/batches/test"
        write_batch(self.root, package("My experiment failed twice; I compared three hypotheses."))
        ingest(self.root.parent, self.database)
        bulk.prepare(self.database, self.output)
        _, self.manifest, self.store = bulk.batch(self.output)
        self.job = self.manifest["conversations"][0]["jobs"][0]
        self.directory = self.store / self.job

    def result(self, disposition="keep"):
        return {"disposition": disposition, "reason": "Personal experiment evidence.", "items": [] if disposition == "skip" else [
            {"title": "An experiment", "summary": "The user tried an experiment and compared hypotheses; the outcome remains open.",
             "user_message_ids": ["m001"], "related_message_ids": []}]}

    def response(self, result):
        return {"id": "synthetic-provider-id", "model": bulk.MODEL, "usage": {"cost": 0.001},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}

    def provider_models(self):
        return io.BytesIO(json.dumps({"data": [{"id": bulk.MODEL, "context_length": 1000000,
                                              "pricing": {"prompt": "0.0000001", "completion": "0.0000001"}}]}).encode())

    def save_response(self, result):
        bulk.save(self.directory / "attempt.json", {"reserved_usd": "0.01"})
        bulk.save(self.directory / "receipt.json", {"http_status": 200})
        bulk.atomic_bytes(self.directory / "response.raw", json.dumps(self.response(result)).encode())

    def test_all_messages_have_one_primary_range_with_context_and_no_truncation(self):
        messages = [{"id": str(i), "role": "user" if i % 2 == 0 else "assistant", "text": str(i) * 7000} for i in range(8)]
        chunks = list(bulk.chunk_messages(messages))
        self.assertEqual([m for context, primary in chunks for m in primary], messages)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(chunks[1][0])
        with self.assertRaisesRegex(ValueError, "Oversize"):
            list(bulk.chunk_messages([{"id": "long", "role": "user", "text": "a" * 24001}]))

    def test_source_policy_is_frozen_for_new_jobs_and_legacy_jobs_remain_readable(self):
        _, definition, data, request = bulk.load_job(self.store, self.job)
        self.assertEqual(definition["identity"]["source_policy_revision"], bulk.SOURCE_POLICY_REVISION)
        self.assertEqual(request["messages"][0]["content"], bulk.SCREEN_PROMPT + bulk.SOURCE_POLICY_PROMPT)
        originals = bulk.source_messages(data["source"])
        old_job = bulk.ensure_job(self.store, data["source"], [], originals, source_policy_revision=None)
        old_dir, old_def, _, old_request = bulk.load_job(self.store, old_job)
        old_bytes = {name: (old_dir / name).read_bytes() for name in ("definition.json", "request.json", "input.json")}
        self.assertNotEqual(old_job, self.job)
        self.assertNotIn("source_policy_revision", old_def["identity"])
        self.assertEqual(old_request["messages"][0]["content"], bulk.SCREEN_PROMPT)
        self.assertEqual(bulk.load_job(self.store, self.job)[3], request)
        self.manifest["conversations"][0]["jobs"] = [old_job]
        bulk.save(self.output / "manifest.json", self.manifest)
        bulk.save(self.output / "manifest-seal.json", {"sha256": bulk.digest(self.manifest)})
        bulk.reconfigure_pending(self.output)
        self.assertEqual(bulk.batch(self.output)[1]["conversations"][0]["jobs"], [old_job])
        self.assertEqual({name: (old_dir / name).read_bytes() for name in old_bytes}, old_bytes)
        with self.assertRaisesRegex(ValueError, "source policy revision"):
            bulk.ensure_job(self.store, data["source"], [], originals, source_policy_revision="unrecognized")

    def test_saved_response_recovers_offline_and_is_never_charged_again(self):
        self.save_response(self.result("skip"))
        self.assertEqual(bulk.recover(self.directory)["state"], "complete")
        with patch("urllib.request.urlopen", return_value=self.provider_models()), \
             patch.object(bulk, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open") as post:
            report = bulk.run(self.output)
        post.assert_not_called()
        self.assertTrue(report["finished"])
        self.assertEqual(report["actual_cost_usd"], "0.001")

    def test_unfinished_attempt_is_quarantined_without_resending(self):
        bulk.save(self.directory / "attempt.json", {"reserved_usd": "0.01"})
        with patch("urllib.request.urlopen", return_value=self.provider_models()), \
             patch.object(bulk, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open") as post:
            report = bulk.run(self.output)
        post.assert_not_called()
        self.assertEqual(report["states"]["needs_reconciliation"], 1)
        self.assertFalse(report["finished"])

    def test_source_mutation_before_call_never_gets_sent(self):
        source = self.manifest["conversations"][0]["source"]
        Path(source["source_path"]).write_bytes(b'changed')
        with patch("urllib.request.urlopen", return_value=self.provider_models()), \
             patch.object(bulk, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open") as post:
            report = bulk.run(self.output)
        post.assert_not_called()
        self.assertEqual(report["states"]["source_changed"], 1)

    def test_bad_roles_and_missing_primary_evidence_rejected(self):
        result = self.result()
        refs = [{"id": "m001", "role": "assistant", "primary": True}]
        with self.assertRaisesRegex(ValueError, "role mismatch"):
            bulk.validate(result, refs)
        refs[0].update(role="user", primary=False)
        with self.assertRaisesRegex(ValueError, "primary"):
            bulk.validate(result, refs)

    def test_keeps_require_a_separate_durable_review_before_display(self):
        self.save_response(self.result())
        bulk.recover(self.directory)
        self.assertEqual(bulk.export(self.output)["kept_items"], 0)
        jobs, reviews = bulk.member_jobs(self.manifest, self.store, create_reviews=True)
        child = self.store / reviews[self.job]
        bulk.save(child / "attempt.json", {"reserved_usd": "0.01"})
        bulk.save(child / "receipt.json", {"http_status": 200})
        bulk.atomic_bytes(child / "response.raw", json.dumps(self.response(self.result())).encode())
        bulk.recover(child)
        report = bulk.export(self.output)
        self.assertEqual(report["kept_items"], 1)
        self.assertEqual(len(jobs), 2)
        self.assertTrue(report["finished"])
        selected = bulk.read(self.output / "selection.json")[0]
        self.assertEqual(selected["evidence"][0]["source_message_id"], "m1")

    def test_budget_guard_prevents_any_paid_call(self):
        with patch("urllib.request.urlopen", return_value=self.provider_models()), \
             patch.object(bulk, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open") as post:
            bulk.run(self.output, budget_usd="0.0001")
        post.assert_not_called()
        self.assertFalse((self.directory / "attempt.json").exists())
        self.assertIn("budget_guard", bulk.read(self.output / "run-state.json")["stop_reasons"])

    def test_entire_screen_review_run_then_resume_has_exactly_two_mocked_calls(self):
        responses = []
        for result in (self.result(), self.result("skip")):
            response = io.BytesIO(json.dumps(self.response(result)).encode())
            response.status = 200
            responses.append(response)
        with patch("urllib.request.urlopen", return_value=self.provider_models()), \
             patch.object(bulk, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=responses) as post:
            report = bulk.run(self.output, workers=1)
        self.assertEqual(post.call_count, 2)
        self.assertTrue(report["finished"])
        self.assertEqual(report["kept_items"], 0)
        self.assertEqual(report["actual_cost_usd"], "0.002")
        with patch("urllib.request.urlopen", return_value=self.provider_models()), \
             patch.object(bulk, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open") as post:
            resumed = bulk.run(self.output)
        post.assert_not_called()
        self.assertTrue(resumed["finished"])

    def test_transport_failure_records_attempt_and_cannot_be_retried(self):
        with patch("urllib.request.urlopen", return_value=self.provider_models()), \
             patch.object(bulk, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError) as post:
            report = bulk.run(self.output, workers=1)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(report["states"], {"needs_reconciliation": 1})
        self.assertTrue((self.directory / "attempt.json").exists())
        with patch("urllib.request.urlopen", return_value=self.provider_models()), \
             patch.object(bulk, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open") as post:
            bulk.run(self.output, workers=1)
        post.assert_not_called()

    def test_compatible_jobs_reused_and_parent_snapshot_immutable(self):
        other = self.base / "runtime/batches/second"
        bulk.prepare(self.database, other)
        _, manifest, _ = bulk.batch(other)
        self.assertEqual(manifest["conversations"][0]["jobs"], [self.job])
        changed = bulk.read(self.output / "manifest.json")
        changed["max_calls"] = 999
        bulk.save(self.output / "manifest.json", changed)
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            bulk.batch(self.output)

    def test_only_unattempted_reasoning_configuration_is_versioned(self):
        data = bulk.read(self.directory / "input.json")
        messages = bulk.source_messages(data["source"])
        original_payload = bulk.payload

        def legacy(stage, rendered):
            request = original_payload(stage, rendered)
            request["reasoning"] = {"enabled": False}
            return request

        with patch.object(bulk, "payload", side_effect=legacy):
            old_job = bulk.ensure_job(self.store, data["source"], [], messages)
        old_request = (self.store / old_job / "request.json").read_bytes()
        self.manifest["conversations"][0]["jobs"] = [old_job]
        bulk.save(self.output / "manifest.json", self.manifest)
        bulk.save(self.output / "manifest-seal.json", {"sha256": bulk.digest(self.manifest)})
        bulk.reconfigure_pending(self.output)
        _, changed, _ = bulk.batch(self.output)
        self.assertEqual(changed["conversations"][0]["jobs"], [self.job])
        self.assertEqual((self.store / old_job / "request.json").read_bytes(), old_request)
        self.assertEqual(changed["max_calls"], self.manifest["max_calls"])
        self.assertEqual(changed["conversations"][0]["source"], self.manifest["conversations"][0]["source"])

    def test_redundant_type_metadata_recovery_preserves_raw_response_and_prior_failure(self):
        result = {"type": "json_object", **self.result("skip")}
        self.save_response(result)
        original = (self.directory / "response.raw").read_bytes()
        bulk.save(self.directory / "state.json", {"state": "validation_failed", "error": "Incorrect selection fields"})
        self.assertTrue(bulk.repair_format_metadata(self.directory))
        self.assertEqual((self.directory / "response.raw").read_bytes(), original)
        self.assertEqual(bulk.read(self.directory / "state.json")["state"], "complete")
        history = bulk.read(self.directory / "format-recovery-v1.json")
        self.assertEqual(history["prior_state"]["state"], "validation_failed")
        self.assertEqual(history["new_api_calls"], 0)

    def test_concurrent_screen_jobs_each_have_one_attempt_and_one_charge(self):
        body = package("a" * 16000)
        message = {"id": "m2", "author": {"role": "user"}, "recipient": "all",
                   "content": {"content_type": "text", "parts": ["b" * 16000]}}
        body["raw"]["mapping"]["n2"] = {"parent": "n1", "message": message}
        body["raw"]["current_node"] = "n2"
        body["messages"].append({"id": "m2", "role": "user", "created": 2, "content": message["content"]})
        write_batch(self.root, body)
        ingest(self.root.parent, self.database)
        output = self.base / "runtime/batches/concurrent"
        bulk.prepare(self.database, output)
        barrier = threading.Barrier(2)

        def response(request, **kwargs):
            barrier.wait(timeout=5)
            result = io.BytesIO(json.dumps(self.response(self.result("skip"))).encode())
            result.status = 200
            return result

        with patch("urllib.request.urlopen", return_value=self.provider_models()), \
             patch.object(bulk, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=response) as post:
            report = bulk.run(output, workers=2)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(report["attempted_calls"], 2)
        self.assertEqual(report["actual_cost_usd"], "0.002")
        self.assertTrue(report["finished"])


if __name__ == "__main__":
    unittest.main()
