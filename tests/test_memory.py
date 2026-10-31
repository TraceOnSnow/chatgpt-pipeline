import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from chatgpt_pipeline import bulk, memory, notes
from chatgpt_pipeline.index import ingest
from chatgpt_pipeline.normalize import normalize
from test_ingestion import package, write_batch


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.module = patch.object(bulk, "__file__", str(self.base / "src/chatgpt_pipeline/bulk.py"))
        self.module.start()
        self.addCleanup(self.module.stop)
        self.zip = self.base / "memory.zip"
        self.profile = self.base / "profile.json"
        bulk.save(self.profile, {"version": "memory-test-v1", "screen_prompt": bulk.SCREEN_PROMPT + "\ntest-screen",
                                 "review_prompt": bulk.REVIEW_PROMPT + "\ntest-review"})
        self.output = self.base / "runtime/memory-test"

    def archive(self, raws):
        with zipfile.ZipFile(self.zip, "w") as z:
            for i, raw in enumerate(raws):
                z.writestr(f"conversations-{i:03d}.json", json.dumps([raw]))
            z.writestr("audio/private.wav", b"unreadable test attachment")

    def prepare(self, raws, **kwargs):
        self.archive(raws)
        return memory.prepare(self.zip, self.output, self.profile, "account-a", **kwargs)

    def job(self):
        _, manifest, store = bulk.batch(self.output)
        job = manifest["conversations"][0]["jobs"][0]
        return store, job, bulk.load_job(store, job)

    def response(self, disposition="keep"):
        result = {"disposition": disposition, "reason": "Personal experiment.", "items": [] if disposition == "skip" else [
            {"title": "An experiment", "summary": "The user tried a personal experiment; the outcome remains open.",
             "user_message_ids": ["m001"], "related_message_ids": []}]}
        return {"id": "mock-id", "model": bulk.MODEL, "usage": {"cost": 0.001},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}

    def finish(self, directory, disposition="keep"):
        bulk.save(directory / "attempt.json", {"reserved_usd": "0.01"})
        bulk.save(directory / "receipt.json", {"http_status": 200})
        bulk.atomic_bytes(directory / "response.raw", json.dumps(self.response(disposition)).encode())
        bulk.recover(directory)

    def test_offline_zip_scope_all_months_current_branch_fingerprint_and_provenance(self):
        first = package("My experience")
        raw = first["raw"]
        raw["create_time"] = 1757462400
        other = copy.deepcopy(raw)
        other["conversation_id"] = "c2"
        other["create_time"] = 1777075200
        with patch("urllib.request.urlopen") as network:
            report = self.prepare([raw, other])
        network.assert_not_called()
        self.assertEqual(report["scope"]["conversations"], 2)
        self.assertEqual(report["scope"]["created_min"][:7], "2025-09")
        self.assertEqual(report["scope"]["created_max"][:7], "2026-04")
        self.assertFalse((self.output / "audio").exists())
        store, job, (directory, definition, source, request) = self.job()
        self.assertEqual(source["source"]["zip_provenance"]["zip_sha256"], memory.file_sha(self.zip))
        self.assertEqual(source["source"]["input_fingerprint"], normalize(first)["input_fingerprint"])
        self.assertEqual(request["messages"][0]["content"], bulk.load_profile(self.profile)["screen_prompt"])
        self.assertEqual(len(bulk.source_messages(source["source"])), 1)

    def test_unsupported_tools_and_attachments_are_gaps_without_losing_readable_text(self):
        raw = package("Personal result")["raw"]
        raw["mapping"]["n2"] = {"parent": "n1", "message": {"id": "m2", "author": {"role": "assistant"},
                                "recipient": "browser", "content": {"content_type": "tether_browsing_display", "result": "opaque"}}}
        raw["mapping"]["n3"] = {"parent": "n2", "message": {"id": "m3", "author": {"role": "user"},
                                "content": {"content_type": "multimodal_text", "parts": ["My image context", {"content_type": "image_asset_pointer", "asset_pointer": "file://image"}]}}}
        raw["current_node"] = "n3"
        report = self.prepare([raw])
        self.assertEqual(report["scope"]["selection_states"], {"prepared": 1})
        source = self.job()[2][2]["source"]
        self.assertEqual([m["id"] for m in bulk.source_messages(source)], ["m1", "m3"])
        gaps = bulk.read(self.output / "gaps.json")
        self.assertEqual(gaps[0]["attachment_references"], 1)
        self.assertTrue(gaps[0]["text_retained"])

    def test_broken_branch_blocks_without_partial_text_becoming_a_job(self):
        raw = package()["raw"]
        raw["mapping"]["n1"]["parent"] = "missing"
        report = self.prepare([raw])
        self.assertEqual(report["scope"]["screen_jobs"], 0)
        self.assertEqual(report["scope"]["selection_states"], {"blocked": 1})
        self.assertFalse(bulk.read(self.output / "gaps.json")[0]["text_retained"])

    def test_same_account_index_dedup_is_read_only_and_cross_account_does_not_skip(self):
        root = self.base / "exports/account-a"
        root.mkdir(parents=True)
        body = package()
        write_batch(root, body)
        database = self.base / "index.sqlite3"
        ingest(root.parent, database)
        before = database.read_bytes()
        report = self.prepare([body["raw"]], index=database)
        self.assertEqual(report["scope"]["screen_jobs"], 0)
        self.assertEqual(report["scope"]["selection_states"], {"existing_same_account_content": 1})
        self.assertEqual(database.read_bytes(), before)
        other = self.base / "runtime/other-account"
        report = memory.prepare(self.zip, other, self.profile, "account-b", database)
        self.assertEqual(report["scope"]["screen_jobs"], 1)
        self.assertEqual(report["account_matching"]["cross_account_matches"], 1)
        self.assertEqual(database.read_bytes(), before)

    def test_oversize_user_text_is_segmented_with_lossless_offsets(self):
        text = "A" * 65001
        self.prepare([package(text)["raw"]])
        _, manifest, store = bulk.batch(self.output)
        source = manifest["conversations"][0]["source"]
        messages = bulk.source_messages(source)
        self.assertEqual("".join(m["text"] for m in messages), text)
        self.assertEqual(len(source["segments"]), 4)
        self.assertEqual(source["segments"][-1]["end"], len(text))
        for job in manifest["conversations"][0]["jobs"]:
            _, _, data, _ = bulk.load_job(store, job)
            bulk.verify_refs(data, messages)

    def test_profile_is_frozen_review_inherits_and_workspace_reuses_existing_notes_flow(self):
        self.prepare([package()["raw"]])
        store, job, (directory, definition, source, request) = self.job()
        bulk.save(self.profile, {"version": "changed", "screen_prompt": "changed", "review_prompt": "changed"})
        self.assertEqual(bulk.load_job(store, job)[3], request)
        self.finish(directory)
        child = bulk.review_job(store, job)
        child_dir, child_def, _, child_request = bulk.load_job(store, child)
        self.assertEqual(child_def["identity"]["selection_profile"], definition["identity"]["selection_profile"])
        self.assertTrue(child_request["messages"][0]["content"].endswith("test-review"))
        bulk.save(directory / "review.json", {"job_id": child, "parent_result_sha256": bulk.digest(bulk.read(directory / "result.json"))})
        self.finish(child_dir)
        self.assertEqual(bulk.export(self.output)["kept_items"], 1)
        workspace = self.base / "runtime/new-notes"
        memory.initialize(self.output, workspace)
        self.assertEqual(notes.Workspace(workspace).list()["counts"]["todo"], 1)

    def test_new_profile_jobs_keep_budget_and_no_resend_guards(self):
        self.prepare([package()["raw"]])
        store, job, (directory, *_rest) = self.job()
        models = {"data": [{"id": bulk.MODEL, "context_length": 1000000,
                            "pricing": {"prompt": "0.0000001", "completion": "0.0000001"}}]}
        def provider():
            return io.BytesIO(json.dumps(models).encode())
        with patch("urllib.request.urlopen", side_effect=lambda *a, **k: provider()), \
             patch.object(bulk, "credential", return_value="mock-key"), \
             patch("urllib.request.OpenerDirector.open") as post:
            bulk.run(self.output, workers=1, budget_usd="0.0001")
            post.assert_not_called()
            self.assertFalse((directory / "attempt.json").exists())
            bulk.save(directory / "attempt.json", {"reserved_usd": "0.01"})
            bulk.run(self.output, workers=1, budget_usd="1")
            post.assert_not_called()
        self.assertEqual(bulk.read(directory / "state.json")["state"], "needs_reconciliation")


    def test_scoped_optional_related_recovery_preserves_raw_and_sealed_requests(self):
        body = bulk.read(self.profile)
        body["version"] = "memory-personal-v1"
        bulk.save(self.profile, body)
        self.prepare([package()["raw"]])
        store, job, (directory, definition, source, request) = self.job()
        response = self.response()
        result = json.loads(response["choices"][0]["message"]["content"])
        del result["items"][0]["related_message_ids"]
        response["choices"][0]["message"]["content"] = json.dumps(result)
        bulk.save(directory / "attempt.json", {"reserved_usd": "0.01"})
        bulk.save(directory / "receipt.json", {"http_status": 200})
        bulk.atomic_bytes(directory / "response.raw", json.dumps(response).encode())
        bulk.recover(directory)
        self.assertEqual(bulk.read(directory / "state.json")["error"], "Invalid item fields")
        before = {name: (directory / name).read_bytes() for name in
                  ("definition.json", "input.json", "request.json", "response.raw", "attempt.json", "usage.json")}
        report = memory.repair_missing_related(self.output)
        self.assertEqual(len(report["repaired"]), 1)
        self.assertEqual(report["new_api_calls"], 0)
        adapted = bulk.read(directory / "result.json")
        self.assertEqual(adapted["items"][0]["related_message_ids"], [])
        self.assertEqual({k: v for k, v in adapted["items"][0].items() if k != "related_message_ids"}, result["items"][0])
        for name, original in before.items():
            self.assertEqual((directory / name).read_bytes(), original)
        evidence = bulk.read(directory / "memory-empty-related-v1.json")
        self.assertEqual(evidence["prior_state"]["state"], "validation_failed")
        self.assertFalse(evidence["semantic_rewrite"])
        self.assertEqual(memory.repair_missing_related(self.output)["repaired"], [])

    def test_scoped_related_recovery_does_not_accept_extra_fields_or_bad_references(self):
        body = bulk.read(self.profile)
        body["version"] = "memory-personal-v1"
        bulk.save(self.profile, body)
        self.prepare([package()["raw"]])
        store, job, (directory, *_rest) = self.job()
        response = self.response()
        result = json.loads(response["choices"][0]["message"]["content"])
        del result["items"][0]["related_message_ids"]
        result["items"][0]["extra"] = "ignored?"
        response["choices"][0]["message"]["content"] = json.dumps(result)
        bulk.save(directory / "attempt.json", {"reserved_usd": "0.01"})
        bulk.save(directory / "receipt.json", {"http_status": 200})
        bulk.atomic_bytes(directory / "response.raw", json.dumps(response).encode())
        bulk.recover(directory)
        report = memory.repair_missing_related(self.output)
        self.assertEqual(report["repaired"], [])
        self.assertEqual(len(report["unchanged"]), 1)
        self.assertFalse((directory / "result.json").exists())
        del result["items"][0]["extra"]
        result["items"][0]["user_message_ids"] = ["m999"]
        response["choices"][0]["message"]["content"] = json.dumps(result)
        bulk.atomic_bytes(directory / "response.raw", json.dumps(response).encode())
        report = memory.repair_missing_related(self.output)
        self.assertEqual(report["repaired"], [])
        self.assertEqual(report["unchanged"][0]["reason"], "Unknown message reference")
        self.assertFalse((directory / "result.json").exists())


if __name__ == "__main__":
    unittest.main()
