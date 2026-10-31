"""Isolated delivery contracts: no archive, network, or actual Vault access."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from chatgpt_pipeline import bulk, notes

SPEC = importlib.util.spec_from_file_location("memory_deliver", Path(__file__).parents[1] / "scripts/memory-deliver.py")
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)
Library = delivery.Library


class MemoryDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        root = patch.object(bulk, "__file__", str(self.base / "src/chatgpt_pipeline/bulk.py"))
        root.start()
        self.addCleanup(root.stop)
        self.workspace = self.base / "runtime/workspace"
        self.workspace.mkdir(parents=True)
        bulk.save(self.workspace / "config.json", notes.config())
        with sqlite3.connect(self.workspace / "state.sqlite3") as db:
            db.execute("CREATE TABLE items(id TEXT, title TEXT, job_id TEXT)")
            db.execute("CREATE TABLE jobs(id TEXT, state TEXT)")
        self.vault = self.base / "vault"
        self.library = Library.initialize(self.vault)
        (self.vault / "分类").mkdir()
        self.plan = self.base / "plan.json"
        self.reports = self.base / "runtime/reports"
        self.item = self.job("# 新稿\n\n新增内容。\n")

    def job(self, markdown, *, role="user", material=None):
        material = material or {"source": {"account_key": "account-a", "conversation_id": "conversation-a",
                    "zip_provenance": {"zip_sha256": "a" * 64, "member": "conversations.json"}},
                    "messages": [{"id": "m001", "source_message_id": "real-message-id", "role": role,
                                  "text": "我的实际材料"}], "personal_evidence_ids": ["m001"]}
        item_id = bulk.digest(markdown)
        identity = {"item_id": item_id, "input_sha256": bulk.digest(material)}
        job_id = bulk.digest(identity)
        directory = self.workspace / "jobs" / job_id
        directory.mkdir(parents=True)
        bulk.save(directory / "definition.json", {"id": job_id, "identity": identity})
        bulk.save(directory / "input.json", material)
        bulk.atomic_bytes(directory / "note.md", markdown.encode())
        bulk.save(directory / "result.json", {"title": "新稿", "markdown_sha256": delivery.sha(markdown.encode())})
        with sqlite3.connect(self.workspace / "state.sqlite3") as db:
            db.execute("INSERT INTO items VALUES(?,?,?)", (item_id, "新稿", job_id))
            db.execute("INSERT INTO jobs VALUES(?,?)", (job_id, "done"))
        return item_id

    def run_plan(self, entries=None, *, apply=False, token=None):
        if entries is not None:
            bulk.save(self.plan, {"items": entries})
        return delivery.deliver(self.workspace, self.vault, self.plan, self.reports,
                                apply=apply, expected_token=token)

    def snapshot(self):
        return {p.relative_to(self.vault).as_posix(): p.read_bytes()
                for p in self.vault.rglob("*") if p.is_file()}

    def test_preview_is_read_only_and_create_is_idempotent_after_user_edit(self):
        before = self.snapshot()
        preview = self.run_plan([{"item_id": self.item, "title": "新笔记", "path": "分类/新笔记.md"}])
        self.assertEqual(before, self.snapshot())
        result = self.run_plan(apply=True, token=preview["expected_token"])
        row = result["items"][0]
        note = self.library.get(row["id"])
        revised = note["markdown"] + "\n用户后来改稿。\n"
        self.library.replace(row["id"], revised, expected_hash=note["sha256"])
        again = self.run_plan()
        self.assertEqual(again["items"][0]["outcome"], "existing")
        self.run_plan(apply=True, token=again["expected_token"])
        self.assertEqual(self.library.get(row["id"])["markdown"], revised)
        source = self.library._read_state()["sources"][row["source_id"]]["source"]
        self.assertEqual(source["account_key"], "account-a")
        self.assertEqual(source["evidence"][0]["message_id"], "real-message-id")
        self.assertEqual(source["archive_provenance"]["zip_sha256"], "a" * 64)
        self.assertNotIn("real-message-id", revised)

    def test_append_preserves_original_properties_checkbox_and_publication(self):
        note = self.library.create("# 原文\n\n- [x] 用户选择\n\n原有正文。\n", title="原文", path="分类/原文.md")
        with self.library._lock():
            state = self.library._load()
            state["publications"][note["id"]] = {"sentinel": "selected snapshot"}
            state["notes"][note["id"]]["custom_relation"] = {"article": "another-id"}
            self.library._save(state)
        state_before = deepcopy(self.library._read_state())
        preview = self.run_plan([{"item_id": self.item, "title": "补稿", "target_id": note["id"]}])
        with patch.object(Library, "_commit", autospec=True, wraps=Library._commit) as commit:
            # Count commits while forwarding to the actual journaling implementation.
            commit.side_effect = lambda obj, *a, **kw: original_commit(obj, *a, **kw)
            result = self.run_plan(apply=True, token=preview["expected_token"])
            self.assertEqual(commit.call_count, 1)
        current = self.library.get(note["id"])
        self.assertIn(note["markdown"].rstrip(), current["markdown"])
        self.assertIn("新增内容。", current["markdown"])
        self.assertEqual(current["properties"], note["properties"])
        self.assertEqual(result["items"][0]["outcome"], "supplemented")
        state_after = self.library._read_state()
        self.assertEqual(state_before["publications"], state_after["publications"])
        self.assertEqual(state_after["notes"][note["id"]]["custom_relation"], {"article": "another-id"})
        self.assertTrue((Path(preview["report_dir"]) / "0000-before.md").exists())

    def test_changed_current_sha_blocks_entire_batch(self):
        note = self.library.create("# 原文\n", title="原文", path="分类/原文.md")
        preview = self.run_plan([{"item_id": self.item, "title": "补稿", "target_id": note["id"]}])
        self.library.replace(note["id"], note["markdown"] + "用户编辑。\n", expected_hash=note["sha256"])
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "token differs"):
            self.run_plan(apply=True, token=preview["expected_token"])
        self.assertEqual(before, self.snapshot())

    def test_errors_are_collected_and_no_partial_batch_applies(self):
        entries = [{"item_id": self.item, "title": "有效", "path": "分类/有效.md"},
                   {"item_id": "0" * 64, "title": "无效", "path": "分类/无效.md"},
                   {"item_id": "1" * 64, "title": "无效二", "path": "分类/无效二.md"}]
        before = self.snapshot()
        preview = self.run_plan(entries)
        self.assertEqual(len(preview["errors"]), 2)
        with self.assertRaisesRegex(ValueError, "Delivery has errors"):
            self.run_plan(apply=True, token=preview["expected_token"])
        self.assertEqual(before, self.snapshot())

    def test_interview_supplement_uses_only_two_subjects(self):
        note = self.library.create("# 面试\n\n## 题目与我的作答\n\n原作答。\n\n## 复盘与重做\n\n原复盘。\n",
                                   title="面试", path="分类/面试.md")
        item = self.job("# 面试补稿\n\n## 题目与我的作答\n\n补充题目。\n\n## 复盘与重做\n\n补充复盘。\n")
        preview = self.run_plan([{"item_id": item, "title": "补稿", "target_id": note["id"]}])
        self.assertFalse(preview["errors"])
        self.run_plan(apply=True, token=preview["expected_token"])
        current = self.library.get(note["id"])["markdown"]
        found = [h[3] for h in delivery.headings(current) if h[2] == 2]
        self.assertEqual(found, list(delivery.SUBJECTS))
        self.assertIn("原作答。", current)
        self.assertIn("补充复盘。", current)
        self.assertLess(current.index("补充题目。"), current.index("## 复盘与重做"))

    def test_replace_preserves_frontmatter_and_user_checkboxes(self):
        note = self.library.create("# 原文\n\n- [x] 保留勾选\n\n原正文\n", title="原文", path="分类/原文.md")
        revision = self.base / "revision.md"
        revision.write_text(note["markdown"].replace("原正文", "完整修订正文"))
        entry = {"item_id": self.item, "title": "修订", "target_id": note["id"],
                 "mode": "replace", "revised_markdown": str(revision)}
        preview = self.run_plan([entry])
        self.run_plan(apply=True, token=preview["expected_token"])
        self.assertIn("完整修订正文", self.library.get(note["id"])["markdown"])
        revision.write_text(revision.read_text().replace("- [x] 保留勾选", "- [ ] 保留勾选"))
        entry["topic_key"] = "second revision"
        rejected = self.run_plan([entry])
        self.assertIn("checkboxes", rejected["errors"][0]["error"])

    def test_evidence_requires_user_and_existing_directory(self):
        item = self.job("# 旧助手\n\n建议。\n", role="assistant")
        result = self.run_plan([{"item_id": item, "title": "旧助手", "path": "分类/旧助手.md"},
                                {"item_id": self.item, "title": "不存在分类", "path": "不存在/新稿.md"}])
        self.assertEqual(len(result["errors"]), 2)
        self.assertIn("user evidence", result["errors"][0]["error"])
        self.assertIn("existing category", result["errors"][1]["error"])

    def test_bundle_records_real_origins_and_second_source_affects_identity(self):
        primary = {"account_key": "account-a", "conversation_id": "conversation-a"}
        secondary = {"account_key": "account-b", "conversation_id": "conversation-b",
                     "zip_provenance": {"zip_sha256": "b" * 64}}
        material = {"source": primary, "sources": [primary, secondary],
                    "editorial": {"bundle_item_ids": ["member-a", "member-b"]},
                    "bundle": {"version": "test-bundle", "bundle_item_ids": ["member-a", "member-b"]},
                    "messages": [{"id": "m001", "source_index": 0, "source_message_id": "shared-id",
                                  "role": "user", "text": "第一账号自述"},
                                 {"id": "m002", "source_index": 1, "source_message_id": "shared-id",
                                  "role": "user", "text": "第二账号自述"}],
                    "personal_evidence_ids": ["m001", "m002"]}
        item = self.job("# 聚合稿\n\n两个来源。\n", material=material)
        workspace = notes.Workspace(self.workspace)
        entry = {"item_id": item, "path": "分类/聚合.md"}
        _, provenance, source_id = delivery.load_job(workspace, entry)
        self.assertEqual(provenance["sources"], [primary, secondary])
        self.assertEqual(provenance["bundle_item_ids"], ["member-a", "member-b"])
        self.assertEqual(provenance["bundle"], material["bundle"])
        self.assertEqual(provenance["evidence"][1]["account_key"], "account-b")
        self.assertEqual(provenance["evidence"][1]["conversation_id"], "conversation-b")
        self.assertEqual(provenance["evidence"][1]["source_index"], 1)
        reordered = deepcopy(material)
        reordered["sources"].reverse()
        reordered["source"] = reordered["sources"][0]
        for message in reordered["messages"]:
            message["source_index"] = 1 - message["source_index"]
        reordered["messages"].reverse()
        other_item = self.job("# 顺序不同\n\n仍是同一证据。\n", material=reordered)
        other_entry = {**entry, "item_id": other_item}
        self.assertEqual(source_id, delivery.load_job(workspace, other_entry)[2])
        changed = deepcopy(material)
        changed["messages"][1]["text"] += "新增细节"
        changed_item = self.job("# 第二来源更新\n\n新证据。\n", material=changed)
        self.assertNotEqual(source_id, delivery.load_job(workspace, {**entry, "item_id": changed_item})[2])
        invalid = deepcopy(material)
        invalid["messages"][1].pop("source_index")
        invalid_item = self.job("# 错误来源\n\n拒绝。\n", material=invalid)
        with self.assertRaisesRegex(ValueError, "lacks source_index"):
            delivery.load_job(workspace, {**entry, "item_id": invalid_item})

    def combined_entry(self):
        material = {"source": {"account_key": "account-b", "conversation_id": "conversation-b"},
                    "messages": [{"id": "m001", "source_message_id": "second-message", "role": "user",
                                  "text": "第二部分实际材料"}], "personal_evidence_ids": ["m001"]}
        second = self.job("# 第二部分\n\n第二部分生成正文。\n", material=material)
        combined = self.base / "combined.md"
        combined.write_text("# 完整主题\n\n## 第一部分\n\n新增内容。\n\n## 第二部分\n\n第二部分生成正文。\n")
        return {"item_id": self.item, "additional_item_ids": [second], "title": "完整主题",
                "path": "分类/完整主题.md", "combined_markdown": str(combined)}, second, combined

    def test_combined_notes_keep_all_jobs_and_stable_deduplicated_identity(self):
        entry, second, combined = self.combined_entry()
        workspace = notes.Workspace(self.workspace)
        markdown, provenance, source_id = delivery.load_job(workspace, entry)
        self.assertEqual(markdown, combined.read_text())
        self.assertEqual(provenance["combined_markdown_sha256"], delivery.sha(combined.read_bytes()))
        self.assertEqual({j["item_id"] for j in provenance["jobs"]}, {self.item, second})
        self.assertEqual({m["account_key"] for m in provenance["evidence"]}, {"account-a", "account-b"})
        self.assertTrue(all(j["input_sha256"] and j["note_sha256"] for j in provenance["jobs"]))
        reordered = {**entry, "item_id": second, "additional_item_ids": [self.item, second, self.item]}
        self.assertEqual(source_id, delivery.load_job(workspace, reordered)[2])
        preview = self.run_plan([entry])
        result = self.run_plan(apply=True, token=preview["expected_token"])
        self.assertIn("第二部分生成正文", self.library.get(result["items"][0]["id"])["markdown"])
        retained = self.library._read_state()["sources"][source_id]["source"]
        self.assertEqual(len(retained["jobs"]), 2)

    def test_combined_actual_file_bytes_are_guarded_by_preview_token(self):
        entry, _, combined = self.combined_entry()
        preview = self.run_plan([entry])
        combined.write_text(combined.read_text() + "\n合成稿又变化。\n")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "token differs"):
            self.run_plan(apply=True, token=preview["expected_token"])
        self.assertEqual(before, self.snapshot())
        combined.write_text("---\n---\n# 禁止新稿frontmatter\n")
        rejected = self.run_plan([entry])
        self.assertIn("frontmatter", rejected["errors"][0]["error"])

    def test_combined_cannot_bypass_additional_note_state_review_or_output_hash(self):
        entry, second, _ = self.combined_entry()
        workspace = notes.Workspace(self.workspace)
        job_id = workspace.item(second)["job_id"]
        with sqlite3.connect(self.workspace / "state.sqlite3") as db:
            db.execute("UPDATE jobs SET state='queued' WHERE id=?", (job_id,))
        rejected = self.run_plan([entry])
        self.assertIn("completed draft", rejected["errors"][0]["error"])
        directory = self.workspace / "jobs" / job_id
        result = bulk.read(directory / "result.json")
        bulk.save(directory / "result.json", {**result, "review": {"status": "needs_review"}})
        with sqlite3.connect(self.workspace / "state.sqlite3") as db:
            db.execute("UPDATE jobs SET state='review' WHERE id=?", (job_id,))
        rejected = self.run_plan([entry])
        self.assertIn("review_confirmed", rejected["errors"][0]["error"])
        entry["review_confirmed"] = True
        self.assertFalse(self.run_plan([entry])["errors"])
        bulk.atomic_bytes(directory / "note.md", b"Changed output despite existing combined file")
        rejected = self.run_plan([entry])
        self.assertIn("Note file changed", rejected["errors"][0]["error"])

    def test_combined_existing_replace_keeps_yaml_and_checkbox_guards(self):
        entry, _, combined = self.combined_entry()
        note = self.library.create("# 原主题\n\n- [x] 用户确认\n\n原正文。\n", title="原主题", path="分类/原主题.md")
        entry.pop("path")
        entry.update({"target_id": note["id"], "mode": "replace", "revised_markdown": str(combined)})
        combined.write_text(note["markdown"] + "\n第二组生成内容。\n")
        preview = self.run_plan([entry])
        self.assertFalse(preview["errors"])
        self.run_plan(apply=True, token=preview["expected_token"])
        current = self.library.get(note["id"])
        self.assertEqual(current["properties"], note["properties"])
        self.assertIn("- [x] 用户确认", current["markdown"])
        entry["topic_key"] = "changed revision"
        combined.write_text(combined.read_text().replace("- [x] 用户确认", "- [ ] 用户确认"))
        self.assertIn("checkboxes", self.run_plan([entry])["errors"][0]["error"])


original_commit = Library._commit

if __name__ == "__main__":
    unittest.main()
