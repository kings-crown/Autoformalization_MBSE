"""Source-obligation coverage gate: positive evidence and frozen slot preservation."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from canonical_coverage import normalize_coverage, validate_coverage
from canonical_tlr import render_sysml, validate_tlr


def fixture():
    inventory = {"schema": "mbse_obligation_inventory/1", "requirements": [{
        "id": "R1", "obligations": [{"id": "R1.O1", "kind": "state_constraint",
        "slots": {"subject": "battery", "scope": "one observation", "quantity": "voltage",
                  "unit": "mV", "bound": "28000", "operator": "<="}}]}]}
    row = {"id": "R1", "status": "supported", "formula": {"op": "<=", "args": [
        {"var": "voltage"}, {"value": "28", "unit": "V"}]}, "coverage": [{
        "obligation_id": "R1.O1", "status": "represented", "formula_path": "/formula",
        "slots": {"quantity": ["/formula/args/0"], "operator": ["/formula"],
                  "bound": ["/formula/args/1"], "unit": ["/formula/args/1"]}}]}
    tlr = {"schema": "mbse_tlr/1", "variables": [{"name": "voltage", "type": "Real", "unit": "V"}],
           "requirements": [row]}
    return inventory, tlr


def codes(report):
    return {f["code"] for f in report["findings"]}


class CoverageTests(unittest.TestCase):
    def test_numeric_preservation_normalizes_exact_units(self):
        inventory, tlr = fixture()
        original = deepcopy(tlr)
        report = validate_coverage(tlr, inventory)
        self.assertEqual(report["eligible_ids"], ["R1"])
        self.assertEqual(report["findings"], [])
        self.assertEqual(tlr, original)
        self.assertEqual(validate_tlr(tlr)["requirements"][0]["coverage"], tlr["requirements"][0]["coverage"])

    def test_endpoint_change_and_weakened_bound_are_not_coverage(self):
        inventory, tlr = fixture()
        for formula, expected in [
            ({"op": "<", "args": [{"var": "voltage"}, {"value": "28", "unit": "V"}]}, "COMPARISON_MISMATCH"),
            ({"op": "<=", "args": [{"var": "voltage"}, {"value": "30", "unit": "V"}]}, "BOUND_MISMATCH"),
        ]:
            changed = deepcopy(tlr)
            changed["requirements"][0]["formula"] = formula
            report = validate_coverage(changed, inventory)
            self.assertEqual(report["eligible_ids"], [])
            self.assertIn(expected, codes(report))

    def test_missing_coverage_and_missing_components_are_repair_findings(self):
        inventory, tlr = fixture()
        del tlr["requirements"][0]["coverage"]
        self.assertIn("MISSING_COMPONENT", codes(validate_coverage(tlr, inventory)))
        self.assertNotIn("coverage", validate_tlr(tlr)["requirements"][0])

    def test_invalid_or_duplicate_component_ids_fail_closed(self):
        inventory, tlr = fixture()
        tlr["requirements"][0]["coverage"].append(deepcopy(tlr["requirements"][0]["coverage"][0]))
        self.assertIn("DUPLICATE_COMPONENT", codes(validate_coverage(tlr, inventory)))
        tlr["requirements"][0]["coverage"][1]["obligation_id"] = "R1.O2"
        self.assertIn("UNKNOWN_COMPONENT", codes(validate_coverage(tlr, inventory)))

    def test_disjunction_and_implication_fragments_cannot_claim_assertion(self):
        inventory, tlr = fixture()
        for op in ("or", "implies"):
            changed = deepcopy(tlr)
            row = changed["requirements"][0]
            row["formula"] = {"op": op, "args": [True, row["formula"]]}
            coverage = row["coverage"][0]
            coverage["formula_path"] = "/formula/args/1"
            coverage["slots"] = {k: [p.replace("/formula", "/formula/args/1") for p in pointers]
                                 for k, pointers in coverage["slots"].items()}
            report = validate_coverage(changed, inventory)
            self.assertIn("NONASSERTED_COMPONENT_PATH", codes(report))

    def test_conjunction_accepts_each_assigned_factor_and_rejects_invented_extra(self):
        inventory, tlr = fixture()
        row = tlr["requirements"][0]
        row["formula"] = {"op": "and", "args": [row["formula"], {"op": ">=", "args": [
            {"var": "voltage"}, {"value": "1", "unit": "V"}]}]}
        coverage = row["coverage"][0]
        coverage["formula_path"] = "/formula/args/0"
        coverage["slots"] = {k: [p.replace("/formula", "/formula/args/0") for p in pointers]
                             for k, pointers in coverage["slots"].items()}
        self.assertIn("UNASSIGNED_CONSTRAINT", codes(validate_coverage(tlr, inventory)))
        obligation = deepcopy(inventory["requirements"][0]["obligations"][0])
        obligation["id"] = "R1.O2"
        obligation["slots"].update(operator=">=", bound="1000")
        inventory["requirements"][0]["obligations"].append(obligation)
        row["coverage"].append({"obligation_id": "R1.O2", "status": "represented", "formula_path": "/formula/args/1",
            "slots": {k: [p.replace("/formula/args/0", "/formula/args/1") for p in pointers]
                      for k, pointers in coverage["slots"].items()}})
        self.assertEqual(validate_coverage(tlr, inventory)["eligible_ids"], ["R1"])

    def test_declared_guard_must_exist_and_unplanned_guard_is_refused(self):
        inventory, tlr = fixture()
        tlr["variables"].append({"name": "charging", "type": "Bool"})
        inventory["requirements"][0]["obligations"][0]["slots"]["condition"] = "while charging"
        self.assertIn("MISSING_GUARD", codes(validate_coverage(tlr, inventory)))
        row = tlr["requirements"][0]
        row["formula"] = {"op": "implies", "args": [{"var": "charging"}, row["formula"]]}
        coverage = row["coverage"][0]
        coverage["slots"] = {k: [p.replace("/formula", "/formula/args/1") for p in pointers]
                             for k, pointers in coverage["slots"].items()}
        coverage["slots"]["condition"] = ["/formula/args/0"]
        self.assertEqual(validate_coverage(tlr, inventory)["eligible_ids"], ["R1"])
        del inventory["requirements"][0]["obligations"][0]["slots"]["condition"]
        self.assertIn("UNPLANNED_GUARD", codes(validate_coverage(tlr, inventory)))

    def test_constant_guard_does_not_bind_source_condition(self):
        inventory, tlr = fixture()
        inventory["requirements"][0]["obligations"][0]["slots"]["condition"] = "while charging"
        row = tlr["requirements"][0]
        row["formula"] = {"op": "implies", "args": [False, row["formula"]]}
        self.assertIn("CONSTANT_GUARD", codes(validate_coverage(tlr, inventory)))

    def test_abstention_cannot_claim_execution_or_disappear(self):
        inventory, tlr = fixture()
        row = tlr["requirements"][0]
        row.update(status="unsupported", reason="Not represented.")
        del row["formula"]
        self.assertIn("REQUIREMENT_NOT_EXECUTABLE", codes(validate_coverage(tlr, inventory)))
        row["coverage"] = [{"obligation_id": "R1.O1", "status": "unsupported", "reason": "No scalar binding supplied."}]
        report = validate_coverage(tlr, inventory)
        self.assertIn("COMPONENT_NOT_REPRESENTED", codes(report))
        self.assertEqual(report["eligible_ids"], [])

    def test_frozen_temporal_plan_cannot_be_overridden_with_static_flag(self):
        inventory, tlr = fixture()
        inventory["requirements"][0]["obligations"][0]["kind"] = "unsupported"
        self.assertIn("FROZEN_PLAN_UNSUPPORTED", codes(validate_coverage(tlr, inventory)))

    def test_capability_and_behavior_remain_distinct(self):
        inventory, tlr = fixture()
        tlr["variables"] = [{"name": "selection_available", "type": "Bool"}]
        row = tlr["requirements"][0]
        row["formula"] = {"var": "selection_available"}
        row["coverage"][0]["slots"] = {"operation": ["/formula"]}
        obligation = inventory["requirements"][0]["obligations"][0]
        obligation.update(kind="capability", slots={"subject": "user", "scope": "search UI", "operation": "select torrent search"})
        self.assertEqual(validate_coverage(tlr, inventory)["eligible_ids"], ["R1"])
        obligation.update(kind="event_relation", slots={"subject": "system", "scope": "search occurrence",
            "condition": "selected and search started", "participants": ["requested database", "queried database"]})
        self.assertIn("MISSING_GUARD", codes(validate_coverage(tlr, inventory)))
        self.assertEqual(validate_coverage(tlr, inventory)["eligible_ids"], [])

    def test_occurrence_relation_binds_distinct_consequence_participants(self):
        inventory, tlr = fixture()
        tlr["variables"] = [{"name": "received", "type": "Bool"}, {"name": "actual", "type": "Int"}, {"name": "specified", "type": "Int"}]
        row = tlr["requirements"][0]
        row["formula"] = {"op": "implies", "args": [{"var": "received"}, {"op": "=", "args": [{"var": "actual"}, {"var": "specified"}]}]}
        row["coverage"][0]["slots"] = {"condition": ["/formula/args/0"], "participants": ["/formula/args/1/args/0", "/formula/args/1/args/1"]}
        obligation = inventory["requirements"][0]["obligations"][0]
        obligation.update(kind="event_relation", slots={"subject": "message", "scope": "one receipt",
            "condition": "message received", "participants": ["actual recipient", "specified recipient"]})
        self.assertEqual(validate_coverage(tlr, inventory)["eligible_ids"], ["R1"])
        row["coverage"][0]["slots"]["participants"][1] = "/formula/args/0"
        self.assertIn("PARTICIPANT_BINDING", codes(validate_coverage(tlr, inventory)))

    def test_wrong_slot_paths_fail_even_if_metadata_mentions_correct_words(self):
        inventory, tlr = fixture()
        for bad in ("/formula/args/1", "/formula/args/0/var", "/formula/args/04"):
            changed = deepcopy(tlr)
            changed["requirements"][0]["coverage"][0]["slots"]["quantity"] = [bad]
            self.assertEqual(validate_coverage(changed, inventory)["eligible_ids"], [])

    def test_sysml_retains_component_ids_and_bindings_without_acceptance_claim(self):
        _, tlr = fixture()
        text = render_sysml(tlr, eligible_ids=[])
        self.assertIn("Source obligation: R1.O1", text)
        self.assertIn("candidate coverage disposition: represented", text)
        self.assertIn("/formula/args/0", text)
        self.assertNotIn("require constraint obligation", text)
        self.assertIn("Executable constraint withheld", text)

    def test_metadata_shape_rejects_arbitrary_evidence_and_invalid_status(self):
        for raw in ({}, [{"obligation_id": "X", "status": "pass"}],
                    [{"obligation_id": "X", "status": "represented", "slots": {"quantity": ["documentation"]}}]):
            with self.assertRaises(ValueError):
                normalize_coverage(raw)

    def test_generic_state_cannot_hide_an_unplanned_guard_inside_conjunction(self):
        inventory, tlr = fixture()
        inventory["requirements"][0]["obligations"][0]["slots"] = {"subject": "battery", "scope": "observation"}
        tlr["variables"].append({"name": "enabled", "type": "Bool"})
        row = tlr["requirements"][0]
        row["formula"] = {"op": "and", "args": [{"var": "enabled"}, {"op": "implies", "args": [{"var": "enabled"}, row["formula"]]}]}
        row["coverage"][0]["slots"] = {}
        self.assertIn("UNPLANNED_NESTED_GUARD", codes(validate_coverage(tlr, inventory)))

    def test_partial_numeric_details_on_frozen_unsupported_cannot_crash_gate(self):
        inventory, tlr = fixture()
        obligation = inventory["requirements"][0]["obligations"][0]
        obligation["kind"] = "unresolved"
        del obligation["slots"]["bound"]
        del obligation["slots"]["operator"]
        self.assertIn("FROZEN_PLAN_UNSUPPORTED", codes(validate_coverage(tlr, inventory)))


if __name__ == "__main__":
    unittest.main()
