"""Component-level source acceptance, with deterministic offline reviewer stubs."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from canonical_source_review import (COMPONENT_DIMENSIONS, review_candidate,
    review_feedback, review_has_repair_findings, source_review_available, validate_review)
from canonical_obligations import validate_inventory
from canonical_tlr import validate_tlr
from test_canonical_source_review import fixture as scalar_fixture, response_for


def fixture():
    sources, tlr = scalar_fixture()
    context_quote = "The battery terminal voltage shall be at least 20 V."
    sources[0]["source"]["context"] = [{"id": "LOWER_BOUND", "quote": context_quote}]
    first = deepcopy(tlr["requirements"][0]["formula"])
    lower = deepcopy(tlr["requirements"][1]["formula"])
    tlr["requirements"][0]["formula"] = {"op": "and", "args": [first, lower]}
    inventory = {"schema": "mbse_obligation_inventory/1", "requirements": []}
    for position, row in enumerate(tlr["requirements"]):
        rid = row["id"]
        entries = [("<=", "28", rid, sources[position]["text"], "/formula/args/0"),
                   (">=", "20", "LOWER_BOUND", context_quote, "/formula/args/1")] if position == 0 else [
                   (">=", "20", rid, sources[position]["text"], "/formula")]
        item = {"id": rid, "context": [{"source_id": "LOWER_BOUND", "role": "additional_obligation",
                "reason": "This lower bound applies to the same battery."}] if position == 0 else [], "obligations": []}
        row["coverage"] = []
        for number, (operator, bound, source_id, quote, path) in enumerate(entries, 1):
            oid = f"{rid}.O{number}"
            item["obligations"].append({"id": oid, "meaning": quote,
                "source_basis": [{"source_id": source_id, "quote": quote}], "kind": "state_constraint",
                "slots": {"subject": "Battery", "scope": "Single observation", "quantity": "Terminal voltage",
                          "unit": "V", "operator": operator, "bound": bound},
                "selection_reason": "Direct numeric restriction with a fixed unit and endpoint.", "limitations": []})
            row["coverage"].append({"obligation_id": oid, "status": "represented", "formula_path": path,
                "slots": {"quantity": [path + "/args/0"], "operator": [path],
                          "bound": [path + "/args/1"], "unit": [path + "/args/1"]}})
        inventory["requirements"].append(item)
    return sources, validate_tlr(tlr, sources), validate_inventory(inventory, sources)


def response(sources, tlr, inventory):
    raw = response_for(sources, tlr)
    for position, row in enumerate(raw["requirements"]):
        row["obligations"] = []
        for component, mapping in zip(inventory["requirements"][position]["obligations"], tlr["requirements"][position]["coverage"]):
            row["obligations"].append({"id": component["id"], "disposition": "pass", "reason": "The source bound is preserved.",
                "dimensions": [{"dimension": name, "status": "pass", "explanation": "The bound and its source scope match."}
                               for name in COMPONENT_DIMENSIONS], "source_basis": deepcopy(component["source_basis"]),
                "ast_paths": [f"/requirements/{position}" + mapping["formula_path"]]})
    return raw


class ObligationSourceReviewTests(unittest.TestCase):
    def test_each_contextual_obligation_requires_an_independent_semantic_review(self):
        sources, tlr, inventory = fixture()
        report = validate_review(response(sources, tlr, inventory), sources, tlr, inventory)
        self.assertEqual(report["eligible_ids"], ["R1", "R2"])
        self.assertEqual(report["component_eligible_ids"], ["R1.O1", "R1.O2", "R2.O1"])
        self.assertEqual(report["structural_coverage"]["status"], "passed")
        self.assertEqual(report["obligation_inventory"], inventory)

    def test_whole_requirement_pass_cannot_replace_a_missing_component_review(self):
        sources, tlr, inventory = fixture()
        raw = response(sources, tlr, inventory)
        raw["requirements"][0]["obligations"].pop()
        report = validate_review(raw, sources, tlr, inventory)
        self.assertEqual(report["eligible_ids"], ["R2"])
        self.assertEqual(report["requirements"][0]["disposition"], "unreviewed")
        self.assertEqual(report["requirements"][0]["obligations"][0]["disposition"], "pass")
        self.assertFalse(source_review_available(report))

    def test_component_failure_is_usable_feedback_even_if_parent_claims_pass(self):
        sources, tlr, inventory = fixture()
        raw = response(sources, tlr, inventory)
        child = raw["requirements"][0]["obligations"][1]
        child["disposition"] = "revise"
        child["dimensions"][1].update(status="fail", explanation="The participant meaning disagrees with the contextual source.")
        report = validate_review(raw, sources, tlr, inventory)
        self.assertEqual(report["eligible_ids"], ["R2"])
        self.assertEqual(report["requirements"][0]["reviewer_disposition"], "pass")
        self.assertEqual(report["requirements"][0]["disposition"], "revise")
        self.assertTrue(source_review_available(report))
        self.assertTrue(review_has_repair_findings(report))

    def test_context_component_cannot_cite_only_the_headline_requirement(self):
        sources, tlr, inventory = fixture()
        raw = response(sources, tlr, inventory)
        raw["requirements"][0]["obligations"][1]["source_basis"] = [{"source_id": "R1", "quote": sources[0]["text"]}]
        report = validate_review(raw, sources, tlr, inventory)
        self.assertEqual(report["eligible_ids"], ["R2"])
        self.assertIn("every inventory source/context", report["requirements"][0]["obligations"][1]["reason"])

    def test_component_cannot_borrow_another_component_formula(self):
        sources, tlr, inventory = fixture()
        raw = response(sources, tlr, inventory)
        raw["requirements"][0]["obligations"][1]["ast_paths"] = ["/requirements/0/formula/args/0"]
        report = validate_review(raw, sources, tlr, inventory)
        self.assertEqual(report["eligible_ids"], ["R2"])
        self.assertEqual(report["requirements"][0]["obligations"][1]["disposition"], "unreviewed")

    def test_missing_mapping_is_a_repair_diagnostic_not_reviewer_unavailability(self):
        sources, tlr, inventory = fixture()
        raw = response(sources, tlr, inventory)
        tlr["requirements"][0]["coverage"].pop()
        # Even an invalid claim of component pass cannot erase structural diagnosis.
        report = validate_review(raw, sources, tlr, inventory)
        self.assertEqual(report["eligible_ids"], ["R2"])
        self.assertTrue(source_review_available(report))
        self.assertTrue(review_has_repair_findings(report))
        feedback = review_feedback(report)
        self.assertEqual(feedback["obligation_inventory"], inventory)
        self.assertIn("MISSING_COMPONENT", [f["code"] for f in feedback["structural_coverage"]["findings"]])
        self.assertEqual(feedback["requirements"][0]["disposition"], "revise")

    def test_wrong_numeric_endpoint_cannot_be_approved_by_semantic_reviewer(self):
        sources, tlr, inventory = fixture()
        tlr["requirements"][0]["formula"]["args"][0]["op"] = "<"
        report = validate_review(response(sources, tlr, inventory), sources, tlr, inventory)
        self.assertEqual(report["eligible_ids"], ["R2"])
        self.assertEqual(report["requirements"][0]["disposition"], "revise")
        self.assertIn("COMPARISON_MISMATCH", [f["code"] for f in report["structural_coverage"]["findings"]])

    def test_honest_component_abstention_is_not_reclassified_as_recoverable(self):
        sources, tlr, inventory = fixture()
        raw = response(sources, tlr, inventory)
        candidate = tlr["requirements"][0]
        candidate.pop("formula")
        candidate.pop("abstraction")
        candidate.update(status="unsupported", reason="A contextual obligation exceeds this profile.", reason_code="profile_limit")
        candidate["coverage"] = [{"obligation_id": c["obligation_id"], "status": "unsupported",
                                  "reason": "Whole source record retained without partial executable projection."}
                                 for c in candidate["coverage"]]
        parent = raw["requirements"][0]
        parent.update(disposition="unsupported", ast_paths=["/requirements/0"])
        for child in parent["obligations"]:
            child.update(disposition="unsupported", ast_paths=["/requirements/0"])
        report = validate_review(raw, sources, tlr, inventory)
        self.assertEqual(report["eligible_ids"], ["R2"])
        self.assertEqual(report["requirements"][0]["disposition"], "unsupported")
        self.assertTrue(source_review_available(report))
        self.assertFalse(review_has_repair_findings(report))

    def test_duplicate_unknown_or_invalid_component_ids_withhold_only_their_requirement(self):
        sources, tlr, inventory = fixture()
        for kind in ("duplicate", "unknown", "invalid"):
            with self.subTest(kind=kind):
                raw = response(sources, tlr, inventory)
                children = raw["requirements"][0]["obligations"]
                if kind == "duplicate":
                    children.append(deepcopy(children[0]))
                else:
                    children[0]["id"] = "INVENTED" if kind == "unknown" else []
                report = validate_review(raw, sources, tlr, inventory)
                self.assertEqual(report["eligible_ids"], ["R2"])
                self.assertFalse(source_review_available(report))

    def test_component_coverage_cannot_be_declared_not_applicable(self):
        sources, tlr, inventory = fixture()
        raw = response(sources, tlr, inventory)
        raw["requirements"][0]["obligations"][0]["dimensions"][0]["status"] = "not_applicable"
        report = validate_review(raw, sources, tlr, inventory)
        self.assertEqual(report["eligible_ids"], ["R2"])
        self.assertEqual(report["requirements"][0]["obligations"][0]["disposition"], "unreviewed")

    def test_question_metadata_is_not_source_evidence_under_inventory_gate(self):
        sources, tlr, inventory = fixture()
        sources[0]["source"]["questions"] = [{"id": "Q1", "question": "Is voltage nominal?", "text": "Assume 24 V."}]
        raw = response(sources, tlr, inventory)
        raw["requirements"][0]["source_basis"].append({"source_id": "R1", "quote": "Assume 24 V."})
        report = validate_review(raw, sources, tlr, inventory)
        self.assertEqual(report["eligible_ids"], ["R2"])
        self.assertIn("not a literal", report["requirements"][0]["reason"])

    def test_inventory_and_coverage_are_recorded_and_bound_for_exact_reuse(self):
        sources, tlr, inventory = fixture()
        def callback(system, prompt, *_):
            payload = json.loads(prompt)
            self.assertIn("EVERY requirement review MUST include obligations", system)
            self.assertEqual(payload["structural_coverage"]["status"], "passed")
            self.assertNotIn("solver_feedback", payload)
            return json.dumps(response(payload["source_packet"], payload["candidate_tlr"], payload["obligation_inventory"]))
        reviewer = Mock(side_effect=callback)
        with tempfile.TemporaryDirectory() as tmp:
            first = review_candidate(sources, tlr, None, tmp, "offline-review", reviewer, obligation_inventory=inventory)
            reused = review_candidate(sources, tlr, None, tmp, "offline-review", reviewer, previous=first, obligation_inventory=inventory)
            self.assertTrue(reused["reused"])
            self.assertEqual(reused["call_count"], 0)
            changed = deepcopy(inventory)
            changed["requirements"][0]["obligations"][0]["selection_reason"] += " Reviewed explicit endpoint."
            revised = review_candidate(sources, tlr, None, tmp, "offline-review", reviewer, previous=first, obligation_inventory=changed)
            self.assertFalse(revised["reused"])
            self.assertEqual(reviewer.call_count, 2)
            self.assertEqual(revised["binding"]["obligation_inventory"], changed)
            saved = json.loads((Path(tmp) / "source_review/report.json").read_text())
            self.assertEqual(saved, revised)

    def test_legacy_review_without_inventory_remains_usable(self):
        sources, tlr = scalar_fixture()
        self.assertTrue(validate_review(response_for(sources, tlr), sources, tlr)["complete"])


if __name__ == "__main__":
    unittest.main()
