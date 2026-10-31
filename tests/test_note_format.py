from copy import deepcopy
import unittest

from chatgpt_pipeline import note_format, notes


class NoteFormatTests(unittest.TestCase):
    def setUp(self):
        self.block = "```py\nx = 1\n```"
        self.material = {
            "messages": [{"id": "m001"}, {"id": "m002"}, {"id": "m003"}],
            "personal_evidence_ids": ["m001"],
            "codes": {"c001": {"message_id": "m002", "text": self.block},
                      "c002": {"message_id": "m003", "text": "~~~js\nx = 2\n~~~"}}}
        self.result = {"title": "A personal note", "markdown": "# A personal note\n\nThe original idea remains uncertain, with its own context.\n",
                       "source_message_ids": ["m001"], "verbatim_code_ids": []}

    def canonical(self, result, stage="draft"):
        before, material_before = deepcopy(result), deepcopy(self.material)
        normalized, changes = note_format.canonicalize(result, self.material, stage)
        self.assertEqual(result, before)
        self.assertEqual(self.material, material_before)
        return normalized, changes

    def test_exact_inline_code_becomes_placeholder_with_explicit_code_source(self):
        original = {**self.result, "markdown": self.result["markdown"] + self.block + "\nA trailing paragraph."}
        normalized, changes = self.canonical(original)
        self.assertEqual(normalized["markdown"], self.result["markdown"] + "[[CODE:c001]]\nA trailing paragraph.")
        self.assertEqual(normalized["source_message_ids"], ["m001", "m002"])
        self.assertEqual(normalized["verbatim_code_ids"], ["c001"])
        self.assertEqual([change["operation"] for change in changes],
                         ["replace_exact_code_block", "derive_code_ids", "add_code_source_ref"])
        self.assertEqual(notes.validate(normalized, self.material, "draft"), normalized)
        self.assertIn(self.block, notes.expand_code(normalized, self.material))
        self.assertNotIn("x = 1", repr(changes))

    def test_no_code_prose_and_review_judgment_untouched(self):
        result = {**self.result, "verdict": "needs_review", "review_reason": "Personal attribution uncertain"}
        self.assertEqual(self.canonical(result, "review"), (result, []))

    def test_known_type_and_unused_omission_can_be_combined(self):
        original = {key: value for key, value in self.result.items() if key != "verbatim_code_ids"}
        normalized, changes = self.canonical({**original, "type": "json_object"})
        self.assertEqual(normalized, self.result)
        self.assertEqual(len(changes), 2)
        notes.validate(normalized, self.material, "draft")

    def test_existing_placeholders_repair_stale_valid_ids_and_source_links(self):
        original = {**self.result, "markdown": self.result["markdown"] + "[[CODE:c001]]\n[[CODE:c001]]",
                    "verbatim_code_ids": ["c002"]}
        normalized, changes = self.canonical(original)
        self.assertEqual(normalized["verbatim_code_ids"], ["c001"])
        self.assertEqual(normalized["source_message_ids"], ["m001", "m002"])
        self.assertEqual(normalized["markdown"], original["markdown"])
        notes.validate(normalized, self.material, "draft")
        self.assertEqual(len(changes), 2)

    def test_removing_unused_valid_code_ids_does_not_add_their_sources(self):
        normalized, _ = self.canonical({**self.result, "verbatim_code_ids": ["c002"]})
        self.assertEqual(normalized, self.result)

    def test_unknown_fields_or_missing_review_fields_remain_invalid(self):
        for result, stage in [({**self.result, "extra": "private"}, "draft"),
                              ({**self.result, "type": "other"}, "draft"),
                              (self.result, "review"),
                              ({**self.result, "verdict": "ready"}, "review")]:
            with self.subTest(stage=stage, fields=set(result)):
                self.assertEqual(self.canonical(result, stage), (result, []))
                with self.assertRaises(ValueError):
                    notes.validate(result, self.material, stage)

    def test_missing_or_invalid_general_sources_never_inferred(self):
        body = self.result["markdown"] + self.block
        for refs in (None, [], ["m999"], ["m001", "m001"], "m001"):
            result = {**self.result, "markdown": body, "source_message_ids": refs}
            if refs is None:
                del result["source_message_ids"]
            self.assertEqual(self.canonical(result), (result, []))

    def test_invalid_declared_or_actual_code_ids_stay_invalid(self):
        for ids, suffix in [(["c999"], self.block), (["c001", "c001"], self.block),
                            ("c001", self.block), ([], "[[CODE:c999]]"),
                            ([], "[[CODE:c001"), ([], "[[CODE:c001]\n")]:
            result = {**self.result, "markdown": self.result["markdown"] + suffix, "verbatim_code_ids": ids}
            self.assertEqual(self.canonical(result), (result, []))

    def test_missing_code_array_with_used_code_is_not_synthesized(self):
        for suffix in (self.block, "[[CODE:c001]]"):
            result = {key: value for key, value in self.result.items() if key != "verbatim_code_ids"}
            result["markdown"] += suffix
            self.assertEqual(self.canonical(result), (result, []))

    def test_modified_or_novel_code_and_fence_language_stay_untouched(self):
        for block in (self.block.replace("x = 1", "x = 2"), self.block.replace("```py", "```python"),
                      self.block.replace("```", "~~~"), self.block[:-3], "`" + self.block + "`",
                      self.block.replace("x = 1", "x = 1 ")):
            result = {**self.result, "markdown": self.result["markdown"] + block}
            self.assertEqual(self.canonical(result), (result, []))
        # A partial successful conversion must not alter unsupported mixed code.
        result = {**self.result, "markdown": self.result["markdown"] + self.block + "\n```py\nnew()\n```"}
        self.assertEqual(self.canonical(result), (result, []))

    def test_inline_fence_markers_are_not_rewritten(self):
        result = {**self.result, "markdown": self.result["markdown"] + "Use ``` inline."}
        self.assertEqual(self.canonical(result), (result, []))

    def test_nested_shorter_fences_and_longer_closing_fence(self):
        for block in ("````md\n```py\nx = 1\n```\n````", "~~~text\n```\nexample\n```\n~~~~"):
            self.material["codes"]["c001"]["text"] = block
            result = {**self.result, "markdown": self.result["markdown"] + block + "\n"}
            normalized, changes = self.canonical(result)
            self.assertEqual(normalized["markdown"], self.result["markdown"] + "[[CODE:c001]]\n")
            self.assertEqual(changes[0]["code_id"], "c001")
            notes.validate(normalized, self.material, "draft")

    def test_ambiguous_duplicate_exact_blocks_require_existing_selection(self):
        self.material["codes"]["c002"]["text"] = self.block
        result = {**self.result, "markdown": self.result["markdown"] + self.block}
        self.assertEqual(self.canonical(result), (result, []))
        for changes, expected in [({"source_message_ids": ["m001", "m002"]}, "c001"),
                                  ({"verbatim_code_ids": ["c002"]}, "c002")]:
            normalized, _ = self.canonical({**result, **changes})
            self.assertEqual(normalized["verbatim_code_ids"], [expected])
            notes.validate(normalized, self.material, "draft")
        conflicting = {**result, "source_message_ids": ["m001", "m002"], "verbatim_code_ids": ["c002"]}
        self.assertEqual(self.canonical(conflicting), (conflicting, []))
        both = {**result, "source_message_ids": ["m001", "m002", "m003"], "verbatim_code_ids": ["c001", "c002"]}
        self.assertEqual(self.canonical(both), (both, []))

    def test_repeat_is_idempotent(self):
        normalized, _ = self.canonical({**self.result, "markdown": self.result["markdown"] + self.block})
        self.assertEqual(self.canonical(normalized), (normalized, []))


if __name__ == "__main__":
    unittest.main()
