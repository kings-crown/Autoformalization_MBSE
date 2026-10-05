"""Source-linked UNSAT cores preserve verdicts, coverage limits and scenario scope."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from canonical_audits import audit_tlr
import mutation_core


def op(name, *args):
    return {"op": name, "args": list(args)}


def quantity(value, unit="V"):
    return {"value": str(value), "unit": unit}


def requirement(rid, formula):
    return {"id": rid, "text": "Source requirement " + rid,
            "status": "supported", "formula": formula}


def fixture():
    return {"schema": "mbse_tlr/1", "variables": [
        {"name": "voltage", "type": "Real", "unit": "V"},
        {"name": "mass", "type": "Real", "unit": "kg"}],
        "assumptions": [], "requirements": [
            requirement("MIN", op(">=", {"var": "voltage"}, quantity(30))),
            requirement("MAX", op("<=", {"var": "voltage"}, quantity(28))),
            requirement("MASS", op("<=", {"var": "mass"}, quantity(2, "kg")))]}


@unittest.skipUnless(shutil.which("z3"), "A local Z3 executable is required.")
class ConflictLocalizationTests(unittest.TestCase):
    def audit(self, tlr, **kwargs):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return audit_tlr(tlr, temp.name, **kwargs), Path(temp.name)

    def test_core_localizes_conflicting_pair_and_preserves_exact_evidence(self):
        result, directory = self.audit(fixture())
        core = result["consistency"]["unsat_core"]
        self.assertEqual(core["status"], "available")
        self.assertEqual(core["solver_status"], "unsat")
        self.assertEqual(core["requirement_ids"], ["MIN", "MAX"])
        self.assertEqual(core["minimality"], "not_minimized")
        self.assertIn("not necessarily minimal", core["interpretation"])
        self.assertIn("No member", core["interpretation"])
        self.assertEqual(core["assumption_ids"], [])
        finding = next(row for row in result["findings"] if row["code"] == "inconsistent_requirements")
        self.assertEqual(finding["requirement_ids"], ["MIN", "MAX"])
        self.assertEqual(finding["checked_requirement_ids"], ["MIN", "MAX", "MASS"])
        self.assertIn("(get-unsat-core)", (directory / core["artifacts"]["query"]).read_text())
        self.assertEqual(json.loads((directory / core["artifacts"]["result"]).read_text()), core)
        self.assertEqual(json.loads((directory / "consistency.json").read_text())["unsat_core"], core)

    def test_named_domains_and_background_assumptions_keep_normalized_units(self):
        tlr = fixture()
        tlr["variables"][0].update(unit="mV", bounds={"lower": "1000"})
        tlr["assumptions"] = [{"id": "ENV_low", "text": "A claimed environmental upper limit.",
                               "predicate": op("<", {"var": "voltage"}, quantity(500, "mV"))}]
        result, directory = self.audit(tlr)
        core = result["background"]["unsat_core"]
        self.assertEqual(result["background_status"], "unsat")
        self.assertEqual(result["consistency_status"], "blocked")
        self.assertEqual(core["requirement_ids"], [])
        self.assertEqual(core["assumption_ids"], ["ENV_low"])
        self.assertEqual(len(core["domain_bounds"]), 1)
        bound = core["domain_bounds"][0]
        self.assertEqual((bound["variable"], bound["unit"], bound["value"]), ("voltage", "V", "1"))
        query = (directory / core["artifacts"]["query"]).read_text()
        self.assertIn("(>= v_voltage_0 1)", query)
        self.assertNotIn(":named requirement_", query)

    def test_requirement_can_conflict_with_domain_without_a_second_requirement(self):
        tlr = fixture()
        tlr["variables"][0]["bounds"] = {"upper": "28"}
        tlr["requirements"] = [tlr["requirements"][0]]
        result, _ = self.audit(tlr)
        self.assertEqual(result["background_status"], "sat")
        core = result["consistency"]["unsat_core"]
        self.assertEqual(core["requirement_ids"], ["MIN"])
        self.assertEqual(core["domain_bounds"][0]["bound"], "upper")
        self.assertEqual(core["domain_bounds"][0]["value"], "28")

    def test_withheld_conflicting_source_is_not_asserted_or_diagnosed(self):
        result, directory = self.audit(fixture(), eligible_ids=["MAX", "MASS"])
        self.assertEqual(result["consistency_status"], "sat")
        self.assertEqual(result["withheld_requirement_ids"], ["MIN"])
        self.assertIn("Partial source-reviewed subset", result["scope"])
        self.assertFalse(result["admitted"])
        self.assertNotIn("unsat_core", result["consistency"])
        self.assertFalse((directory / "consistency_core.smt2").exists())

    def test_partial_source_coverage_can_still_localize_an_actual_encoded_conflict(self):
        result, _ = self.audit(fixture(), eligible_ids=["MIN", "MAX"])
        self.assertEqual(result["consistency_status"], "unsat")
        self.assertFalse(result["admitted"])
        self.assertEqual(result["withheld_requirement_ids"], ["MASS"])
        self.assertEqual(result["consistency"]["unsat_core"]["requirement_ids"], ["MIN", "MAX"])

    def test_opposing_guarded_rules_are_scenario_conflicts_not_global_unsat(self):
        tlr = {"schema": "mbse_tlr/1", "variables": [
            {"name": "maintenance", "type": "Bool"}, {"name": "motion", "type": "Bool"}],
            "assumptions": [], "requirements": [
                requirement("MOVE", op("implies", {"var": "maintenance"}, {"var": "motion"})),
                requirement("STOP", op("implies", {"var": "maintenance"}, op("not", {"var": "motion"})))]}
        result, _ = self.audit(tlr)
        self.assertEqual(result["consistency_status"], "sat")
        self.assertNotIn("inconsistent_requirements", [row["code"] for row in result["findings"]])
        check = result["requirements"][0]["checks"]["in_model_trigger"]
        self.assertEqual(check["status"], "unsat")
        self.assertEqual(check["unsat_core"]["requirement_ids"], ["MOVE", "STOP"])
        self.assertEqual(check["unsat_core"]["scenario_assumptions"][0]["expression"], "v_maintenance_0")
        self.assertEqual(check["unsat_core"]["assumption_ids"], [])
        self.assertIn("scenario-specific", next(row for row in result["findings"]
                                                if row["code"] == "trigger_excluded_by_specification")["explanation"])

    def test_localization_failure_does_not_erase_unsat_or_fabricate_a_core(self):
        execute = mutation_core._execute

        def failing_core(query, *args):
            if "(get-unsat-core)" in query:
                return {"status": "timeout", "stdout": "", "stderr": "", "diagnostic": "Core timed out."}
            return execute(query, *args)

        with patch("canonical_audits.solver_core._execute", side_effect=failing_core):
            result, directory = self.audit(fixture())
        self.assertEqual(result["consistency_status"], "unsat")
        core = result["consistency"]["unsat_core"]
        self.assertEqual(core["status"], "unavailable")
        self.assertEqual(core["solver_status"], "timeout")
        self.assertEqual(core["requirement_ids"], [])
        finding = next(row for row in result["findings"] if row["code"] == "inconsistent_requirements")
        self.assertEqual(finding["requirement_ids"], ["MIN", "MAX", "MASS"])
        self.assertIn("not a localized conflict", finding["explanation"])
        self.assertTrue((directory / core["artifacts"]["result"]).exists())

    def test_unknown_assertion_in_solver_core_is_parse_failure(self):
        execute = mutation_core._execute

        def malformed_core(query, *args):
            if "(get-unsat-core)" in query:
                return {"status": "unsat", "stdout": "unsat\n(invented_source)\n", "stderr": ""}
            return execute(query, *args)

        with patch("canonical_audits.solver_core._execute", side_effect=malformed_core):
            result, _ = self.audit(fixture())
        core = result["consistency"]["unsat_core"]
        self.assertEqual(core["status"], "parse_error")
        self.assertEqual(core["requirement_ids"], [])
        self.assertEqual(result["consistency_status"], "unsat")

    def test_source_identifiers_are_data_not_smt_assertion_syntax(self):
        tlr = fixture()
        tlr["requirements"][0]["id"] = "MIN odd | source (id)"
        result, directory = self.audit(tlr)
        core = result["consistency"]["unsat_core"]
        self.assertEqual(core["requirement_ids"][0], "MIN odd | source (id)")
        self.assertNotIn("MIN odd", (directory / core["artifacts"]["query"]).read_text())


if __name__ == "__main__":
    unittest.main()
