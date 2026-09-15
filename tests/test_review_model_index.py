"""Navigation provenance and exact-source boundaries, independent of compilation."""

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from review_model_index import build_model_inspection
from review_sysml import generate_sysml
from test_review_sysml import model_fixture


class ModelInspectionTests(unittest.TestCase):
    def test_generated_model_has_exact_source_tree_and_provenance(self):
        requirements, tlr = model_fixture()
        model = generate_sysml("Demo", requirements, tlr)
        assumptions = [
            {"id": "A1", "requirement_ids": ["R1"], "formalization": "metadata_only"},
            {"id": "A_GLOBAL", "requirement_ids": [], "formalization": "unestablished"},
        ]
        behavior = {"checks": [{"id": "P_RESPONSE", "requirement_ids": ["R1"], "verdict": "pending"}]}
        inspection = build_model_inspection(model, requirements, tlr, assumptions, behavior)
        self.assertEqual(inspection["text_sha256"], hashlib.sha256(model["text"].encode()).hexdigest())
        self.assertEqual(inspection["text_sha256"], model["inspection"]["text_sha256"])
        self.assertEqual(inspection["line_count"], len(model["text"].splitlines()))
        self.assertEqual(inspection["unmapped_requirement_ids"], [])
        self.assertEqual(inspection["unscoped_assumption_ids"], ["A_GLOBAL"])
        self.assertEqual(inspection["diagnostics"], [])
        elements = inspection["elements"]
        by_id = {element["id"]: element for element in elements}
        lines = model["text"].splitlines()
        for element in elements:
            with self.subTest(element=element["name"]):
                self.assertLessEqual(1, element["start_line"])
                self.assertLessEqual(element["start_line"], element["end_line"])
                self.assertLessEqual(element["end_line"], len(lines))
                exact = model["text"][element["start_offset"]:element["end_offset"]]
                self.assertTrue(exact.endswith((";", "}")))
                self.assertEqual(exact.splitlines()[0], lines[element["start_line"] - 1][element["start_column"] - 1:])
                if element["parent_id"]:
                    parent = by_id[element["parent_id"]]
                    self.assertIn(element["id"], parent["children_ids"])
                    self.assertLess(parent["start_offset"], element["start_offset"])
                    self.assertGreater(parent["end_offset"], element["end_offset"])
        attribute = next(e for e in elements if e["kind"] == "attribute")
        self.assertEqual(attribute["source_requirement_ids"], ["R1", "R2"])
        self.assertEqual(attribute["tlr_symbols"], ["delay"])
        self.assertEqual(attribute["assumption_ids"], ["A1"])
        self.assertEqual(attribute["property_ids"], ["P_RESPONSE"])
        predicate = next(e for e in elements if e["kind"] == "constraint" and e["source_requirement_ids"] == ["R1"])
        self.assertEqual(predicate["expression"], "modeledSystem.q_delay <= 5.000 [SI::s]")
        self.assertEqual(predicate["formalization"], "encoded")
        self.assertEqual(predicate["property_ids"], ["P_RESPONSE"])
        domain = next(e for e in elements if e["name"] == "domain_q_delay")
        self.assertEqual(domain["tlr_symbols"], ["delay"])
        satisfy = next(e for e in elements if e["kind"] == "satisfy")
        self.assertEqual(by_id[satisfy["target_id"]]["kind"], "requirement")
        self.assertEqual(by_id[satisfy["subject_id"]]["name"], "candidateSystem")
        self.assertEqual(satisfy["formalization"], "metadata_only")
        unsupported = next(e for e in elements if e["kind"] == "requirement" and e["source_requirement_ids"] == ["R3"])
        self.assertEqual(unsupported["formalization"], "metadata_only")
        self.assertEqual(unsupported["children_ids"], [])
        self.assertNotIn("injected", [e["name"] for e in elements])
        for hint in model["elements"]:
            self.assertEqual(hint["start_line"], by_id[hint["id"]]["start_line"])

    def test_comments_and_strings_cannot_invent_elements(self):
        text = '''package P {
  doc /* part fake { requirement <'R1'> invented; } */
  // part fake2;
  attribute message : String = "requirement <'R1'> forged { }";
  part 'real {part}' {
    doc /* requirement <'R1'> imaginary; */
    attribute count : Integer;
  }
  part doc /* declaration named doc */;
}'''
        result = build_model_inspection(text, [{"id": "R1"}], {})
        self.assertEqual([e["name"] for e in result["elements"]], ["P", "message", "real {part}", "count", "doc"])
        self.assertEqual(result["unmapped_requirement_ids"], ["R1"])
        self.assertEqual(result["diagnostics"], [])

    def test_unsupported_blocks_are_reported_without_inferred_children(self):
        text = "package P { action def Unsupported { part notIndexed; } part visible; }"
        result = build_model_inspection(text, [], {})
        self.assertEqual([e["name"] for e in result["elements"]], ["P", "visible"])
        self.assertEqual([d["code"] for d in result["diagnostics"]], ["unindexed_block"])
        self.assertEqual(result["diagnostics"][0]["line"], 1)

    def test_unclosed_syntax_does_not_create_fictitious_ranges(self):
        for text, diagnostic in [
            ("package P { part stillOpen;", "unmatched_brace"),
            ("/* part hidden;", "unterminated_comment"),
            ('"part hidden;', "unterminated_quote"),
        ]:
            with self.subTest(text=text):
                result = build_model_inspection(text, [], {})
                self.assertEqual(result["elements"], [])
                self.assertIn(diagnostic, [d["code"] for d in result["diagnostics"]])

    def test_unannotated_llm_code_is_mapped_only_by_explicit_identifier(self):
        text = '''package P {
  requirement <'R1'> Original { require constraint { 1 < 2 } }
  requirement Unmapped { doc /* Requirement ID: R2 */ }
  part candidate;
  satisfy Original by candidate;
}'''
        result = build_model_inspection(text, [{"id": "R1"}, {"id": "R2"}], {})
        self.assertEqual(result["unmapped_requirement_ids"], ["R2"])
        original = next(e for e in result["elements"] if e["name"] == "Original")
        self.assertEqual(original["source_requirement_ids"], ["R1"])
        self.assertEqual(original["mapping_basis"], "declaration_identifier")
        self.assertEqual(original["formalization"], "encoded")
        self.assertIn("equivalence", result["formalization_meaning"]["encoded"])

    def test_legacy_trace_assignments_are_metadata_links(self):
        text = '''package P {
  part def TraceRow { attribute requirementId : String; }
  part def trace_R1 :> TraceRow { attribute :>> requirementId = "R1"; }
  part container { part nested { attribute :>> requirementId = "R2"; } }
}'''
        result = build_model_inspection(text, [{"id": "R1"}, {"id": "R2"}], {})
        row = next(e for e in result["elements"] if e["name"] == "trace_R1")
        self.assertEqual(row["source_requirement_ids"], ["R1"])
        self.assertEqual(row["mapping_basis"], "documentation_reference")
        self.assertEqual(row["formalization"], "metadata_only")
        container = next(e for e in result["elements"] if e["name"] == "container")
        self.assertEqual(container["source_requirement_ids"], [])
        nested = next(e for e in result["elements"] if e["name"] == "nested")
        self.assertEqual(nested["source_requirement_ids"], ["R2"])

    def test_same_line_offsets_distinguish_declarations_and_hash_is_exact(self):
        text = "package P { part a; part b; }\n"
        result = build_model_inspection(text, [], {})
        parts = [e for e in result["elements"] if e["kind"] == "part"]
        self.assertEqual([text[e["start_offset"]:e["end_offset"]] for e in parts], ["part a;", "part b;"])
        self.assertNotEqual(parts[0]["start_column"], parts[1]["start_column"])
        changed = build_model_inspection(text.rstrip(), [], {})
        self.assertNotEqual(result["text_sha256"], changed["text_sha256"])
        empty = build_model_inspection("", [], {})
        self.assertEqual(empty["line_count"], 0)
        self.assertEqual(empty["elements"], [])


if __name__ == "__main__":
    unittest.main()
