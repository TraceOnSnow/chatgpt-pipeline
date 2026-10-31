import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chatgpt_pipeline.samples import PROMPT, PROMPT_VERSION, SCHEMA_VERSION, ENDPOINT, prepare, run, save, validate
from chatgpt_pipeline.verify import encoded, sha
from chatgpt_pipeline.index import ingest
from chatgpt_pipeline.review import audit_evidence, report
from test_ingestion import package, write_batch


MESSAGES = [
    {"id": "m001", "source_message_id": "original-user", "role": "user", "text": "I have not verified this approach yet."},
    {"id": "m002", "source_message_id": "original-assistant", "role": "assistant", "text": "Try using a hash table for repeated lookups."}]


def result():
    return {"topic": "Practice", "disposition": "keep", "reason": "Reusable learning experience", "overview": "An unverified approach.",
            "evidence": [{"message_id": "m001", "quote": "not verified this approach"}],
            "fragments": [{"title": "Hash table", "kind": "practice", "summary": "An assistant suggestion, still unverified.",
                           "claims": [{"text": "Assistant proposed hash table lookups.", "attribution": "assistant", "status": "proposed",
                                       "evidence": [{"message_id": "m002", "quote": "Try using a hash table"}]}]}]}


class SampleTests(unittest.TestCase):
    def test_report_audit_collects_all_quote_problems_without_relaxing_matching(self):
        messages = [{"id": "m001", "source_message_id": "source-1", "role": "user",
                     "text": "This is **bold quoted text** in the source."}]
        data = {"evidence": [{"message_id": "m001", "quote": "This is bold quoted text"},
                             {"message_id": "missing", "quote": "invented quote"},
                             {"message_id": "m001", "quote": "source"},
                             {"message_id": "m001", "quote": "bold quoted text"}], "fragments": []}
        audit = audit_evidence(data, messages)
        kinds = {issue["type"] for issue in audit["issues"]}
        self.assertEqual(kinds, {"evidence_count", "non_verbatim_quote", "unknown_message", "quote_length"})
        self.assertEqual(len(audit["checked"]), 4)
        self.assertFalse(audit["checked"][0]["verbatim"])

    def test_report_is_offline_and_includes_cost_of_failed_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            directory = output / "sample-01"
            directory.mkdir()
            raw_source = output / "source.json"
            raw_source.write_bytes(b'{"synthetic":true}')
            source = {"source": {"source_path": str(raw_source), "byte_sha256": sha(raw_source.read_bytes()),
                                 "title": "Synthetic test", "url": "https://example.invalid/test"}, "messages": MESSAGES}
            payload = {"synthetic": True}
            data = result()
            data["evidence"] *= 4
            response = {"id": "synthetic-id", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(data)}}]}
            save(directory / "input.json", source)
            save(directory / "request.json", payload)
            save(directory / "response.json", response)
            (directory / "response.raw").write_bytes(encoded(response))
            save(directory / "status.json", {"state": "validation_failed", "response_id": "synthetic-id",
                                             "usage": {"prompt_tokens": 100, "completion_tokens": 200, "cost": 0.01}})
            save(output / "manifest.json", {"jobs": [{"name": "sample-01", "input_sha256": sha(encoded(source, sort=True)),
                                                     "request_sha256": sha(encoded(payload, sort=True))}]})
            with patch("urllib.request.OpenerDirector.open") as request:
                outcome = report(output)
                request.assert_not_called()
            self.assertEqual(outcome["reported_cost_usd"], "0.01")
            self.assertEqual(outcome["strict_validation_passed"], 0)
            self.assertTrue((output / "report.txt").exists())
            self.assertEqual(json.loads((directory / "status.json").read_text())["state"], "validation_failed")

    def test_references_are_resolved_to_original_ids(self):
        checked = validate(result(), MESSAGES)
        self.assertEqual(checked["checked_evidence"][1]["source_message_id"], "original-assistant")
        self.assertIn("pending", checked["semantic_review"])

    def test_fabricated_quote_and_unknown_message_are_rejected(self):
        for evidence in ({"message_id": "missing", "quote": "not verified this approach"},
                         {"message_id": "m001", "quote": "this quotation was invented"}):
            data = result()
            data["evidence"] = [evidence]
            with self.assertRaises(ValueError):
                validate(data, MESSAGES)

    def test_assistant_evidence_cannot_be_assigned_to_user(self):
        data = result()
        data["fragments"][0]["claims"][0]["attribution"] = "user"
        with self.assertRaisesRegex(ValueError, "role mismatch"):
            validate(data, MESSAGES)

    def test_assistant_cannot_confirm_decisions(self):
        data = result()
        data["fragments"][0]["claims"][0]["status"] = "decided"
        with self.assertRaisesRegex(ValueError, "combination"):
            validate(data, MESSAGES)

    def test_skip_requires_evidence_and_no_fragments(self):
        data = result()
        data.update(disposition="skip", fragments=[])
        self.assertTrue(validate(data, MESSAGES)["references_valid"])
        data["evidence"] = []
        with self.assertRaises(ValueError):
            validate(data, MESSAGES)

    def test_preparation_verifies_sources_and_keeps_review_labels_out_of_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "exports/account-a"
            root.mkdir(parents=True)
            write_batch(root, package())
            database = base / "index.sqlite3"
            ingest(root.parent, database)
            selection = base / "selection.json"
            save(selection, [{"account_key": "account-a", "conversation_id": "c1", "review_only": "EXPECTED_SKIP_SECRET_LABEL"}])
            output = base / "runtime/pilot"
            with patch("chatgpt_pipeline.samples.__file__", str(base / "src/chatgpt_pipeline/samples.py")):
                prepared = prepare(database, selection, output, "deepseek/test")
                self.assertEqual(prepared["jobs"], 1)
                payload = (output / "sample-01/request.json").read_text()
                self.assertNotIn("EXPECTED_SKIP_SECRET_LABEL", payload)
                original = json.loads((output / "sample-01/input.json").read_text())
                self.assertEqual(original["messages"][0]["source_message_id"], "m1")
                self.assertEqual(original["source"]["account_key"], "account-a")
                (root / "catalog.json").write_text("new catalog still uploading")
                with self.assertRaisesRegex(ValueError, "behind latest"):
                    prepare(database, selection, base / "runtime/new-pilot", "deepseek/test")
                self.assertFalse((base / "runtime/new-pilot").exists())

    def test_truncated_response_is_saved_but_never_marked_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            directory = output / "sample-01"
            directory.mkdir()
            payload = {"model": "deepseek/test", "max_tokens": 1000,
                       "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": "archive"}]}
            source = {"messages": MESSAGES}
            save(directory / "input.json", source)
            save(directory / "request.json", payload)
            save(directory / "status.json", {"state": "prepared"})
            save(output / "manifest.json", {"endpoint": ENDPOINT, "prompt_version": PROMPT_VERSION, "schema_version": SCHEMA_VERSION,
                                            "model": "deepseek/test", "max_calls": 1, "jobs": [
                                                {"name": directory.name, "request_sha256": sha(encoded(payload, sort=True)),
                                                 "input_sha256": sha(encoded(source, sort=True))}]})
            response = io.BytesIO(encoded({"id": "synthetic-response", "model": "deepseek/test",
                                          "usage": {"prompt_tokens": 100, "completion_tokens": 1000, "cost": 0.01},
                                          "choices": [{"finish_reason": "length", "message": {"content": "{partial"}}]}))
            response.status = 200
            with patch("chatgpt_pipeline.samples.credential", return_value="synthetic-test-key"), \
                 patch("urllib.request.OpenerDirector.open", return_value=response):
                outcome = run(output)
            self.assertEqual(outcome["validated"], 0)
            state = json.loads((directory / "status.json").read_text())
            self.assertEqual(state["state"], "validation_failed")
            self.assertEqual(state["usage"]["cost"], 0.01)
            self.assertTrue((directory / "response.raw").exists())
            self.assertFalse((directory / "result.json").exists())

    def test_timeout_cannot_be_retried_and_stops_remaining_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            jobs = []
            for n in (1, 2):
                directory = output / f"sample-{n:02d}"
                directory.mkdir()
                payload = {"model": "deepseek/test", "max_tokens": 1000,
                           "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": "archive"}]}
                source = {"messages": MESSAGES}
                save(directory / "input.json", source)
                save(directory / "request.json", payload)
                save(directory / "status.json", {"state": "prepared"})
                jobs.append({"name": directory.name, "request_sha256": sha(encoded(payload, sort=True)),
                             "input_sha256": sha(encoded(source, sort=True))})
            save(output / "manifest.json", {"endpoint": ENDPOINT, "prompt_version": PROMPT_VERSION, "schema_version": SCHEMA_VERSION,
                                            "model": "deepseek/test", "max_calls": 2, "jobs": jobs})
            with patch("chatgpt_pipeline.samples.credential", return_value="synthetic-test-key"), \
                 patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError) as request:
                outcome = run(output)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(outcome["attempted"], 1)
                state = json.loads((output / "sample-01/status.json").read_text())
                self.assertEqual(state["state"], "needs_reconciliation")
                self.assertFalse((output / "sample-02/attempt.json").exists())
                with self.assertRaisesRegex(ValueError, "already attempted"):
                    run(output)
                self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
