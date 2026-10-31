import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from chatgpt_pipeline import bulk, notes, workbench
from chatgpt_pipeline.index import ingest
from chatgpt_pipeline.verify import encoded, loads
from test_ingestion import package, write_batch


class NoteTests(unittest.TestCase):
    def setUp(self):
        # These fixtures exercise sent v2/legacy requests and their compatibility.
        # The content-first default has separate end-to-end coverage.
        from chatgpt_pipeline import note_contract
        execution = patch.object(notes, "DEFAULT_EXECUTION_REVISION", note_contract.REVISION)
        execution.start()
        self.addCleanup(execution.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.mod = patch.object(bulk, "__file__", str(self.base / "src/chatgpt_pipeline/bulk.py"))
        self.mod.start()
        self.addCleanup(self.mod.stop)
        self.source = self.base / "exports/account-a"
        self.source.mkdir(parents=True)
        write_batch(self.source, package("I tried two experiments and kept the uncertainty.\n```py\nx = 1\n```"))
        db = self.base / "offline.sqlite3"
        ingest(self.source.parent, db)
        self.batch = self.base / "runtime/batch"
        bulk.prepare(db, self.batch)
        _, manifest, store = bulk.batch(self.batch)
        parent = manifest["conversations"][0]["jobs"][0]
        selection = {"disposition": "keep", "reason": "Personal experiment", "items": [
            {"title": "My experiment", "summary": "I compared two experiments; the outcome is uncertain.",
             "user_message_ids": ["m001"], "related_message_ids": []}]}
        self.respond_selection(store / parent, selection)
        jobs, reviews = bulk.member_jobs(manifest, store, create_reviews=True)
        self.respond_selection(store / reviews[parent], selection)
        bulk.export(self.batch)
        self.directory = self.base / "runtime/workbench"
        notes.initialize(self.batch, self.directory)
        self.workspace = notes.Workspace(self.directory)
        self.item = self.workspace.list()["items"][0]["id"]

    def respond_selection(self, directory, result):
        bulk.save(directory / "attempt.json", {"reserved_usd": "0.01"})
        bulk.save(directory / "receipt.json", {"http_status": 200})
        bulk.atomic_bytes(directory / "response.raw", encoded(self.response(result)))
        bulk.recover(directory)

    def response(self, result):
        return {"id": "test-response", "model": bulk.MODEL, "usage": {"cost": 0.001, "prompt_tokens": 20},
                "choices": [{"finish_reason": "stop", "message": {"content": encoded(result).decode()}}]}

    def draft(self):
        return {"title": "我的实验", "markdown": "# 我的实验\n\n我做了两次实验，结果仍不确定。这段记录保留了当时的尝试。\n\n[[CODE:c001]]",
                "source_message_ids": ["m001"], "verbatim_code_ids": ["c001"]}

    def responses(self, review=None):
        result = self.draft()
        responses = [io.BytesIO(encoded(self.response(result))), io.BytesIO(encoded(self.response(
            review or {**result, "verdict": "ready", "review_reason": "Faithful to the original, uncertainty preserved."})))]
        for response in responses:
            response.status = 200
        return responses

    def test_import_decisions_and_job_identity_survive_restart(self):
        self.workspace.decide(self.item, "skip")
        notes.initialize(self.batch, self.directory)
        other = notes.Workspace(self.directory)
        self.assertEqual(other.list("skipped")["counts"]["skipped"], 1)
        with self.assertRaisesRegex(ValueError, "Restore"):
            other.request(self.item)
        other.decide(self.item, "restore")
        first = other.request(self.item)
        self.assertEqual(first["id"], notes.Workspace(self.directory).request(self.item)["id"])

    def test_generate_review_exact_code_and_repeat_without_any_calls(self):
        job = self.workspace.request(self.item)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()) as post:
            result = self.workspace.run(job["id"])
            self.assertEqual(result["state"], "done", result)
            self.workspace.run(job["id"])
            self.assertEqual(post.call_count, 2)
        title, content = self.workspace.note(self.item)
        self.assertIn(b"```py\nx = 1\n```", content)
        self.assertNotIn(b"[[CODE:", content)
        self.assertEqual(self.workspace.usage()["actual_usd"], "0.002")
        self.assertEqual(notes.Workspace(self.directory).list("done")["counts"]["done"], 1)

    def test_timeout_never_resends_and_cost_remains_reserved(self):
        job = self.workspace.request(self.item)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError) as post:
            self.assertEqual(self.workspace.run(job["id"])["state"], "attention")
            self.workspace.state(job["id"], "running")  # Simulate restart after a process death.
            result = self.workspace.run(job["id"])
        self.assertEqual(post.call_count, 1)
        self.assertIn("needs_reconciliation", result["error"])
        self.assertEqual(self.workspace.usage()["unknown_cost_calls"], 1)

    def test_response_saved_before_crash_is_parsed_offline(self):
        job = self.workspace.request(self.item)
        directory = self.directory / "jobs" / job["id"] / "draft"
        directory.mkdir()
        material = bulk.read(directory.parent / "input.json")
        payload = self.workspace.payload(job["id"], "draft", material, None)
        bulk.save(directory / "request.json", payload)
        bulk.atomic_bytes(directory / "response.raw", encoded(self.response(self.draft())))
        with self.workspace.db() as db:
            db.execute("INSERT INTO calls(job_id,stage,request_hash,reservation) VALUES(?,?,?,?)",
                       (job["id"], "draft", bulk.digest(payload), "0.02"))
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()[1:]) as post:
            result = self.workspace.run(job["id"])
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(post.call_count, 1)

    def test_new_jobs_persist_strict_stage_specific_output_contract(self):
        from chatgpt_pipeline import note_contract
        job = self.workspace.request(self.item)
        directory = self.directory / "jobs" / job["id"]
        definition = bulk.read(directory / "definition.json")
        self.assertEqual(definition["identity"]["execution_revision"], note_contract.REVISION)
        material = bulk.read(directory / "input.json")
        for stage, draft, expected in (("draft", None, set(self.draft())),
                                       ("review", self.draft(), set(self.draft()) | {"verdict", "review_reason"})):
            payload = self.workspace.payload(job["id"], stage, material, draft)
            self.assertEqual(payload["response_format"]["type"], "json_schema")
            contract = payload["response_format"]["json_schema"]
            self.assertTrue(contract["strict"])
            schema = contract["schema"]
            self.assertEqual(set(schema["required"]), expected)
            self.assertEqual(set(schema["properties"]), expected)
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(schema["properties"]["source_message_ids"]["items"]["enum"], ["m001"])
            self.assertEqual(schema["properties"]["verbatim_code_ids"]["items"]["enum"], ["c001"])
        review = self.workspace.payload(job["id"], "review", material, self.draft())
        self.assertEqual(review["response_format"]["json_schema"]["schema"]["properties"]["verdict"]["enum"],
                         ["ready", "needs_review"])

    def test_unflagged_legacy_identity_keeps_exact_original_request_on_resume(self):
        current = self.workspace.request(self.item)
        directory = self.directory / "jobs" / current["id"]
        definition = bulk.read(directory / "definition.json")
        definition["identity"].pop("execution_revision")
        legacy_id = bulk.digest(definition["identity"])
        definition["id"] = legacy_id
        legacy_dir = directory.with_name(legacy_id)
        directory.rename(legacy_dir)
        bulk.save(legacy_dir / "definition.json", definition)
        with self.workspace.db() as db:
            db.execute("UPDATE jobs SET id=? WHERE id=?", (legacy_id, current["id"]))
            db.execute("UPDATE items SET job_id=? WHERE id=?", (legacy_id, self.item))
        material = bulk.read(legacy_dir / "input.json")
        original_payload = notes.request_payload("draft", material, None)
        self.assertEqual(self.workspace.payload(legacy_id, "draft", material, None), original_payload)
        self.assertEqual(original_payload["response_format"], {"type": "json_object"})
        self.assertEqual(original_payload["max_tokens"], 6000)
        draft_dir = legacy_dir / "draft"
        draft_dir.mkdir()
        bulk.save(draft_dir / "request.json", original_payload)
        raw = encoded(self.response(self.draft()))
        bulk.atomic_bytes(draft_dir / "response.raw", raw)
        with self.workspace.db() as db:
            db.execute("INSERT INTO calls(job_id,stage,request_hash,reservation) VALUES(?,?,?,?)",
                       (legacy_id, "draft", bulk.digest(original_payload), "0.02"))
        request_before = (draft_dir / "request.json").read_bytes()
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()[1:]) as post:
            result = self.workspace.run(legacy_id)
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(post.call_count, 1)
        self.assertEqual((draft_dir / "request.json").read_bytes(), request_before)
        self.assertEqual((draft_dir / "response.raw").read_bytes(), raw)
        self.assertEqual(bulk.read(legacy_dir / "review/request.json")["response_format"], {"type": "json_object"})

    def test_current_schema_markdown_pattern_accepts_inline_code_and_rejects_literal_fences(self):
        import re
        job = self.workspace.request(self.item)
        material = bulk.read(self.directory / "jobs" / job["id"] / "input.json")
        payload = self.workspace.payload(job["id"], "draft", material, None)
        pattern = payload["response_format"]["json_schema"]["schema"]["properties"]["markdown"]["pattern"]
        for markdown in ("# 普通中文标题\n\n保留 Unicode：思考与经历。",
                         "# 多行正文\n\n第一段\n第二段\n",
                         "# 行内代码\n\n这里使用 `x`，也能写 `x + 1`。",
                         "# 两个反引号\n\n``单个 ` 符号`` 仍是行内代码。",
                         "# 末尾标记\n\n保留一个或两个反引号``"):
            with self.subTest(accepted=markdown):
                self.assertIsNotNone(re.search(pattern, markdown))
        for markdown in ("```", "```python\nx = 1\n```",
                         "# 单行\n\n```invented()```",
                         "# 行内混入\n\n正文 ``` 含围栏",
                         "# 长围栏\n\n````markdown\ntext\n````",
                         "# 结尾\n\n正文```\n"):
            with self.subTest(rejected=markdown):
                self.assertIsNone(re.search(pattern, markdown))

    def test_long_heading_and_whitespace_after_heading_are_not_a_note_body(self):
        job = self.workspace.request(self.item)
        material = bulk.read(self.directory / "jobs" / job["id"] / "input.json")
        heading = "# " + "只有很长的标题仍然没有实际正文" * 4
        for markdown in (heading, heading + "\n", heading + "\n\n  \t \r\n"):
            self.assertGreater(len(markdown), 30)
            for stage in ("draft", "review"):
                result = {**self.draft(), "markdown": markdown, "verbatim_code_ids": []}
                if stage == "review":
                    result.update(verdict="ready", review_reason="不能把模型的 ready 当作正文已完整的证明。")
                with self.subTest(stage=stage, markdown=markdown):
                    with self.assertRaisesRegex(ValueError, "body is empty after title"):
                        notes.validate(result, material, stage)

    def test_changed_source_blocks_request_and_semantic_failure_has_no_note(self):
        job = self.workspace.request(self.item)
        material = bulk.read(self.directory / "jobs" / job["id"] / "input.json")
        source = Path(material["source"]["source_path"])
        original = source.read_bytes()
        source.write_bytes(b"changed")
        with patch("urllib.request.OpenerDirector.open") as post:
            result = self.workspace.run(job["id"])
        post.assert_not_called()
        self.assertIn("Source bytes changed", result["error"])
        source.write_bytes(original)
        self.workspace.state(job["id"], "queued")
        bad_review = {**self.draft(), "verdict": "needs_review", "review_reason": "Missing context"}
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses(bad_review)):
            rejected = self.workspace.run(job["id"])
            self.assertEqual(rejected["state"], "attention")
            self.assertIn("Semantic review", rejected["error"])
        with self.assertRaisesRegex(ValueError, "not ready"):
            self.workspace.note(self.item)

    def test_budget_reservations_and_double_click_do_not_duplicate_calls(self):
        job = self.workspace.request(self.item)
        with self.workspace.db() as db:
            db.execute("INSERT INTO calls(job_id,stage,request_hash,reservation) VALUES('other','draft','hash','2')")
        with patch.object(notes, "credential", return_value="synthetic-key"), patch("urllib.request.OpenerDirector.open") as post:
            self.assertIn("budget", self.workspace.run(job["id"])["error"])
        post.assert_not_called()
        with self.workspace.db() as db:
            db.execute("DELETE FROM calls WHERE job_id='other'")
        self.workspace.state(job["id"], "queued")
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()) as post:
            threads = [threading.Thread(target=self.workspace.run, args=(job["id"],)) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(post.call_count, 2)
        self.assertEqual(self.workspace.job(job["id"])["state"], "done")

    def test_reference_and_code_fabrication_rejected(self):
        job = self.workspace.request(self.item)
        material = bulk.read(self.directory / "jobs" / job["id"] / "input.json")
        for changes in ({"source_message_ids": ["m999"]}, {"verbatim_code_ids": ["c999"]},
                        {"markdown": "# 我的实验\n\n" + "Original body " * 5 + "```py\nx=2\n```"}):
            with self.assertRaises(ValueError):
                notes.validate({**self.draft(), **changes}, material, "draft")

    def test_only_unused_empty_code_metadata_can_be_recovered(self):
        job = self.workspace.request(self.item)
        material = bulk.read(self.directory / "jobs" / job["id"] / "input.json")
        original = {"title": "我的实验", "markdown": "# 我的实验\n\n我做了两次实验，结果尚不确定。这是一项尚未完成验证的个人记录。",
                    "source_message_ids": ["m001"]}
        result = notes.validate(original, material, "draft")
        self.assertEqual(result, {**original, "verbatim_code_ids": []})
        with self.assertRaises(ValueError):
            notes.validate({**original, "markdown": original["markdown"] + "[[CODE:c001]]"}, material, "draft")

    def test_offline_calibration_preserves_failed_review_and_makes_no_calls(self):
        job = self.workspace.request(self.item)
        bad_review = {**self.draft(), "verdict": "needs_review", "review_reason": "Attribution needs review"}
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses(bad_review)):
            self.workspace.run(job["id"])
        result = {**self.draft(), "verdict": "ready", "review_reason": "Compared original, retained only supported material"}
        path = self.directory / "review-decision.json"
        bulk.save(path, {"reviewer": "Test reviewer", "reason": "Explicit source comparison", "result": result})
        with patch("urllib.request.OpenerDirector.open") as post:
            self.assertEqual(self.workspace.accept_calibration(job["id"], path)["state"], "done")
        post.assert_not_called()
        directory = self.directory / "jobs" / job["id"]
        self.assertEqual(bulk.read(directory / "review/result.json")["verdict"], "needs_review")
        self.assertEqual(bulk.read(directory / "calibration-review.json")["new_calls"], 0)
        self.assertEqual(self.workspace.usage()["calls"], 2)

    def test_upgrade_retains_calls_and_terminal_jobs_and_refuses_active_jobs(self):
        bulk.save(self.directory / "config.json", notes.config("standalone-note-v1"))
        notes.initialize(self.batch, self.directory)
        self.assertEqual(bulk.read(self.directory / "config.json"), notes.config())
        self.assertEqual(len(list((self.directory / "configuration-history").glob('*.json'))), 1)
        job = self.workspace.request(self.item)
        bulk.save(self.directory / "config.json", notes.config("standalone-note-v1"))
        with self.assertRaisesRegex(ValueError, "active jobs"):
            notes.initialize(self.batch, self.directory)
        self.assertEqual(self.workspace.job(job["id"])["state"], "queued")

    def test_v2_upgrade_keeps_completed_output_and_budget_ledger(self):
        job = self.workspace.request(self.item)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()):
            self.workspace.run(job["id"])
        original = self.workspace.note(self.item)[1]
        bulk.save(self.directory / "config.json", notes.config("standalone-note-v2"))
        notes.initialize(self.batch, self.directory)
        self.assertEqual(self.workspace.note(self.item)[1], original)
        self.assertEqual(self.workspace.usage()["calls"], 2)
        self.assertEqual(self.workspace.usage()["actual_usd"], "0.002")
        self.assertEqual(self.workspace.request(self.item)["id"], job["id"])

    def test_revision_keeps_previous_output_and_is_idempotent_without_calls(self):
        job = self.workspace.request(self.item)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()):
            self.workspace.run(job["id"])
        old_content = self.workspace.note(self.item)[1]
        decision = {"reviewer": "Explicit reviewer", "reason": "Preserve useful experimental background",
                    "result": {**self.draft(), "markdown": self.draft()["markdown"] + "\n\n两次尝试的结果仍不确定，不能当作成功记录。",
                               "verdict": "ready", "review_reason": "Context retained without inventing results"}}
        path = self.directory / "revision.json"
        bulk.save(path, decision)
        with patch("urllib.request.OpenerDirector.open") as post:
            self.workspace.revise_note(job["id"], path)
            self.workspace.revise_note(job["id"], path)
        post.assert_not_called()
        archives = list((self.directory / "jobs" / job["id"] / "revisions").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertEqual((archives[0] / "previous-note.md").read_bytes(), old_content)
        self.assertEqual(bulk.read(archives[0] / "journal.json")["state"], "applied")
        self.assertNotEqual(self.workspace.note(self.item)[1], old_content)
        self.assertEqual(self.workspace.usage()["calls"], 2)
        # A file edited outside the workflow is protected even if the same decision is replayed.
        (self.directory / "jobs" / job["id"] / "note.md").write_text('user edited content')
        with self.assertRaisesRegex(ValueError, "edited"):
            self.workspace.revise_note(job["id"], path)

    def test_interrupted_revision_recovers_saved_journal_without_model_calls(self):
        job = self.workspace.request(self.item)
        with patch.object(notes, "credential", return_value="synthetic-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=self.responses()):
            self.workspace.run(job["id"])
        path = self.directory / "revision.json"
        bulk.save(path, {"reviewer": "Explicit reviewer", "reason": "Add background",
                         "result": {**self.draft(), "markdown": self.draft()["markdown"] + "\n\n这是尚未确认结果的实验。",
                                    "verdict": "ready", "review_reason": "Faithful expansion"}})
        real_save = bulk.save
        root_result = self.directory / "jobs" / job["id"] / "result.json"
        def fail_result_once(destination, value):
            if destination == root_result:
                raise OSError('Simulated interrupted persistence')
            return real_save(destination, value)
        with patch.object(bulk, "save", side_effect=fail_result_once):
            with self.assertRaises(OSError):
                self.workspace.revise_note(job["id"], path)
        with patch("urllib.request.OpenerDirector.open") as post:
            self.workspace.revise_note(job["id"], path)
        post.assert_not_called()
        self.assertIn('尚未确认结果', self.workspace.note(self.item)[1].decode())
        self.assertEqual(self.workspace.usage()["calls"], 2)

    def test_http_actions_require_token_no_arbitrary_paths_or_inputs(self):
        app = workbench.Application(self.directory)
        self.addCleanup(app.close)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), workbench.handler(app))
        except PermissionError:
            self.skipTest("Loopback sockets unavailable in sandbox; run this test on host")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/api/session") as response:
            token = loads(response.read())["token"]
        action = base + f"/api/items/{self.item}/skip"
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(urllib.request.Request(action, data=b"{}"))
        request = urllib.request.Request(action, data=b"{}", headers={"X-Workbench-Token": token})
        with urllib.request.urlopen(request) as response:
            self.assertTrue(loads(response.read())["ok"])
        self.assertEqual(self.workspace.list("skipped")["counts"]["skipped"], 1)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/../config.json")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(urllib.request.Request(action, data=b"{}", headers={"X-Workbench-Token": token, "Origin": "https://evil.example"}))


if __name__ == "__main__":
    unittest.main()
