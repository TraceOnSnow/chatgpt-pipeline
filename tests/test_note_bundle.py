from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from chatgpt_pipeline import bulk, memory, notes
from chatgpt_pipeline.verify import encoded, loads
from test_ingestion import package


class NoteBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        module = patch.object(bulk, "__file__", str(self.base / "src/chatgpt_pipeline/bulk.py"))
        module.start()
        self.addCleanup(module.stop)
        first = package("My first experiment.\n```py\nx = 1\n```")["raw"]
        for number, role, text in ((2, "assistant", "An unverified suggestion."),
                                   (3, "user", "My distinct related observation."),
                                   (4, "assistant", "A later unverified suggestion."),
                                   (5, "user", "Please save this personal reflection in my private notes; this is not permission to publish."),
                                   (6, "assistant", "An unverified interpretation of that reflection.")):
            first["mapping"][f"n{number}"] = {"parent": f"n{number - 1}", "message": {
                "id": f"m{number}", "author": {"role": role}, "recipient": "all",
                "content": {"content_type": "text", "parts": [text]}}}
        first["current_node"] = "n6"
        second = deepcopy(first)
        second["conversation_id"] = "c2"
        second["mapping"] = {k: v for k, v in second["mapping"].items() if k in ("n1", "n2")}
        second["current_node"] = "n2"
        second["mapping"]["n1"]["message"]["content"]["parts"] = ["My second conversation experiment.\n```py\ny = 2\n```"]
        archive = self.base / "test.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("conversations.json", json.dumps([first, second]))
        profile = self.base / "profile.json"
        bulk.save(profile, {"version": "bundle-test", "screen_prompt": bulk.SCREEN_PROMPT, "review_prompt": bulk.REVIEW_PROMPT})
        self.batch = self.base / "runtime/batch"
        memory.prepare(archive, self.batch, profile, "account-a")
        _, manifest, store = bulk.batch(self.batch)
        for entry in manifest["conversations"]:
            parent = entry["jobs"][0]
            count = 2 if entry["source"]["conversation_id"] == "c1" else 1
            result = {"disposition": "keep", "reason": "Personal experiments", "items": [
                {"title": f"Topic {entry['source']['conversation_id']} {i}", "summary": f"Personal observation {i}",
                 "user_message_ids": ["m001" if i == 0 else "m003"], "related_message_ids": []}
                for i in range(count)]}
            self.finish_selection(store / parent, result)
            child = bulk.review_job(store, parent)
            bulk.save(store / parent / "review.json", {"job_id": child,
                      "parent_result_sha256": bulk.digest(bulk.read(store / parent / "result.json"))})
            self.finish_selection(store / child, result)
        bulk.export(self.batch)
        self.directory = self.base / "runtime/workbench"
        notes.initialize(self.batch, self.directory)
        self.workspace = notes.Workspace(self.directory)
        self.items = self.workspace.list()["items"]
        self.main = self.items[0]["id"]

    def response(self, content):
        return {"id": "mock-response", "model": bulk.MODEL, "usage": {"cost": 0.001},
                "choices": [{"finish_reason": "stop", "message": {"content": content}}]}

    def finish_selection(self, directory, result):
        bulk.save(directory / "attempt.json", {"reserved_usd": "0.01"})
        bulk.save(directory / "receipt.json", {"http_status": 200})
        bulk.atomic_bytes(directory / "response.raw", encoded(self.response(encoded(result).decode())))
        self.assertEqual(bulk.recover(directory)["state"], "complete")

    def bundle(self):
        editorial = {"version": "memory-editorial-v1", "goal": "Combine all related experiments and observations.",
                     "bundle_item_ids": [item["id"] for item in self.items]}
        folder = self.directory / "editorial-inputs"
        folder.mkdir(exist_ok=True)
        bulk.save(folder / (self.main + ".json"), editorial)
        job = self.workspace.request(self.main)
        material = bulk.read(self.directory / "jobs" / job["id"] / "input.json")
        return job, material

    def test_union_dedup_aliases_codes_and_all_subtopics_are_preserved(self):
        job, material = self.bundle()
        self.assertEqual(len(material["sources"]), 2)
        self.assertEqual(len(material["messages"]), 8)
        self.assertEqual(len(material["bundle"]["candidates"]), 3)
        self.assertEqual(len(material["codes"]), 2)
        self.assertEqual(len(material["personal_evidence_ids"]), 3)
        self.assertEqual(len({m["id"] for m in material["messages"]}), 8)
        self.assertEqual({s["conversation_id"] for s in material["sources"]}, {"c1", "c2"})
        notes.verify_material_sources(material)
        self.assertEqual(self.workspace.request(self.main)["id"], job["id"])
        with self.workspace.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM calls").fetchone()[0], 0)

    def test_short_original_keeps_save_intent_outside_candidate_neighbors(self):
        _, material = self.bundle()
        # Neither retained candidate cites m5; m5 is outside their immediate
        # neighbors. Earlier bundling silently omitted this explicit intent.
        saved = [m for m in material["messages"] if m["source_index"] == 0 and m["source_message_id"] == "m5"]
        self.assertEqual(len(saved), 1)
        self.assertIn("not permission to publish", saved[0]["text"])
        self.assertTrue(material["bundle"]["complete_source_context"])
        self.assertEqual(material["bundle"]["omitted_source_messages"], 0)
        self.assertEqual(material["source_policy_revision"], bulk.SOURCE_POLICY_REVISION)
        notes.verify_material_sources(material)

    def test_cross_source_role_and_text_tampering_fail_closed(self):
        _, material = self.bundle()
        for field, value in (("source_index", 1), ("role", "assistant"), ("text", "fabricated")):
            changed = deepcopy(material)
            changed["messages"][0][field] = value
            with self.assertRaisesRegex(ValueError, "reference changed"):
                notes.verify_material_sources(changed)
        changed = deepcopy(material)
        changed["messages"][0]["source_index"] = 99
        with self.assertRaisesRegex(ValueError, "source index"):
            notes.verify_material_sources(changed)

    def test_bundle_has_exactly_one_draft_review_pair_and_shared_budget_no_resend(self):
        job, material = self.bundle()
        draft = "# Related experiments\n\nThe user tried experiments; the outcomes remain uncertain."
        review = encoded({"markdown": draft, "verdict": "ready", "reason": "Faithful user attribution."}).decode()
        responses = [io.BytesIO(encoded(self.response(text))) for text in (draft, review)]
        for response in responses:
            response.status = 200
        with patch.object(notes, "credential", return_value="mock-key"), \
             patch("urllib.request.OpenerDirector.open", side_effect=responses) as post:
            result = self.workspace.run(job["id"])
            self.assertEqual(result["state"], "done", result)
            self.workspace.run(job["id"])
            self.assertEqual(post.call_count, 2)
        self.assertEqual(self.workspace.usage()["actual_usd"], "0.002")

    def test_existing_budget_prevents_bundle_calls(self):
        job, _ = self.bundle()
        with self.workspace.db() as db:
            db.execute("INSERT INTO calls(job_id,stage,request_hash,reservation,cost) VALUES(?,?,?,?,?)",
                       ("historical", "draft", "frozen", "2", "2"))
        with patch.object(notes, "credential", return_value="mock-key"), \
             patch("urllib.request.OpenerDirector.open") as post:
            result = self.workspace.run(job["id"])
        post.assert_not_called()
        self.assertEqual(result["state"], "attention")
        self.assertIn("Workspace budget reached", result["error"])

    def test_mandatory_evidence_bound_is_explicit_and_legacy_material_unchanged(self):
        item = self.workspace.item(self.main)
        candidate = loads(item["candidate"])
        expected = notes.prepare_material(candidate, Path(loads(item["origin"])["store"]))
        job = self.workspace.request(self.main)
        material = bulk.read(self.directory / "jobs" / job["id"] / "input.json")
        self.assertEqual(material, expected)
        self.assertNotIn("sources", material)
        editorial = {"bundle_item_ids": [i["id"] for i in self.items]}
        with patch.object(notes, "MAX_SOURCE", 1):
            with self.assertRaisesRegex(ValueError, "no truncation"):
                notes.prepare_bundle(self.workspace, self.main, editorial)


if __name__ == "__main__":
    unittest.main()
