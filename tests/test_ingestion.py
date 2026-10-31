import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from chatgpt_pipeline.index import ingest, status
from chatgpt_pipeline.normalize import normalize
from chatgpt_pipeline.verify import encoded, mapping_hash, sha, verify


def package(text="original user text", title="title", current="n1"):
    message = {"id": "m1", "author": {"role": "user"},
               "content": {"content_type": "text", "parts": [text]}, "recipient": "all"}
    raw = {"conversation_id": "c1", "title": title, "current_node": current,
           "mapping": {"n1": {"parent": None, "message": message}}}
    return {"schema_version": 1, "source": "chatgpt", "untrusted_data": True,
            "conversation_id": "c1", "url": "https://chatgpt.com/c/c1", "raw": raw,
            "raw_sha256": sha(encoded(raw)), "content_sha256": mapping_hash(raw["mapping"]),
            "readable_status": "complete_current_branch",
            "messages": [{"id": "m1", "role": "user", "created": 1, "content": copy.deepcopy(message["content"])}]}


def write_batch(root, body, body_status="current", publish=True):
    # Fixtures follow the uploader's byte/raw/mapping hash distinction.
    body["raw_sha256"] = sha(encoded(body["raw"]))
    body["content_sha256"] = mapping_hash(body["raw"]["mapping"])
    obj = f"objects/c1/{body['raw_sha256']}.json"
    data = encoded(body)
    (root / obj).parent.mkdir(parents=True, exist_ok=True)
    (root / obj).write_bytes(data)
    entry = {"id": "c1", "title": body["raw"].get("title"), "body_status": body_status,
             "object": obj, "raw_sha256": body["raw_sha256"], "content_sha256": body["content_sha256"]}
    catalog = {"schema_version": 1, "account_key": root.name, "listing_complete": True,
               "bodies_complete": body_status == "current", "body_count": 1, "conversations": [entry]}
    cat_data = encoded(catalog)
    cat_path = f"catalogs/{sha(cat_data)}.json"
    (root / "catalogs").mkdir(exist_ok=True)
    (root / cat_path).write_bytes(cat_data)
    if publish:
        (root / "catalog.json").write_bytes(cat_data)
    commit = {"schema_version": 1, "type": "chatgpt-upload-commit", "account_key": root.name,
              "catalog": cat_path, "catalog_sha256": sha(cat_data), "object_count": 1,
              "objects": [{"path": obj, "size": len(data), "sha256": sha(data)}]}
    return write_commit(root, commit), obj


def write_commit(root, commit):
    data = encoded(commit)
    (root / "commits").mkdir(exist_ok=True)
    path = root / "commits" / (sha(data) + ".commit.json")
    path.write_bytes(data)
    return path


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.exports = self.base / "exports"
        self.root = self.exports / "account-a"
        self.root.mkdir(parents=True)
        self.db = self.base / "index.sqlite3"

    def ingest(self):
        return ingest(self.exports, self.db)

    def query(self, sql):
        with sqlite3.connect(self.db) as db:
            return db.execute(sql).fetchall()

    def test_repeat_and_source_bytes_unchanged(self):
        write_batch(self.root, package())
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        first = self.ingest()
        self.assertTrue(first["accounts"][0]["current_updated"])
        self.assertEqual(first["index"], self.ingest()["index"])
        self.assertEqual(first["index"], status(self.db))
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_same_ids_changed_text_preserve_versions(self):
        write_batch(self.root, package())
        self.ingest()
        write_batch(self.root, package("edited user text"))
        result = self.ingest()
        self.assertEqual(result["index"]["objects"], 2)
        self.assertEqual(result["index"]["analysis_inputs"], 2)
        self.assertEqual(len(self.query("SELECT DISTINCT text FROM messages")), 2)

    def test_title_change_reuses_analysis(self):
        write_batch(self.root, package())
        self.ingest()
        write_batch(self.root, package(title="new title"))
        result = self.ingest()
        self.assertEqual(result["index"]["objects"], 2)
        self.assertEqual(result["index"]["analysis_inputs"], 1)

    def test_latest_requires_matching_verified_commit(self):
        write_batch(self.root, package())
        self.ingest()
        pointer = self.query("SELECT current_catalog_sha256 FROM accounts")
        latest, _ = write_batch(self.root, package("new text"))
        latest.unlink()
        result = self.ingest()
        self.assertFalse(result["accounts"][0]["current_updated"])
        self.assertEqual(pointer, self.query("SELECT current_catalog_sha256 FROM accounts"))

    def test_corruption_does_not_create_partial_commit(self):
        _, obj = write_batch(self.root, package())
        (self.root / obj).write_text("corrupt")
        result = self.ingest()
        self.assertEqual(result["accounts"][0]["failed_commits"], 1)
        self.assertEqual(result["index"]["objects"], 0)
        self.assertEqual(self.query("SELECT status FROM ingest_attempts"), [("failed",)])

    def test_unknown_and_incomplete_are_recorded(self):
        body = package()
        body["messages"][0]["content"] = {"content_type": "future_type", "value": "data"}
        body["raw"]["mapping"]["n1"]["message"]["content"] = body["messages"][0]["content"]
        body["readable_status"] = "failed"
        write_batch(self.root, body)
        result = self.ingest()
        self.assertEqual(result["index"]["analysis_inputs"], 0)
        self.assertEqual(result["index"]["current"][0]["state"], "blocked")
        self.assertIn("unknown_content_type", self.query("SELECT issues_json FROM normalizations")[0][0])

    def test_stale_is_not_eligible_current_input(self):
        write_batch(self.root, package(), "stale")
        result = self.ingest()
        self.assertEqual(result["index"]["current_analysis_messages"], 0)
        self.assertEqual(result["index"]["current"][0]["body_status"], "stale")

    def test_account_keys_do_not_collide(self):
        write_batch(self.root, package())
        other = self.exports / "account-b"
        other.mkdir()
        write_batch(other, package())
        result = self.ingest()
        self.assertEqual(result["index"]["accounts"], 2)
        self.assertEqual(result["index"]["messages"], 2)

    def test_path_escape_symlink_and_catalog_mismatch_rejected(self):
        path, obj = write_batch(self.root, package())
        commit = json.loads(path.read_bytes())
        commit["objects"][0]["path"] = "../outside.json"
        with self.assertRaises(ValueError):
            verify(self.root, write_commit(self.root, commit))
        original = (self.root / obj).read_bytes()
        (self.root / obj).unlink()
        outside = self.base / "outside.json"
        outside.write_bytes(original)
        (self.root / obj).symlink_to(outside)
        with self.assertRaises(ValueError):
            verify(self.root, path)

    def test_missing_branch_and_branch_switch(self):
        self.assertEqual(normalize(package(current="missing"))["state"], "blocked")
        body = package()
        alternate = copy.deepcopy(body["raw"]["mapping"]["n1"])
        alternate["message"]["id"] = "m2"
        alternate["message"]["content"]["parts"] = ["alternate user text"]
        body["raw"]["mapping"]["n2"] = alternate
        old = normalize(body)
        body["raw"]["current_node"] = "n2"
        body["messages"][0].update(id="m2", content=alternate["message"]["content"])
        new = normalize(body)
        self.assertEqual(new["state"], "ready")
        self.assertNotEqual(old["input_fingerprint"], new["input_fingerprint"])

    def test_tool_calls_and_reasoning_excluded(self):
        body = package()
        body["raw"]["mapping"]["n1"]["message"]["recipient"] = "python"
        self.assertEqual(normalize(body)["analysis_messages"], [])
        body["raw"]["mapping"]["n1"]["message"]["recipient"] = "all"
        body["raw"]["mapping"]["n1"]["message"]["channel"] = "analysis"
        self.assertEqual(normalize(body)["analysis_messages"], [])

    def test_multimodal_transcription_and_unknown_part(self):
        body = package()
        content = {"content_type": "multimodal_text", "parts": [
            {"content_type": "audio_transcription", "text": "available transcript"},
            {"content_type": "image_asset_pointer", "asset_pointer": "unavailable"}]}
        body["messages"][0]["content"] = content
        body["raw"]["mapping"]["n1"]["message"]["content"] = content
        normalized = normalize(body)
        self.assertEqual(normalized["state"], "ready")
        self.assertEqual(normalized["analysis_messages"][0]["text"], "available transcript")
        content["parts"].append({"content_type": "future_asset"})
        self.assertEqual(normalize(body)["state"], "blocked")

    def test_unpublished_catalog_cannot_replace_current(self):
        write_batch(self.root, package())
        self.ingest()
        pointer = self.query("SELECT current_catalog_sha256 FROM accounts")
        write_batch(self.root, package("unpublished new text"), publish=False)
        self.ingest()
        self.assertEqual(pointer, self.query("SELECT current_catalog_sha256 FROM accounts"))

    def test_rehashed_file_with_wrong_mapping_hash_rejected(self):
        body = package()
        path, obj = write_batch(self.root, body)
        commit = json.loads(path.read_bytes())
        body["content_sha256"] = "0" * 64
        data = encoded(body)
        (self.root / obj).write_bytes(data)
        commit["objects"][0].update(size=len(data), sha256=sha(data))
        with self.assertRaisesRegex(ValueError, "Mapping checksum"):
            verify(self.root, write_commit(self.root, commit))

    def test_database_cannot_write_into_exports(self):
        with self.assertRaises(ValueError):
            ingest(self.exports, self.root / "bad.sqlite3")


if __name__ == "__main__":
    unittest.main()
