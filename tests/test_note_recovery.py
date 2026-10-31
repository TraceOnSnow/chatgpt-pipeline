"""Recovery contracts exercised entirely with temporary sources and mocked calls."""
from decimal import Decimal
import io
import unittest
from unittest.mock import patch
import test_notes as note_fixture

from chatgpt_pipeline import bulk, notes, note_recovery
from chatgpt_pipeline.verify import encoded, loads


class RecoveryTests(unittest.TestCase):
    # Reuse the established isolated ingestion fixture without inheriting its tests.
    setUp = note_fixture.NoteTests.setUp
    respond_selection = note_fixture.NoteTests.respond_selection
    response = note_fixture.NoteTests.response
    draft = note_fixture.NoteTests.draft
    responses = note_fixture.NoteTests.responses

    def start_failed(self, draft=None, review=None, finish="stop"):
        job = self.workspace.request(self.item)
        self.job_id = job["id"]
        self.job_dir = self.directory / "jobs" / self.job_id
        self.material = bulk.read(self.job_dir / "input.json")
        original = self.draft() if draft is None else draft
        self.save_original("draft", original, finish=finish)
        if review is not None:
            self.save_original("review", review, draft=original)
        self.workspace.state(self.job_id, "attention", "Invalid note fields")
        self.recovery = note_recovery.Recovery(self.workspace)
        return self.job_id

    def save_original(self, stage, result, *, draft=None, finish="stop"):
        directory = self.job_dir / stage
        directory.mkdir()
        payload = self.workspace.payload(self.job_id, stage, self.material, draft)
        response = self.response(result)
        response["choices"][0]["finish_reason"] = finish
        bulk.save(directory / "request.json", payload)
        bulk.atomic_bytes(directory / "response.raw", encoded(response))
        bulk.save(directory / "receipt.json", {"status": 200})
        with self.workspace.db() as db:
            db.execute("INSERT INTO calls(job_id,stage,request_hash,reservation,cost,response_id) "
                       "VALUES(?,?,?,?,?,?)", (self.job_id, stage, bulk.digest(payload),
                                               "0.02", "0.001", "test-response"))

    def original_snapshot(self):
        files = {str(p.relative_to(self.job_dir)): p.read_bytes()
                 for p in self.job_dir.rglob("*") if p.is_file() and p.name != "worker.lock"}
        with self.workspace.db() as db:
            calls = [tuple(row) for row in db.execute(
                "SELECT * FROM calls WHERE job_id=? ORDER BY stage", (self.job_id,))]
        return files, calls

    def assert_original_preserved(self, snapshot):
        files, calls = snapshot
        for name, content in files.items():
            self.assertEqual((self.job_dir / name).read_bytes(), content, name)
        with self.workspace.db() as db:
            original = [tuple(row) for row in db.execute(
                "SELECT * FROM calls WHERE job_id=? AND stage IN ('draft','review') ORDER BY stage",
                (self.job_id,))]
        self.assertEqual(original, calls)

    def response_streams(self, results):
        streams = []
        for result in results:
            stream = io.BytesIO(encoded(result))
            stream.status = 200
            streams.append(stream)
        return streams

    def ready(self):
        return {**self.draft(), "verdict": "ready", "review_reason": "原文依据充分，未把未验证结果写成事实。"}

    def invalid_draft(self):
        return {"title": self.draft()["title"], "markdown": self.draft()["markdown"]}

    def test_planning_is_read_only_and_calls_require_explicit_opt_in(self):
        self.start_failed(draft=self.invalid_draft())
        snapshot = self.original_snapshot()
        before = self.workspace.job(self.job_id)
        with patch("urllib.request.OpenerDirector.open") as post:
            plan = self.recovery.plan(self.job_id)
            self.assertEqual(plan["max_new_calls"], 2)
            self.assertEqual((plan["draft"], plan["review"]), ("call", "call"))
            with self.assertRaisesRegex(ValueError, "explicit --call"):
                self.recovery.prepare(self.job_id)
        post.assert_not_called()
        self.assertEqual(self.workspace.job(self.job_id), before)
        self.assertFalse((self.job_dir / "recovery-v1.json").exists())
        self.assert_original_preserved(snapshot)

    def test_valid_original_draft_reused_and_only_new_review_is_billed(self):
        # The old review omitted its required verdict; this must not imply ready.
        self.start_failed(review=self.draft())
        snapshot = self.original_snapshot()
        self.assertEqual(self.recovery.plan(self.job_id)["max_new_calls"], 1)
        self.recovery.prepare(self.job_id, allow_calls=True)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()[1:]) as post:
            result = self.recovery.run(self.job_id)
            self.assertEqual(result["state"], "done", result)
            self.recovery.prepare(self.job_id, allow_calls=True)
            self.recovery.run(self.job_id)
        self.assertEqual(post.call_count, 1)
        self.assertFalse((self.job_dir / "recovery-v1-draft").exists())
        self.assertTrue((self.job_dir / "recovery-v1-draft-reuse.json").exists())
        self.assertEqual(self.workspace.usage()["calls"], 3)
        self.assertEqual(self.workspace.usage()["actual_usd"], "0.003")
        self.assert_original_preserved(snapshot)
        payload = bulk.read(self.job_dir / "recovery-v1-review/request.json")
        self.assertEqual(payload["max_tokens"], 8000)
        data = bulk.read(self.job_dir / "recovery-v1-review/result.json")
        self.assertEqual(data["verdict"], "ready")

    def test_exact_original_fence_repaired_offline_then_independent_review(self):
        original = self.draft()
        original["markdown"] = original["markdown"].replace(
            "[[CODE:c001]]", self.material_code_from_new_job())
        self.start_failed(draft=original)
        snapshot = self.original_snapshot()
        plan = self.recovery.plan(self.job_id)
        self.assertEqual((plan["draft"], plan["review"], plan["max_new_calls"]), ("reuse", "call", 1))
        self.recovery.prepare(self.job_id, allow_calls=True)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()[1:]) as post:
            result = self.recovery.run(self.job_id)
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(post.call_count, 1)
        reused = bulk.read(self.job_dir / "recovery-v1-draft-reuse.json")
        self.assertTrue(any(c["operation"] == "replace_exact_code_block" for c in reused["changes"]))
        self.assertIn("[[CODE:c001]]", reused["result"]["markdown"])
        self.assertIn(b"```py\nx = 1\n```", self.workspace.note(self.item)[1])
        self.assert_original_preserved(snapshot)

    def material_code_from_new_job(self):
        # Preparing an item is deterministic and does not call a model.
        job = self.workspace.request(self.item)
        material = bulk.read(self.directory / "jobs" / job["id"] / "input.json")
        return material["codes"]["c001"]["text"]

    def test_missing_source_metadata_requires_fresh_draft_and_separate_review(self):
        self.start_failed(draft=self.invalid_draft())
        snapshot = self.original_snapshot()
        self.recovery.prepare(self.job_id, allow_calls=True)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()) as post:
            result = self.recovery.run(self.job_id)
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(post.call_count, 2)
        draft_payload = bulk.read(self.job_dir / "recovery-v1-draft/request.json")
        review_payload = bulk.read(self.job_dir / "recovery-v1-review/request.json")
        draft_input = loads(draft_payload["messages"][1]["content"])
        review_input = loads(review_payload["messages"][1]["content"])
        self.assertNotIn("draft", draft_input)
        self.assertEqual(review_input["draft"], self.draft())
        self.assertEqual(review_input["processing_stage"], "review")
        self.assertEqual(set(review_input["required_output_fields"]), set(self.ready()))
        self.assertNotIn("```py", draft_input["conversation"])
        self.assertIn("[[CODE:c001]]", draft_input["conversation"])
        self.assertEqual(draft_input["code_blocks"]["c001"]["text"], self.material["codes"]["c001"]["text"])
        self.assert_original_preserved(snapshot)

    def test_original_budget_ledger_prevents_new_recovery_call(self):
        self.start_failed(draft=self.invalid_draft())
        with self.workspace.db() as db:
            db.execute("INSERT INTO calls(job_id,stage,request_hash,reservation,cost) "
                       "VALUES('other','draft','hash','1.999','1.999')")
        self.recovery.prepare(self.job_id, allow_calls=True)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open") as post:
            result = self.recovery.run(self.job_id)
        post.assert_not_called()
        self.assertEqual(result["state"], "attention")
        self.assertIn("budget", result["error"])
        self.assertEqual(self.workspace.usage()["actual_usd"], "2.000")
        self.assertEqual(self.workspace.usage()["calls"], 2)

    def test_original_unknown_attempt_without_response_is_never_resent(self):
        self.start_failed(draft=self.invalid_draft())
        (self.job_dir / "draft/response.raw").unlink()
        with self.workspace.db() as db:
            db.execute("UPDATE calls SET cost=NULL WHERE job_id=?", (self.job_id,))
        with patch("urllib.request.OpenerDirector.open") as post:
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "needs_reconciliation"):
                    self.recovery.plan(self.job_id)
                with self.assertRaisesRegex(ValueError, "needs_reconciliation"):
                    self.recovery.prepare(self.job_id, allow_calls=True)
        post.assert_not_called()
        self.assertEqual(self.workspace.usage()["calls"], 1)
        self.assertEqual(self.workspace.usage()["reserved_usd"], "0.02")

    def test_recovery_timeout_keeps_reservation_and_cannot_trigger_retry(self):
        self.start_failed(draft=self.invalid_draft())
        self.recovery.prepare(self.job_id, allow_calls=True)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError) as post:
            first = self.recovery.run(self.job_id)
            self.recovery.prepare(self.job_id, allow_calls=True)
            second = self.recovery.run(self.job_id)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(first["state"], "attention")
        self.assertEqual(second["state"], "attention")
        self.assertEqual(self.workspace.usage()["unknown_cost_calls"], 1)
        self.assertGreater(Decimal(self.workspace.usage()["reserved_usd"]), 0)
        self.assertFalse((self.job_dir / "recovery-v1-draft/response.raw").exists())

    def test_truncated_recovery_response_cannot_become_a_draft(self):
        self.start_failed(draft=self.invalid_draft())
        self.recovery.prepare(self.job_id, allow_calls=True)
        response = self.response(self.draft())
        response["choices"][0]["finish_reason"] = "length"
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.response_streams([response])) as post:
            result = self.recovery.run(self.job_id)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["state"], "attention")
        self.assertIn("Truncated", result["error"])
        self.assertFalse((self.job_dir / "note.md").exists())
        self.assertFalse((self.job_dir / "recovery-v1-review").exists())

    def test_review_without_verdict_is_not_repaired_or_approved(self):
        self.start_failed(review=self.draft())
        self.recovery.prepare(self.job_id, allow_calls=True)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.response_streams([
                 self.response(self.draft())])) as post:
            result = self.recovery.run(self.job_id)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["state"], "attention")
        self.assertIn("Invalid note fields", result["error"])
        self.assertFalse((self.job_dir / "note.md").exists())

    def test_semantic_review_rejection_is_not_overridden_by_recovery(self):
        self.start_failed(review=self.draft())
        self.recovery.prepare(self.job_id, allow_calls=True)
        rejected = {**self.ready(), "verdict": "needs_review", "review_reason": "原文缺少个人经历的关键依据。"}
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.response_streams([
                 self.response(rejected)])) as post:
            result = self.recovery.run(self.job_id)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["state"], "attention")
        self.assertIn("Semantic review:", result["error"])
        self.assertEqual(bulk.read(self.job_dir / "recovery-v1-review/result.json")["verdict"], "needs_review")
        self.assertFalse((self.job_dir / "note.md").exists())

    def test_changed_source_after_preparation_blocks_all_calls(self):
        self.start_failed(draft=self.invalid_draft())
        self.recovery.prepare(self.job_id, allow_calls=True)
        from pathlib import Path
        Path(self.material["source"]["source_path"]).write_bytes(b"changed isolated source")
        with patch("urllib.request.OpenerDirector.open") as post:
            result = self.recovery.run(self.job_id)
        post.assert_not_called()
        self.assertEqual(result["state"], "attention")
        self.assertIn("Source bytes changed", result["error"])

    def test_original_artifact_changed_after_preparation_blocks_all_calls(self):
        self.start_failed(draft=self.invalid_draft())
        self.recovery.prepare(self.job_id, allow_calls=True)
        (self.job_dir / "draft/request.json").write_bytes(b"changed original request")
        with patch("urllib.request.OpenerDirector.open") as post:
            result = self.recovery.run(self.job_id)
        post.assert_not_called()
        self.assertEqual(result["state"], "attention")
        self.assertIn("Original attempt changed", result["error"])

    def test_existing_edited_note_is_preserved_before_and_after_preparation(self):
        self.start_failed(review=self.ready())
        target = self.job_dir / "note.md"
        target.write_text("user edited existing note")
        with self.assertRaisesRegex(ValueError, "Existing note preserved"):
            self.recovery.prepare(self.job_id)
        target.unlink()
        # Both original stages validate, so this is a fully offline recovery.
        self.assertEqual(self.recovery.plan(self.job_id)["max_new_calls"], 0)
        self.recovery.prepare(self.job_id)
        target.write_text("user edited during preparation")
        with patch("urllib.request.OpenerDirector.open") as post:
            result = self.recovery.run(self.job_id)
        post.assert_not_called()
        self.assertEqual(result["state"], "attention")
        self.assertEqual(target.read_text(), "user edited during preparation")
        self.assertIn("edited", result["error"])

    def test_custom_stage_reservation_uses_its_own_output_cap(self):
        self.start_failed(draft=self.invalid_draft())
        payload = note_recovery.recovery_payload("draft", self.material, None)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.workspace.stage(self.job_id, "recovery-v1-draft", self.material, None, None,
                                     payload=payload, validation_stage="draft")
        cap = bulk.PROVIDER["max_price"]
        expected = (Decimal(len(encoded(payload))) * Decimal(str(cap["prompt"])) +
                    Decimal(8000) * Decimal(str(cap["completion"]))) / Decimal(1000000) * Decimal("1.1")
        with self.workspace.db() as db:
            call = db.execute("SELECT reservation FROM calls WHERE job_id=? AND stage='recovery-v1-draft'",
                              (self.job_id,)).fetchone()
        self.assertEqual(Decimal(call["reservation"]), expected)
        self.assertEqual(bulk.read(self.job_dir / "recovery-v1-draft/attempt.json")["max_attempts"], 1)

    def test_prepared_journal_before_queue_can_be_explicitly_resumed(self):
        self.start_failed(draft=self.invalid_draft())
        self.recovery.prepare(self.job_id, allow_calls=True)
        # The receipt was written but the process died before the queue update.
        self.workspace.state(self.job_id, "attention", "Invalid note fields")
        receipt = (self.job_dir / "recovery-v1.json").read_bytes()
        with patch("urllib.request.OpenerDirector.open") as post:
            plan = self.recovery.plan(self.job_id)
            self.assertTrue(plan["eligible"])
            self.assertTrue(plan["resume"])
            self.assertEqual(self.recovery.prepare(self.job_id, allow_calls=True)["state"], "queued")
        post.assert_not_called()
        self.assertEqual((self.job_dir / "recovery-v1.json").read_bytes(), receipt)

    def test_saved_recovery_responses_survive_process_death_before_finish(self):
        self.start_failed(draft=self.invalid_draft())
        snapshot = self.original_snapshot()
        self.recovery.prepare(self.job_id, allow_calls=True)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()) as post:
            with patch.object(self.workspace, "finish", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.recovery.run(self.job_id)
            raw_before = {stage: (self.job_dir / stage / "response.raw").read_bytes()
                          for stage in ("recovery-v1-draft", "recovery-v1-review")}
            self.assertEqual(self.workspace.job(self.job_id)["state"], "running")
            self.recovery.prepare(self.job_id, allow_calls=True)
            result = self.recovery.run(self.job_id)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result["state"], "done", result)
        for stage, raw in raw_before.items():
            self.assertEqual((self.job_dir / stage / "response.raw").read_bytes(), raw)
        self.assert_original_preserved(snapshot)

    def test_raw_saved_before_receipt_is_reused_without_duplicate_charge(self):
        self.start_failed(draft=self.invalid_draft())
        self.recovery.prepare(self.job_id, allow_calls=True)
        original_save = bulk.save
        receipt_target = self.job_dir / "recovery-v1-draft/receipt.json"

        def crash_at_receipt(destination, value):
            if destination == receipt_target:
                raise KeyboardInterrupt
            return original_save(destination, value)

        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()) as post:
            with patch.object(bulk, "save", side_effect=crash_at_receipt):
                with self.assertRaises(KeyboardInterrupt):
                    self.recovery.run(self.job_id)
            self.assertTrue((receipt_target.parent / "response.raw").exists())
            self.assertFalse(receipt_target.exists())
            self.assertEqual(self.workspace.usage()["unknown_cost_calls"], 1)
            result = self.recovery.run(self.job_id)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(self.workspace.usage()["calls"], 3)
        self.assertEqual(self.workspace.usage()["unknown_cost_calls"], 0)

    def test_completed_note_is_reconciled_after_final_receipt_write_crash(self):
        self.start_failed(review=self.draft())
        self.recovery.prepare(self.job_id, allow_calls=True)
        original_save = bulk.save
        journal_target = self.job_dir / "recovery-v1.json"

        def crash_at_done_receipt(destination, value):
            if destination == journal_target and value.get("state") == "done":
                raise KeyboardInterrupt
            return original_save(destination, value)

        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()[1:]) as post:
            with patch.object(bulk, "save", side_effect=crash_at_done_receipt):
                with self.assertRaises(KeyboardInterrupt):
                    self.recovery.run(self.job_id)
            self.assertEqual(self.workspace.job(self.job_id)["state"], "done")
            self.assertEqual(bulk.read(journal_target)["state"], "prepared")
            note_before = self.workspace.note(self.item)
            result = self.recovery.run(self.job_id)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["state"], "done")
        self.assertEqual(bulk.read(journal_target)["state"], "done")
        self.assertEqual(self.workspace.note(self.item), note_before)

    def test_completed_note_edited_after_receipt_crash_cannot_be_reconciled_as_original(self):
        self.start_failed(review=self.ready())
        self.recovery.prepare(self.job_id)
        original_save = bulk.save
        journal_target = self.job_dir / "recovery-v1.json"

        def crash_at_done_receipt(destination, value):
            if destination == journal_target and value.get("state") == "done":
                raise KeyboardInterrupt
            return original_save(destination, value)

        with patch.object(bulk, "save", side_effect=crash_at_done_receipt):
            with self.assertRaises(KeyboardInterrupt):
                self.recovery.run(self.job_id)
        target = self.job_dir / "note.md"
        target.write_text("an edit after successful completion")
        with patch("urllib.request.OpenerDirector.open") as post:
            with self.assertRaisesRegex(ValueError, "Completed note differs"):
                self.recovery.run(self.job_id)
        post.assert_not_called()
        self.assertEqual(target.read_text(), "an edit after successful completion")
        self.assertEqual(bulk.read(journal_target)["state"], "prepared")

    def test_payload_keeps_identical_code_aliases_in_their_source_message(self):
        self.start_failed(draft=self.invalid_draft())
        from copy import deepcopy
        material = deepcopy(self.material)
        code = material["codes"]["c001"]["text"]
        material["messages"].append({"id": "m002", "source_message_id": "another-source",
                                     "role": "assistant", "text": "当时的助手也给出了这段代码：\n" + code})
        material["codes"]["c002"] = {"message_id": "m002", "text": code}
        payload = note_recovery.recovery_payload("draft", material, None)
        conversation = loads(payload["messages"][1]["content"])["conversation"]
        first, second = conversation.split("[m002 assistant]\n")
        self.assertIn("[[CODE:c001]]", first)
        self.assertNotIn("[[CODE:c002]]", first)
        self.assertIn("[[CODE:c002]]", second)
        self.assertNotIn("[[CODE:c001]]", second)

    def test_edited_note_after_preparation_blocks_before_any_paid_stage(self):
        self.start_failed(draft=self.invalid_draft())
        self.recovery.prepare(self.job_id, allow_calls=True)
        target = self.job_dir / "note.md"
        target.write_text("a concurrent user edit must not be overwritten")
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()) as post:
            result = self.recovery.run(self.job_id)
        post.assert_not_called()
        self.assertEqual(result["state"], "attention")
        self.assertEqual(target.read_text(), "a concurrent user edit must not be overwritten")
        self.assertEqual(self.workspace.usage()["calls"], 1)

    def test_structured_round_reuses_valid_v1_draft_after_missing_review_verdict(self):
        self.start_failed(draft=self.invalid_draft())
        self.recovery.prepare(self.job_id, allow_calls=True)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.response_streams([
                 self.response(self.draft()), self.response(self.draft())])) as first_post:
            first = self.recovery.run(self.job_id)
        self.assertEqual(first_post.call_count, 2)
        self.assertEqual(first["state"], "attention")
        self.assertIn("Invalid note fields", first["error"])
        files, calls = self.original_snapshot()
        structured = note_recovery.StructuredRecovery(self.workspace)
        plan = structured.plan(self.job_id)
        self.assertEqual(plan["revision"], "recovery-schema-v2")
        self.assertEqual(plan["draft_source"], "recovery-v1-draft")
        self.assertEqual((plan["draft"], plan["review"], plan["max_new_calls"]), ("reuse", "call", 1))
        structured.prepare(self.job_id, allow_calls=True)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()[1:]) as post:
            result = structured.run(self.job_id)
            structured.prepare(self.job_id, allow_calls=True)
            structured.run(self.job_id)
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(post.call_count, 1)
        self.assertFalse((self.job_dir / "recovery-schema-v2-draft").exists())
        self.assertTrue((self.job_dir / "recovery-schema-v2-draft-reuse.json").exists())
        for name, content in files.items():
            self.assertEqual((self.job_dir / name).read_bytes(), content, name)
        with self.workspace.db() as db:
            old_calls = [tuple(row) for row in db.execute(
                "SELECT * FROM calls WHERE job_id=? AND stage NOT LIKE 'recovery-schema-v2-%' ORDER BY stage",
                (self.job_id,))]
        self.assertEqual(old_calls, calls)
        payload = bulk.read(self.job_dir / "recovery-schema-v2-review/request.json")
        schema = payload["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        self.assertEqual(set(schema["schema"]["required"]), set(self.ready()))
        self.assertFalse(schema["schema"]["additionalProperties"])
        self.assertEqual(schema["schema"]["properties"]["source_message_ids"]["items"]["enum"], ["m001"])
        self.assertEqual(schema["schema"]["properties"]["verbatim_code_ids"]["items"]["enum"], ["c001"])
        self.assertEqual(self.workspace.usage()["calls"], 4)

    def test_structured_provider_response_still_requires_actual_source_references(self):
        self.start_failed(draft=self.invalid_draft())
        structured = note_recovery.StructuredRecovery(self.workspace)
        self.assertEqual(structured.plan(self.job_id)["max_new_calls"], 2)
        structured.prepare(self.job_id, allow_calls=True)
        missing_refs = self.draft()
        del missing_refs["source_message_ids"]
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.response_streams([
                 self.response(missing_refs)])) as post:
            result = structured.run(self.job_id)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["state"], "attention")
        self.assertIn("Invalid note fields", result["error"])
        self.assertFalse((self.job_dir / "note.md").exists())
        self.assertFalse((self.job_dir / "recovery-schema-v2-review").exists())

    def test_explicit_vendor_contract_retains_model_price_caps_and_never_falls_back(self):
        self.start_failed(draft=self.invalid_draft())
        payload = note_recovery.RoutedRecovery.make_payload("draft", self.material, None)
        self.assertEqual(payload["model"], bulk.MODEL)
        self.assertEqual(payload["provider"]["max_price"], bulk.PROVIDER["max_price"])
        self.assertEqual(payload["provider"]["ignore"], bulk.PROVIDER["ignore"])
        self.assertEqual(payload["provider"]["order"], ["deepinfra"])
        self.assertFalse(payload["provider"]["allow_fallbacks"])
        self.assertNotIn("order", bulk.PROVIDER)
        self.assertEqual(payload["response_format"]["type"], "json_schema")

    def test_cli_structured_planning_does_not_prepare_or_charge_a_round(self):
        from chatgpt_pipeline import cli
        self.start_failed(draft=self.invalid_draft())
        snapshot = self.original_snapshot()
        output = io.StringIO()
        with patch("sys.argv", ["chatgpt-pipeline", "notes-recover", "--workspace", str(self.directory),
                                "--job", self.job_id, "--structured"]), \
             patch("sys.stdout", output), patch("urllib.request.OpenerDirector.open") as post:
            cli.main()
        post.assert_not_called()
        plan = loads(output.getvalue())
        self.assertFalse(plan["applied"])
        self.assertEqual(plan["plans"][0]["revision"], "recovery-body-v4")
        self.assertEqual(plan["plans"][0]["max_new_calls"], 2)
        self.assertFalse((self.job_dir / "recovery-body-v4.json").exists())
        self.assertEqual(self.workspace.job(self.job_id)["state"], "attention")
        self.assert_original_preserved(snapshot)

    def test_code_safe_round_completes_two_strict_stages_and_preserves_old_contract(self):
        self.start_failed(draft=self.invalid_draft())
        snapshot = self.original_snapshot()
        safe = note_recovery.CodeSafeRecovery(self.workspace)
        self.assertEqual(safe.plan(self.job_id)["max_new_calls"], 2)
        safe.prepare(self.job_id, allow_calls=True)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()) as post:
            result = safe.run(self.job_id)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result["state"], "done", result)
        self.assert_original_preserved(snapshot)
        for stage in ("draft", "review"):
            payload = bulk.read(self.job_dir / f"recovery-code-v3-{stage}/request.json")
            schema = payload["response_format"]["json_schema"]["schema"]
            self.assertIn("pattern", schema["properties"]["markdown"])
            old_round = note_recovery.StructuredRecovery.make_payload(stage, self.material,
                                                                     None if stage == "draft" else self.draft())
            old_schema = old_round["response_format"]["json_schema"]["schema"]
            self.assertNotIn("pattern", old_schema["properties"]["markdown"])
        self.assertIn(b"```py\nx = 1\n```", self.workspace.note(self.item)[1])

    def test_code_safe_round_does_not_approve_provider_novel_fenced_code(self):
        self.start_failed(draft=self.invalid_draft())
        safe = note_recovery.CodeSafeRecovery(self.workspace)
        safe.prepare(self.job_id, allow_calls=True)
        fabricated = self.ready()
        fabricated["markdown"] += "\n\n```novel_call_without_source()```"
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.response_streams([
                 self.response(self.draft()), self.response(fabricated)])) as post:
            result = safe.run(self.job_id)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result["state"], "attention")
        self.assertIn("Use verified code placeholders", result["error"])
        self.assertTrue((self.job_dir / "recovery-code-v3-review/response.raw").exists())
        self.assertFalse((self.job_dir / "note.md").exists())
        self.assertFalse((self.job_dir / "recovery-code-v3-review/result.json").exists())

    def test_complete_recovery_schema_requires_body_and_retains_inline_code(self):
        import re
        self.start_failed(draft=self.invalid_draft())
        for stage in ("draft", "review"):
            draft = None if stage == "draft" else self.draft()
            payload = note_recovery.CompleteRecovery.make_payload(stage, self.material, draft)
            pattern = payload["response_format"]["json_schema"]["schema"]["properties"]["markdown"]["pattern"]
            for markdown in ("# " + "只有标题" * 10,
                             "# " + "只有标题" * 10 + "\n",
                             "# 长标题\n\n  \t \r\n",
                             "# 正文不得含围栏\n\n```novel()```\n"):
                with self.subTest(stage=stage, rejected=markdown):
                    self.assertIsNone(re.search(pattern, markdown))
            for markdown in ("# 中文标题\n\n这是正文，并非只有标题。",
                             "# 多行\r\n\r\n保留背景。\n再说明具体经过。\n",
                             "# 行内代码\n\n用 `x` 和 ``包含 ` 的文本`` 解释原始想法。",
                             "# 保留来源代码\n\n[[CODE:c001]]"):
                with self.subTest(stage=stage, accepted=markdown):
                    self.assertIsNotNone(re.search(pattern, markdown))
            # The prior sent contract bans fences but still permits heading-only output.
            previous = note_recovery.CodeSafeRecovery.make_payload(stage, self.material, draft)
            previous_pattern = previous["response_format"]["json_schema"]["schema"]["properties"]["markdown"]["pattern"]
            self.assertIsNotNone(re.search(previous_pattern, "# " + "只有标题" * 10))

    def finish_empty_generated_note(self, *, calibration=True):
        self.start_failed(draft=self.invalid_draft())
        result = {**self.ready(), "markdown": "# " + "很长的标题仍然不能替代实际正文" * 4,
                  "verbatim_code_ids": []}
        # Reproduce output accepted by the previous validator without bypassing
        # any current model stage or touching a real workspace.
        self.workspace.finish(self.job_id, result, self.material,
                              {"type": "bounded-model-recovery", "revision": "recovery-code-v3"}
                              if calibration else None)
        self.assertEqual(self.workspace.job(self.job_id)["state"], "done")

    def test_empty_generated_note_quarantine_plan_and_apply_preserve_all_attempts(self):
        self.finish_empty_generated_note()
        before_job = self.workspace.job(self.job_id)
        before_usage = self.workspace.usage()
        files, calls = self.original_snapshot()
        archive = self.job_dir / "empty-body-audit-v1"
        with patch("urllib.request.OpenerDirector.open") as post:
            plan = self.recovery.quarantine_empty(self.job_id)
            self.assertTrue(plan["eligible"])
            self.assertFalse(plan["quarantined"])
            self.assertEqual(plan["new_calls"], 0)
            self.assertFalse(archive.exists())
            self.assertEqual(self.workspace.job(self.job_id), before_job)
            self.assert_original_preserved((files, calls))
            applied = self.recovery.quarantine_empty(self.job_id, apply=True)
            repeated = self.recovery.quarantine_empty(self.job_id, apply=True)
        post.assert_not_called()
        self.assertTrue(applied["quarantined"])
        self.assertTrue(repeated["quarantined"])
        self.assertEqual(self.workspace.job(self.job_id)["state"], "attention")
        self.assertIn("empty after title", self.workspace.job(self.job_id)["error"])
        self.assertEqual(self.workspace.usage(), before_usage)
        self.assertEqual(bulk.read(archive / "journal.json")["state"], "applied")
        for name, content in files.items():
            target = archive / name if name in ("note.md", "result.json") else self.job_dir / name
            self.assertEqual(target.read_bytes(), content, name)
        self.assertFalse((self.job_dir / "note.md").exists())
        self.assertFalse((self.job_dir / "result.json").exists())

    def test_empty_note_quarantine_refuses_edited_file(self):
        self.finish_empty_generated_note()
        target = self.job_dir / "note.md"
        target.write_text("an existing user edit must be kept")
        before = self.workspace.usage()
        with patch("urllib.request.OpenerDirector.open") as post:
            for apply in (False, True):
                with self.assertRaisesRegex(ValueError, "Existing note was edited"):
                    self.recovery.quarantine_empty(self.job_id, apply=apply)
        post.assert_not_called()
        self.assertEqual(target.read_text(), "an existing user edit must be kept")
        self.assertEqual(self.workspace.job(self.job_id)["state"], "done")
        self.assertEqual(self.workspace.usage(), before)
        self.assertFalse((self.job_dir / "empty-body-audit-v1").exists())

    def test_empty_note_quarantine_refuses_existing_import_receipt(self):
        self.finish_empty_generated_note()
        receipt_dir = self.directory / "vault-imports" / ("a" * 64)
        receipt_dir.mkdir(parents=True)
        bulk.save(receipt_dir / f"{self.item}.json", {"outcome": "created", "id": "existing-library-note"})
        snapshot = self.original_snapshot()
        with patch("urllib.request.OpenerDirector.open") as post:
            for apply in (False, True):
                with self.assertRaisesRegex(ValueError, "already imported"):
                    self.recovery.quarantine_empty(self.job_id, apply=apply)
        post.assert_not_called()
        self.assert_original_preserved(snapshot)
        self.assertEqual(self.workspace.job(self.job_id)["state"], "done")
        self.assertFalse((self.job_dir / "empty-body-audit-v1").exists())

    def test_empty_note_quarantine_is_scoped_to_this_recovery_generated_output(self):
        self.finish_empty_generated_note(calibration=False)
        snapshot = self.original_snapshot()
        with self.assertRaisesRegex(ValueError, "Only this recovery"):
            self.recovery.quarantine_empty(self.job_id, apply=True)
        self.assert_original_preserved(snapshot)
        self.assertEqual(self.workspace.job(self.job_id)["state"], "done")

    def test_empty_note_quarantine_preserves_generated_note_with_actual_body(self):
        self.start_failed(draft=self.invalid_draft())
        self.workspace.finish(self.job_id, self.ready(), self.material,
                              {"type": "bounded-model-recovery", "revision": "recovery-code-v3"})
        snapshot = self.original_snapshot()
        with self.assertRaisesRegex(ValueError, "Note has a body"):
            self.recovery.quarantine_empty(self.job_id, apply=True)
        self.assert_original_preserved(snapshot)
        self.assertEqual(self.workspace.job(self.job_id)["state"], "done")

    def test_empty_note_quarantine_resumes_journal_after_first_file_move(self):
        from pathlib import Path
        self.finish_empty_generated_note()
        before_usage = self.workspace.usage()
        note = (self.job_dir / "note.md").read_bytes()
        result = (self.job_dir / "result.json").read_bytes()
        original_rename = Path.rename

        def stop_before_second_move(source, target):
            if source == self.job_dir / "result.json":
                raise OSError("simulated process interruption between archive moves")
            return original_rename(source, target)

        with patch.object(Path, "rename", new=stop_before_second_move):
            with self.assertRaisesRegex(OSError, "between archive moves"):
                self.recovery.quarantine_empty(self.job_id, apply=True)
        archive = self.job_dir / "empty-body-audit-v1"
        self.assertEqual(bulk.read(archive / "journal.json")["state"], "prepared")
        self.assertEqual((archive / "note.md").read_bytes(), note)
        self.assertFalse((self.job_dir / "note.md").exists())
        self.assertEqual((self.job_dir / "result.json").read_bytes(), result)
        self.assertEqual(self.workspace.job(self.job_id)["state"], "done")
        with self.assertRaisesRegex(ValueError, "explicit --apply"):
            self.recovery.quarantine_empty(self.job_id)
        with patch("urllib.request.OpenerDirector.open") as post:
            recovered = self.recovery.quarantine_empty(self.job_id, apply=True)
        post.assert_not_called()
        self.assertTrue(recovered["quarantined"])
        self.assertEqual((archive / "note.md").read_bytes(), note)
        self.assertEqual((archive / "result.json").read_bytes(), result)
        self.assertEqual(bulk.read(archive / "journal.json")["state"], "applied")
        self.assertEqual(self.workspace.job(self.job_id)["state"], "attention")
        self.assertEqual(self.workspace.usage(), before_usage)


if __name__ == "__main__":
    unittest.main()
