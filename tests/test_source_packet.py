"""Compact prepared packets preserve the existing contextualized-source contract."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from canonical_cli import sources_from_file
from review_profile import MAX_INPUT_BYTES
from source_packet import PREPARED_PACKET_SCHEMA, expand_prepared_packet


def packet():
    return {
        "schema": PREPARED_PACKET_SCHEMA,
        "shared_context": [{"id": "D1", "kind": "definition", "text": "Voltage is measured in volts.",
                            "citations": [{"page": 2, "quote": "Voltage is measured in volts."}],
                            "context_ids": [], "note": "Reviewed definition."}],
        "requirements": [
            {"id": rid, "text": text, "source": {
                "document": "source.pdf", "location": "PDF page 1", "pages": [1],
                "citations": [{"page": 1, "quote": text}], "context_ids": ["D1"],
                "context": {"related": [], "scope_note": "Keep this field unchanged."},
                "review": {"reviewer": "Engineer", "note": "Reviewed against the original PDF."}}}
            for rid, text in (("R1", "The battery voltage shall be at most 28 V."),
                              ("R2", "The battery voltage shall be at least 12 V."))]}


class SourcePacketTests(unittest.TestCase):
    def import_value(self, value):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            return sources_from_file(path)

    def test_expand_preserves_every_field_without_aliases(self):
        compact = packet()
        compact["requirements"][0]["extra_metadata"] = {"unchanged": True}
        original = deepcopy(compact)
        expanded = expand_prepared_packet(compact)
        expected = deepcopy(compact["requirements"])
        for row in expected:
            row["source"]["context"]["shared"] = deepcopy(compact["shared_context"])
        self.assertEqual(expanded, expected)
        expanded[0]["source"]["context"]["shared"][0]["citations"][0]["page"] = 99
        expanded[0]["source"]["review"]["reviewer"] = "Changed only in returned data"
        self.assertEqual(compact, original)
        self.assertEqual(expanded[1]["source"]["context"]["shared"], original["shared_context"])

    def test_compact_import_restores_context_and_exact_source(self):
        compact = packet()
        compact["requirements"][0]["text"] = "  Preserve exact source wording — including whitespace.  "
        self.assertEqual(self.import_value(compact), expand_prepared_packet(compact))

    def test_small_upload_with_large_expanded_context_is_accepted_losslessly(self):
        compact = packet()
        compact["shared_context"] = [
            {"id": f"D{i}", "kind": "definition", "text": (f"Definition {i}. " + "Context ") * 80,
             "citations": [{"page": 2, "quote": "Source excerpt."}], "context_ids": [], "note": ""}
            for i in range(20)]
        compact["requirements"] = [deepcopy(compact["requirements"][0]) for _ in range(30)]
        for i, row in enumerate(compact["requirements"]):
            row["id"] = f"R{i}"
        expanded = expand_prepared_packet(compact)
        self.assertLess(len(json.dumps(compact).encode()), MAX_INPUT_BYTES)
        self.assertGreater(len(json.dumps(expanded).encode()), MAX_INPUT_BYTES)
        self.assertEqual(self.import_value(compact), expanded)

    def test_empty_shared_context_is_explicit_and_valid(self):
        compact = packet()
        compact["shared_context"] = []
        for row in self.import_value(compact):
            self.assertEqual(row["source"]["context"]["shared"], [])

    def test_legacy_array_and_object_retain_context_unchanged(self):
        expanded = expand_prepared_packet(packet())
        self.assertEqual(self.import_value(expanded), expanded)
        self.assertEqual(self.import_value({"requirements": expanded}), expanded)
        self.assertEqual(self.import_value({"schema": "legacy_metadata/1", "requirements": expanded}), expanded)

    def test_unknown_schema_and_registry_without_schema_are_rejected(self):
        for version in ("prepared_source_packet/2", "prepared_source_packet", None):
            with self.subTest(schema=version):
                compact = packet()
                if version is None:
                    del compact["schema"]
                else:
                    compact["schema"] = version
                with self.assertRaisesRegex(ValueError, "schema"):
                    self.import_value(compact)

    def test_missing_registry_and_unknown_top_level_metadata_are_rejected(self):
        compact = packet()
        del compact["shared_context"]
        with self.assertRaisesRegex(ValueError, "exactly"):
            self.import_value(compact)
        compact = packet()
        compact["unapplied_context"] = {"text": "Do not silently discard this material."}
        with self.assertRaisesRegex(ValueError, "exactly"):
            self.import_value(compact)

    def test_malformed_shared_context_is_rejected(self):
        for shared in (None, {}, [None], [{"id": "D1", "kind": "definition"}],
                       [{"id": "D1", "kind": [], "text": "Definition."}],
                       [{"id": "D1", "kind": "requirement", "text": "An obligation."}],
                       [{"id": "", "kind": "definition", "text": "Definition."}]):
            with self.subTest(shared=shared):
                compact = packet()
                compact["shared_context"] = shared
                with self.assertRaises(ValueError):
                    self.import_value(compact)

    def test_duplicate_registry_ids_and_requirement_collisions_are_rejected(self):
        compact = packet()
        compact["shared_context"].append(deepcopy(compact["shared_context"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate or conflicting"):
            self.import_value(compact)
        compact = packet()
        compact["shared_context"][0]["id"] = "R1"
        with self.assertRaisesRegex(ValueError, "Duplicate or conflicting"):
            self.import_value(compact)

    def test_compact_rows_need_explicit_source_context_objects(self):
        for source in (None, "page 1", {}, {"context": []}):
            with self.subTest(source=source):
                compact = packet()
                compact["requirements"][0]["source"] = source
                with self.assertRaisesRegex(ValueError, "source.context"):
                    self.import_value(compact)

    def test_inline_shared_context_is_rejected_even_if_identical(self):
        compact = packet()
        compact["requirements"][0]["source"]["context"]["shared"] = deepcopy(compact["shared_context"])
        with self.assertRaisesRegex(ValueError, "ambiguous inline"):
            self.import_value(compact)

    def test_input_byte_limit_remains_in_force_before_expansion(self):
        compact = packet()
        compact["shared_context"][0]["text"] = "x" * MAX_INPUT_BYTES
        with patch("source_packet.expand_prepared_packet", side_effect=AssertionError("Must not expand oversized input")):
            with self.assertRaisesRegex(ValueError, "Input exceeds"):
                self.import_value(compact)


if __name__ == "__main__":
    unittest.main()
