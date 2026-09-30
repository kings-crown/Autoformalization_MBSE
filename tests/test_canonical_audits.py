"""Real-solver checks for canonical TLR audits and honest failure outcomes."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from canonical_audits import audit_tlr


def var(name):
    return {"var": name}


def number(value):
    return {"value": str(value), "unit": "1"}


def op(name, *args):
    return {"op": name, "args": list(args)}


def requirement(rid, formula):
    return {"id": rid, "text": "A source clause for " + rid, "status": "supported", "formula": formula}


def fixture():
    return {"schema": "mbse_tlr/1", "variables": [{"name": "x", "type": "Int", "unit": "1"}],
            "assumptions": [], "requirements": [requirement("REQ-28", op("<=", var("x"), number(28)))]}


@unittest.skipUnless(shutil.which("z3"), "A local Z3 executable is required.")
class CanonicalAuditTests(unittest.TestCase):
    def run_audit(self, tlr):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return audit_tlr(tlr, directory.name), Path(directory.name)

    def test_bound_is_violatable_without_assuming_the_requirement(self):
        result, path = self.run_audit(fixture())
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["admitted"])
        check = result["requirements"][0]["checks"]["violatability"]
        self.assertEqual(check["status"], "sat")
        self.assertGreater(int(check["witness"]["x"]), 28)
        query = (path / check["artifacts"]["query"]).read_text()
        self.assertNotIn("(assert (<= v_x_0 28))", query)
        self.assertIn("(not (<= v_x_0 28))", query)
        saved = json.loads((path / check["artifacts"]["result"]).read_text())
        self.assertEqual(saved["requirement_ids"], ["REQ-28"])

    def test_conflict_blocks_admission_without_selecting_a_wrong_source(self):
        tlr = fixture()
        tlr["requirements"].append(requirement("REQ-30", op(">=", var("x"), number(30))))
        result, _ = self.run_audit(tlr)
        self.assertEqual(result["background_status"], "sat")
        self.assertEqual(result["consistency_status"], "unsat")
        self.assertEqual(result["status"], "findings")
        self.assertFalse(result["admitted"])
        conflict = next(x for x in result["findings"] if x["code"] == "inconsistent_requirements")
        self.assertEqual(conflict["requirement_ids"], ["REQ-28", "REQ-30"])

    def test_unreachable_trigger_and_background_entailment_are_distinguished(self):
        tlr = fixture()
        tlr["variables"].append({"name": "enabled", "type": "Bool"})
        tlr["assumptions"] = [{"id": "A_off", "text": "The mode is disabled.", "predicate": op("not", var("enabled"))}]
        tlr["requirements"][0]["formula"] = op("implies", var("enabled"), op("<=", var("x"), number(28)))
        result, _ = self.run_audit(tlr)
        checks = result["requirements"][0]["checks"]
        self.assertEqual(checks["trigger_reachability"]["status"], "unsat")
        self.assertEqual(checks["violatability"]["status"], "unsat")
        self.assertIn("trigger is unreachable", checks["violatability"]["interpretation"])
        self.assertNotIn("background_entails_requirement", [x["code"] for x in result["findings"]])

    def test_redundancy_requires_feasible_remaining_requirements(self):
        tlr = fixture()
        tlr["requirements"].append(requirement("REQ-20", op("<=", var("x"), number(20))))
        result, _ = self.run_audit(tlr)
        checks = result["requirements"][0]["checks"]
        self.assertEqual(checks["redundancy_context"]["status"], "sat")
        self.assertEqual(checks["redundancy"]["status"], "unsat")
        self.assertTrue(result["admitted"], "Redundancy is a finding, not an automatic admission veto.")
        other = result["requirements"][1]["checks"]
        self.assertEqual(other["redundancy"]["status"], "sat")

    def test_conflicting_neighbors_do_not_make_an_unrelated_requirement_redundant(self):
        tlr = fixture()
        tlr["requirements"].extend([requirement("REQ-neg", op("<", var("x"), number(0))),
                                     requirement("REQ-pos", op(">", var("x"), number(0)))])
        result, _ = self.run_audit(tlr)
        checks = result["requirements"][0]["checks"]
        self.assertEqual(checks["redundancy_context"]["status"], "unsat")
        self.assertEqual(checks["redundancy"]["status"], "blocked")
        self.assertNotIn("REQ-28", [x["requirement_ids"][0] for x in result["findings"] if x["code"] == "redundant_requirement"])

    def test_inconsistent_background_blocks_all_interpretive_audits(self):
        tlr = fixture()
        tlr["assumptions"] = [
            {"id": "A1", "text": "Negative domain.", "predicate": op("<", var("x"), number(0))},
            {"id": "A2", "text": "Positive domain.", "predicate": op(">", var("x"), number(0))}]
        result, _ = self.run_audit(tlr)
        self.assertEqual(result["background_status"], "unsat")
        self.assertEqual(result["consistency_status"], "blocked")
        self.assertFalse(result["admitted"])
        for check in result["requirements"][0]["checks"].values():
            self.assertEqual(check["status"], "blocked")

    def test_in_model_trigger_is_only_tested_after_model_feasibility(self):
        tlr = fixture()
        tlr["variables"].append({"name": "enabled", "type": "Bool"})
        tlr["requirements"][0]["formula"] = op("implies", var("enabled"), op("<=", var("x"), number(28)))
        tlr["requirements"].extend([requirement("REQ-pos", op(">", var("x"), number(0))),
                                     requirement("REQ-neg", op("<", var("x"), number(0)))])
        result, path = self.run_audit(tlr)
        checks = result["requirements"][0]["checks"]
        self.assertEqual(checks["trigger_reachability"]["status"], "sat")
        self.assertEqual(checks["in_model_trigger"]["status"], "blocked")
        self.assertFalse((path / "requirement_0001_in_model_trigger.smt2").exists())

    def test_empty_unsupported_context_does_not_claim_vacuous_success(self):
        tlr = fixture()
        tlr["variables"] = []
        tlr["requirements"] = [{"id": "P1", "text": "Delivery succeeds with probability 0.999.",
                                "status": "unsupported", "reason": "Probabilistic semantics are unsupported."}]
        result, path = self.run_audit(tlr)
        self.assertEqual(result["status"], "unsupported")
        self.assertFalse(result["admitted"])
        self.assertEqual(result["background_status"], "not_run")
        self.assertFalse(list(path.glob("*.smt2")))

    def test_partially_supported_model_is_checked_but_not_admitted(self):
        tlr = fixture()
        tlr["requirements"].append({"id": "P1", "text": "Eventually delivery succeeds.", "status": "unsupported",
                                    "reason": "Unbounded temporal semantics are unsupported."})
        result, _ = self.run_audit(tlr)
        self.assertEqual(result["consistency_status"], "sat")
        self.assertEqual(result["status"], "unsupported")
        self.assertFalse(result["admitted"])
        self.assertEqual(result["unsupported_requirement_ids"], ["P1"])

    def test_source_input_is_unchanged_and_artifacts_are_hash_free(self):
        tlr = fixture()
        before = deepcopy(tlr)
        result, path = self.run_audit(tlr)
        self.assertEqual(tlr, before)
        self.assertNotIn("sha256", json.dumps(result))
        for result_file in path.glob("*.json"):
            self.assertNotIn("sha256", result_file.read_text())

    def test_unavailable_solver_is_not_a_consistency_finding(self):
        with tempfile.TemporaryDirectory() as directory:
            result = audit_tlr(fixture(), directory, solver="missing-z3-for-canonical-test")
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["background_status"], "solver_error")
        self.assertFalse(result["admitted"])
        self.assertEqual(result["findings"], [])

    def test_unknown_and_timeout_remain_separate_outcomes(self):
        for status in ("unknown", "timeout"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                with patch("canonical_audits.solver_core._execute", return_value={"status": status, "stdout": status, "stderr": ""}):
                    result = audit_tlr(fixture(), directory)
                self.assertEqual(result["background_status"], status)
                self.assertEqual(result["status"], "inconclusive")
                self.assertFalse(result["admitted"])

    def test_rejects_overwriting_a_previous_audit(self):
        result, path = self.run_audit(fixture())
        with self.assertRaisesRegex(ValueError, "must be empty"):
            audit_tlr(fixture(), path)

    def test_invalid_input_reports_encoding_error_without_solver_calls(self):
        tlr = fixture()
        tlr["requirements"][0]["formula"] = op("<=", var("missing"), number(28))
        with patch("canonical_audits.solver_core._execute") as execute:
            result, _ = self.run_audit(tlr)
        execute.assert_not_called()
        self.assertEqual(result["background_status"], "encoding_error")
        self.assertFalse(result["admitted"])


if __name__ == "__main__":
    unittest.main()
