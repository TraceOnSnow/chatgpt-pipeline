"""Content-first execution, durable billing and readable review drafts: no live APIs."""
import io
import unittest
from unittest.mock import patch

import test_notes as fixtures
from chatgpt_pipeline import bulk, notes, note_content
from chatgpt_pipeline.verify import encoded


class SimulatedDeath(BaseException):
    pass


class ContentPipelineTests(unittest.TestCase):
    respond_selection = fixtures.NoteTests.respond_selection
    response = fixtures.NoteTests.response

    def setUp(self):
        fixtures.NoteTests.setUp(self)
        default = patch.object(notes, "DEFAULT_EXECUTION_REVISION", note_content.REVISION)
        default.start()
        self.addCleanup(default.stop)
        self.job_id = self.workspace.request(self.item)["id"]
        self.job_dir = self.directory / "jobs" / self.job_id
        self.markdown = "# 我的实验\n\n我试过两次，结果仍未确定。\n\n```py\nx = 1\n```\n"

    def wire(self, content, *, finish="stop"):
        response = self.response({})
        response["choices"][0]["message"]["content"] = content
        response["choices"][0]["finish_reason"] = finish
        stream = io.BytesIO(encoded(response))
        stream.status = 200
        return stream

    def review_wire(self, *, markdown=None, verdict="ready"):
        return self.wire(encoded({"markdown": markdown or self.markdown, "verdict": verdict,
                                  "reason": "保留了尝试与尚未确定的状态。"}).decode())

    def execute(self, responses):
        with patch.object(notes, "credential", return_value="synthetic-key"), \
                patch("urllib.request.OpenerDirector.open", side_effect=responses) as post:
            result = self.workspace.run(self.job_id)
        return result, post.call_count

    def test_plain_markdown_needs_no_machine_fields_and_derives_metadata(self):
        result, calls = self.execute([self.wire(self.markdown), self.review_wire()])
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(calls, 2)
        self.assertTrue(self.workspace.uses_content(self.job_id))
        definition = bulk.read(self.job_dir / "definition.json")
        self.assertEqual(definition["identity"]["content_configuration"], note_content.configuration())
        self.assertNotIn("response_format", bulk.read(self.job_dir / "draft/request.json"))
        saved = bulk.read(self.job_dir / "result.json")
        self.assertEqual(saved["title"], "我的实验")
        self.assertEqual(saved["provenance"]["source_context_message_ids"], ["m1"])
        self.assertEqual(saved["provenance"]["scope"], "input-context-only")
        self.assertEqual(saved["code_blocks"][0]["source_matches"][0]["source_message_id"], "m1")
        self.assertEqual(self.workspace.review_status(self.item)["status"], "passed")
        self.assertEqual(self.workspace.note(self.item)[1], self.markdown.encode())
        self.assertEqual(self.workspace.usage()["actual_usd"], "0.002")
        _, calls = self.execute([])
        self.assertEqual(calls, 0)

    def test_invalid_review_keeps_readable_draft_without_inventing_approval(self):
        result, calls = self.execute([self.wire(self.markdown), self.wire('{"markdown":"only"}')])
        self.assertEqual(result["state"], "review", result)
        self.assertEqual(calls, 2)
        self.assertEqual(self.workspace.note(self.item)[1], self.markdown.encode())
        status = self.workspace.review_status(self.item)
        self.assertEqual(status["status"], "unavailable")
        self.assertNotIn("model_verdict", status)
        self.assertEqual(self.workspace.list("review")["total"], 1)
        self.assertEqual(self.execute([])[1], 0)

    def test_review_timeout_preserves_draft_and_unknown_cost_without_retry(self):
        result, calls = self.execute([self.wire(self.markdown), TimeoutError("private URL")])
        self.assertEqual(result["state"], "review", result)
        self.assertEqual(calls, 2)
        self.assertEqual(self.workspace.usage()["unknown_cost_calls"], 1)
        self.assertNotIn("private URL", str(self.workspace.review_status(self.item)))
        self.assertEqual(self.workspace.note(self.item)[1], self.markdown.encode())
        self.assertEqual(self.execute([])[1], 0)

    def test_semantic_needs_review_saves_corrected_body_and_original_draft(self):
        corrected = self.markdown.replace("我试过两次", "我曾提出两次实验的构想")
        result, _ = self.execute([self.wire(self.markdown), self.review_wire(markdown=corrected, verdict="needs_review")])
        self.assertEqual(result["state"], "review", result)
        self.assertEqual(self.workspace.note(self.item)[1], corrected.encode())
        self.assertEqual((self.job_dir / "draft.md").read_text(), self.markdown)
        self.assertEqual(self.workspace.review_status(self.item)["model_verdict"], "needs_review")

    def test_new_code_is_readable_with_warning_not_marked_verified(self):
        changed = self.markdown.replace("x = 1", "x = 2")
        result, _ = self.execute([self.wire(changed), self.review_wire(markdown=changed)])
        self.assertEqual(result["state"], "review", result)
        self.assertEqual(self.workspace.note(self.item)[1], changed.encode())
        saved = bulk.read(self.job_dir / "result.json")
        self.assertEqual(saved["verdict"], "ready")
        self.assertEqual(saved["code_blocks"][0]["source_matches"], [])
        status = self.workspace.review_status(self.item)
        self.assertEqual(status["status"], "needs_review")
        self.assertIn("未与原文逐字匹配", status["warnings"][0])

    def test_empty_draft_is_a_real_failure_not_a_finished_note(self):
        result, calls = self.execute([self.wire("# 只有标题")])
        self.assertEqual(result["state"], "attention", result)
        self.assertEqual(calls, 1)
        self.assertFalse((self.job_dir / "note.md").exists())

    def test_truncated_draft_is_a_real_failure_not_a_finished_note(self):
        result, calls = self.execute([self.wire(self.markdown, finish="length")])
        self.assertEqual(result["state"], "attention", result)
        self.assertEqual(calls, 1)
        self.assertFalse((self.job_dir / "note.md").exists())

    def test_budget_block_during_review_keeps_draft_without_second_charge(self):
        draft_response = self.wire(self.markdown)
        original_response = self.workspace.response

        def response(job_id, stage, payload, key_file):
            if stage == "review":
                raise ValueError("Workspace budget reached; no request sent")
            return original_response(job_id, stage, payload, key_file)

        with patch.object(self.workspace, "response", side_effect=response):
            result, calls = self.execute([draft_response])
        self.assertEqual(result["state"], "review", result)
        self.assertEqual(calls, 1)
        self.assertEqual(self.workspace.usage()["calls"], 1)
        self.assertEqual(self.workspace.note(self.item)[1], self.markdown.encode())

    def test_crash_after_saved_draft_resumes_raw_response_without_duplicate_call(self):
        with patch.object(self.workspace, "response", wraps=self.workspace.response) as response:
            transport = self.workspace.response._mock_wraps

            def crash_at_review(job_id, stage, payload, key_file):
                if stage == "review":
                    raise SimulatedDeath()
                return transport(job_id, stage, payload, key_file)

            response.side_effect = crash_at_review
            with self.assertRaises(SimulatedDeath):
                self.execute([self.wire(self.markdown)])
        self.assertEqual(self.workspace.job(self.job_id)["state"], "running")
        self.assertEqual((self.job_dir / "note.md").read_text(), self.markdown)
        result, calls = self.execute([self.review_wire()])
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(calls, 1)
        self.assertEqual(self.workspace.usage()["calls"], 2)

    def test_final_commit_crash_recovers_corrected_note_without_any_network(self):
        corrected = self.markdown.replace("结果仍未确定", "结论还未确定")
        atomic = bulk.atomic_bytes

        def interrupt(target, data):
            if target == self.job_dir / "note.md" and data == corrected.encode():
                raise SimulatedDeath()
            return atomic(target, data)

        with patch.object(bulk, "atomic_bytes", side_effect=interrupt):
            with self.assertRaises(SimulatedDeath):
                self.execute([self.wire(self.markdown), self.review_wire(markdown=corrected)])
        self.assertEqual(bulk.read(self.job_dir / "content-journal.json")["state"], "prepared")
        result, calls = self.execute([])
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(calls, 0)
        self.assertEqual(self.workspace.note(self.item)[1], corrected.encode())
        self.assertEqual(self.workspace.usage()["calls"], 2)

    def test_external_edit_during_review_is_preserved(self):
        transport = self.workspace.response
        edited = "# 用户修改\n\n必须保留。\n"

        def edit(job_id, stage, payload, key_file):
            if stage == "review":
                (self.job_dir / "note.md").write_text(edited)
            return transport(job_id, stage, payload, key_file)

        with patch.object(self.workspace, "response", side_effect=edit):
            result, _ = self.execute([self.wire(self.markdown), self.review_wire()])
        self.assertEqual(result["state"], "attention", result)
        self.assertIn("preserving", result["error"])
        self.assertEqual((self.job_dir / "note.md").read_text(), edited)

    def test_unknown_draft_attempt_never_resends_on_restart(self):
        result, calls = self.execute([TimeoutError()])
        self.assertEqual(result["state"], "attention")
        self.assertEqual(calls, 1)
        self.workspace.state(self.job_id, "running")  # Represents interrupted state only.
        result, calls = self.execute([])
        self.assertEqual(calls, 0)
        self.assertIn("needs_reconciliation", result["error"])
        self.assertEqual(self.workspace.usage()["unknown_cost_calls"], 1)

    def test_cli_recovery_cannot_switch_a_new_job_to_old_machine_field_protocol(self):
        from chatgpt_pipeline.note_recovery import CompleteRecovery
        self.workspace.state(self.job_id, "attention", "fixture")
        recovery = CompleteRecovery(self.workspace)
        with self.assertRaisesRegex(ValueError, "cannot enter legacy"):
            recovery.plan(self.job_id)
        with self.assertRaisesRegex(ValueError, "cannot enter legacy"):
            recovery.prepare(self.job_id, allow_calls=True)
        self.assertEqual(self.workspace.usage()["calls"], 0)
        self.assertFalse((self.job_dir / f"{recovery.revision}.json").exists())

    def test_changed_prompt_configuration_blocks_without_network(self):
        with patch.object(note_content, "PROMPT", note_content.PROMPT + "changed"):
            result, calls = self.execute([])
        self.assertEqual(calls, 0)
        self.assertIn("configuration changed", result["error"])

    def test_json_wrapper_does_not_discard_markdown_or_trust_fabricated_metadata(self):
        wrapped = encoded({"markdown": self.markdown, "title": "伪造标题",
                           "source_message_ids": ["invented"], "verbatim_code_ids": ["fake"]}).decode()
        result, _ = self.execute([self.wire(wrapped), self.review_wire()])
        self.assertEqual(result["state"], "done", result)
        draft = bulk.read(self.job_dir / "draft/result.json")
        self.assertEqual(draft["title"], "我的实验")
        self.assertEqual(draft["provenance"]["source_context_message_ids"], ["m1"])
        self.assertEqual(draft["output_adapter"]["operation"], "extract-json-markdown")

    def test_journal_resume_refuses_external_edits(self):
        corrected = self.markdown.replace("结果仍未确定", "结论还未确定")
        atomic = bulk.atomic_bytes

        def interrupt(target, data):
            if target == self.job_dir / "note.md" and data == corrected.encode():
                raise SimulatedDeath()
            return atomic(target, data)

        with patch.object(bulk, "atomic_bytes", side_effect=interrupt):
            with self.assertRaises(SimulatedDeath):
                self.execute([self.wire(self.markdown), self.review_wire(markdown=corrected)])
        edited = "# 外部修改\n\n保留本版本。\n"
        (self.job_dir / "note.md").write_text(edited)
        result, calls = self.execute([])
        self.assertEqual(calls, 0)
        self.assertEqual(result["state"], "attention", result)
        self.assertIn("preserving", result["error"])
        self.assertEqual((self.job_dir / "note.md").read_text(), edited)


if __name__ == "__main__":
    unittest.main()
