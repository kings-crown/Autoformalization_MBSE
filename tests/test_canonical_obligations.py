"""Source-only inventories cannot silently erase guards or context authority."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from canonical_obligations import (SCHEMA, REVIEW_SCHEMA, REVIEW_DIMENSIONS,
                                  prepare_inventory, source_registry,
                                  validate_inventory, validate_inventory_review)


def fixture():
    sources = [{"id": "R1", "text": "The user can select torrent searching.", "source": {"context": [
        {"id": "CTX1", "quote": "When selected and a search starts, query the torrent-site database."},
        {"id": "Q1", "question": "Should queries complete immediately?", "text": "An analyst question."}]}},
        {"id": "R2", "text": "Query duration shall be at most 5 seconds."}]
    inventory = {"schema": SCHEMA, "requirements": [
        {"id": "R1", "context": [{"source_id": "CTX1", "role": "additional_obligation", "reason": "Defines behavior of selected searching."}],
         "obligations": [
             {"id": "R1.O1", "meaning": sources[0]["text"], "source_basis": [{"source_id": "R1", "quote": sources[0]["text"]}],
              "kind": "capability", "slots": {"subject": "User", "scope": "Search interface", "operation": "Select torrent searching"},
              "selection_reason": "Requires selection availability.", "limitations": ["Does not establish execution."]},
             {"id": "R1.O2", "meaning": "A selected search invokes the torrent-site query.",
              "source_basis": [{"source_id": "CTX1", "quote": "When selected and a search starts, query the torrent-site database."}],
              "kind": "unsupported", "slots": {"subject": "Search engine", "scope": "One search", "condition": "Selected and search starts"},
              "selection_reason": "An execution obligation cannot be discharged by static availability.",
              "limitations": ["Static profile does not establish execution or response ordering."]}]},
        {"id": "R2", "context": [], "obligations": [
            {"id": "R2.O1", "meaning": sources[1]["text"], "source_basis": [{"source_id": "R2", "quote": sources[1]["text"]}],
             "kind": "state_constraint", "slots": {"subject": "Search engine", "scope": "One query", "quantity": "Query duration", "unit": "s", "operator": "<=", "bound": "5"},
             "selection_reason": "A named duration has a precise inclusive upper bound.", "limitations": []}]}]}
    return sources, inventory


def accepted_review(sources, inventory):
    return {"schema": REVIEW_SCHEMA, "requirements": [
        {"id": row["id"], "disposition": "pass", "reason": "Retains contextual meanings and limits.",
         "source_basis": [{"source_id": source["id"], "quote": source["text"]}],
         "dimensions": [{"dimension": name, "status": "pass", "explanation": "Source and inventory correspond at this dimension."} for name in REVIEW_DIMENSIONS],
         "obligations": [{"id": component["id"], "disposition": "pass", "reason": "Meaning and boundary explicit."} for component in row["obligations"]]}
        for source, row in zip(sources, inventory["requirements"])]}


class ObligationInventoryTests(unittest.TestCase):
    def test_preserves_compound_obligation_and_unsupported_temporal_residual(self):
        sources, inventory = fixture()
        result = validate_inventory(inventory, sources)
        self.assertEqual(result, inventory)
        self.assertIsNot(result, inventory)
        self.assertEqual(result["requirements"][0]["obligations"][1]["kind"], "unsupported")

    def test_context_registry_does_not_promote_questions_to_authoritative_excerpts(self):
        sources, inventory = fixture()
        registry = source_registry(sources)
        self.assertEqual(set(registry), {"R1", "R2", "CTX1"})
        self.assertEqual(registry["CTX1"]["kind"], "context")
        inventory["requirements"][0]["context"].append({"source_id": "Q1", "role": "assumption", "reason": "Question treated as an answer."})
        with self.assertRaisesRegex(ValueError, "known source"):
            validate_inventory(inventory, sources)

    def test_literal_context_evidence_cannot_be_attributed_to_requirement_or_question(self):
        sources, inventory = fixture()
        for sid, quote in [("R1", "query the torrent-site database"), ("CTX1", "An analyst question.")]:
            changed = deepcopy(inventory)
            changed["requirements"][0]["obligations"][1]["source_basis"] = [{"source_id": sid, "quote": quote}]
            with self.assertRaisesRegex(ValueError, "literal quotation"):
                validate_inventory(changed, sources)

    def test_identical_context_repetition_allowed_conflicts_and_id_collisions_rejected(self):
        sources, _ = fixture()
        sources[1]["source"] = {"context": [deepcopy(sources[0]["source"]["context"][0])]}
        self.assertEqual(len(source_registry(sources)), 3)
        for entry in ({"id": "CTX1", "quote": "A conflicting excerpt."}, {"id": "R1", "quote": sources[0]["text"]}):
            sources[1]["source"]["context"] = [entry]
            with self.assertRaisesRegex(ValueError, "Conflicting or colliding"):
                source_registry(sources)

    def test_contextual_obligation_requires_explicit_role_and_actual_component(self):
        sources, inventory = fixture()
        missing_role = deepcopy(inventory)
        missing_role["requirements"][0]["context"] = []
        with self.assertRaisesRegex(ValueError, "explicit context role"):
            validate_inventory(missing_role, sources)
        missing_component = deepcopy(inventory)
        missing_component["requirements"][0]["obligations"].pop()
        with self.assertRaisesRegex(ValueError, "Missing required local source_basis citations"):
            validate_inventory(missing_component, sources)

    def test_capability_cannot_claim_guard_or_numeric_constraint(self):
        sources, inventory = fixture()
        for slot, value in [("condition", "When selected"), ("participants", ["A", "B"])]:
            changed = deepcopy(inventory)
            changed["requirements"][0]["obligations"][0]["slots"][slot] = value
            with self.assertRaisesRegex(ValueError, "cannot conceal"):
                validate_inventory(changed, sources)
        missing = deepcopy(inventory)
        del missing["requirements"][0]["obligations"][0]["slots"]["operation"]
        with self.assertRaisesRegex(ValueError, "Capability needs operation"):
            validate_inventory(missing, sources)

    def test_numeric_endpoint_slots_preserve_exact_quantity_unit_and_operator(self):
        sources, inventory = fixture()
        for key in ("quantity", "unit", "operator", "bound"):
            changed = deepcopy(inventory)
            del changed["requirements"][1]["obligations"][0]["slots"][key]
            with self.assertRaisesRegex(ValueError, "together"):
                validate_inventory(changed, sources)
        for bound in (5, "NaN", "1/3", "1e3"):
            changed = deepcopy(inventory)
            changed["requirements"][1]["obligations"][0]["slots"]["bound"] = bound
            with self.assertRaises(ValueError):
                validate_inventory(changed, sources)

    def test_known_numeric_slots_survive_honest_source_ambiguity(self):
        sources, inventory = fixture()
        component = inventory["requirements"][1]["obligations"][0]
        component.update(kind="unresolved", limitations=["The source leaves the precise duration meaning ambiguous."])
        del component["slots"]["bound"]
        self.assertEqual(validate_inventory(inventory, sources)["requirements"][1]["obligations"][0]["slots"]["unit"], "s")

    def test_stable_component_ids_and_source_order_are_enforced(self):
        sources, inventory = fixture()
        inventory["requirements"][0]["obligations"][1]["id"] = "R1.O1"
        with self.assertRaisesRegex(ValueError, "stable sequential"):
            validate_inventory(inventory, sources)
        sources, inventory = fixture()
        inventory["requirements"].reverse()
        with self.assertRaisesRegex(ValueError, "IDs/order"):
            validate_inventory(inventory, sources)

    def test_inventory_review_cannot_bypass_missing_component_or_negative_dimension(self):
        sources, inventory = fixture()
        for mutate in (lambda r: r["requirements"][0]["obligations"].pop(),
                       lambda r: r["requirements"][0]["dimensions"][0].update(status="fail"),
                       lambda r: r["requirements"][0]["obligations"][0].update(disposition="revise")):
            raw = accepted_review(sources, inventory)
            mutate(raw)
            report = validate_inventory_review(raw, sources, inventory)
            self.assertEqual(report["status"], "withheld")
            self.assertEqual(report["requirements"][1]["disposition"], "pass")
            self.assertFalse(report["complete"])

    def test_source_only_generation_and_separate_review_record_input_usage_and_boundary(self):
        sources, inventory = fixture()
        sources[0]["judge_verdict"] = "DO NOT LEAK THIS"
        generator = Mock(return_value=json.dumps(inventory))
        def review(system, prompt, model, directory, call_id):
            (directory/f"{call_id}.json").write_text(json.dumps({"input_tokens": 123, "output_tokens": 45, "estimated_cost": 0.02}))
            return accepted_review(sources, inventory)
        reviewer = Mock(side_effect=review)
        with tempfile.TemporaryDirectory() as tmp:
            report = prepare_inventory(sources, tmp, "test-model", generator, reviewer)
            self.assertEqual(report["status"], "passed")
            self.assertEqual((report["generation_call_count"], report["review_call_count"]), (1, 1))
            self.assertNotIn("DO NOT LEAK THIS", generator.call_args.args[1])
            self.assertNotIn("candidate_tlr", reviewer.call_args.args[1])
            self.assertNotIn("solver", json.loads(reviewer.call_args.args[1]))
            target = Path(tmp)/"obligation_inventory"
            self.assertEqual(json.loads((target/"report.json").read_text()), report)
            self.assertEqual(json.loads((target/"review_call.json").read_text())["estimated_cost"], 0.02)
            self.assertTrue((target/"generation_response.txt").is_file())

    def test_supplied_inventory_skips_generation_not_review_and_exact_reuse_checks_response(self):
        sources, inventory = fixture()
        generator = Mock(side_effect=AssertionError("Generation must not run"))
        reviewer = Mock(return_value=accepted_review(sources, inventory))
        with tempfile.TemporaryDirectory() as tmp:
            first = prepare_inventory(sources, Path(tmp)/"first", "test-model", generator, reviewer, supplied=inventory)
            second = prepare_inventory(sources, Path(tmp)/"second", "test-model", generator, reviewer, supplied=inventory, previous=first)
            self.assertEqual(first["call_count"], 1)
            self.assertEqual(second["call_count"], 0)
            self.assertTrue(second["reused"])
            self.assertEqual(reviewer.call_count, 1)
            # Stored status is never trusted without revalidating response.
            first["response"]["requirements"][0]["obligations"].pop()
            third = prepare_inventory(sources, Path(tmp)/"third", "test-model", generator, reviewer, supplied=inventory, previous=first)
            self.assertFalse(third["complete"])

    def test_reuse_binding_includes_effective_instructions_configuration_source_and_inventory(self):
        sources, inventory = fixture()
        reviewer = Mock(return_value=accepted_review(sources, inventory))
        with tempfile.TemporaryDirectory() as tmp:
            first = prepare_inventory(sources, Path(tmp)/"first", "m", None, reviewer, supplied=inventory)
            with patch("canonical_obligations.REVIEW_INSTRUCTIONS", "Changed review policy"):
                changed = prepare_inventory(sources, Path(tmp)/"changed", "m", None, reviewer, supplied=inventory, previous=first)
            self.assertFalse(changed["reused"])
            self.assertEqual(reviewer.call_count, 2)

    def test_failure_records_history_without_candidate_or_implicit_retry(self):
        sources, inventory = fixture()
        for callback in (Mock(side_effect=RuntimeError("transport unavailable")), Mock(return_value="not JSON"), Mock(return_value={"schema": SCHEMA, "requirements": []})):
            reviewer = Mock(side_effect=AssertionError("Review must not run without valid inventory"))
            with tempfile.TemporaryDirectory() as tmp:
                report = prepare_inventory(sources, tmp, "m", callback, reviewer)
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["call_count"], 1)
                self.assertIsNone(report["inventory"])
                self.assertIsNotNone(report["failure"])
                self.assertEqual(json.loads((Path(tmp)/"obligation_inventory/report.json").read_text()), report)
                reviewer.assert_not_called()

    def test_malformed_review_and_valid_rejection_both_withhold_preparation(self):
        sources, inventory = fixture()
        rejected = accepted_review(sources, inventory)
        rejected["requirements"][0].update(disposition="revise", reason="Context is incomplete.")
        for response in ("not JSON", rejected):
            with tempfile.TemporaryDirectory() as tmp:
                report = prepare_inventory(sources, tmp, "m", None, Mock(return_value=response), supplied=inventory)
                self.assertFalse(report["complete"])
                self.assertEqual(report["status"], "withheld")
                self.assertEqual(report["inventory"], inventory)


if __name__ == "__main__":
    unittest.main()
