"""Executable TLR validation and real SysML compilation of shared expressions."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from canonical_tlr import conjunction, render_sysml, requirement_formula, tlr_context, validate_tlr
from mutation_core import emit_formula, validate_formula
from review_sysml import compile_sysml, compiler_capability


def fixture():
    sources = [
        {"id": "R-1", "text": "When maintenance is active the motion enable signal shall be false.", "source": {"document": "demo.csv", "line": 2}},
        {"id": "R-2", "text": "Battery voltage shall not exceed 28000 mV."},
        {"id": "R-3", "text": "The controller shall eventually recover."},
    ]
    payload = {"schema": "mbse_tlr/1", "variables": [
        {"name": "maintenance", "type": "Bool", "description": "Maintenance mode active."},
        {"name": "motion_enabled", "type": "Bool"},
        {"name": "voltage", "type": "Real", "unit": "mV", "bounds": {"lower": "0"}},
    ], "assumptions": [], "requirements": [
        {"id": "R-1", "status": "supported", "formula": {"op": "implies", "args": [
            {"var": "maintenance"}, {"op": "not", "args": [{"var": "motion_enabled"}]}]}},
        {"id": "R-2", "status": "supported", "formula": {"op": "<=", "args": [
            {"var": "voltage"}, {"value": "28000", "unit": "mV"}]}},
        {"id": "R-3", "status": "unsupported", "reason": "Unbounded eventuality is outside the current-state profile."},
    ]}
    return sources, payload


class CanonicalTlrTests(unittest.TestCase):
    def test_normalizes_units_and_preserves_sources_without_attestations(self):
        sources, payload = fixture()
        original = deepcopy(payload)
        normalized = validate_tlr(payload, sources)
        self.assertEqual(payload, original)
        self.assertEqual(normalized["variables"][2]["unit"], "V")
        self.assertEqual(normalized["requirements"][1]["formula"]["args"][1], {"value": "28", "unit": "V"})
        self.assertEqual(normalized["requirements"][0]["source"], sources[0]["source"])
        self.assertEqual(normalized["requirements"][0]["text"], sources[0]["text"])
        self.assertEqual(validate_tlr(normalized, sources), normalized)
        self.assertNotIn("description", tlr_context(normalized)["variables"][0])
        self.assertIsNone(requirement_formula(normalized["requirements"][2]))

    def test_source_inventory_duplicates_and_rewording_are_errors(self):
        sources, payload = fixture()
        variations = []
        dropped = deepcopy(payload)
        dropped["requirements"].pop()
        variations.append(dropped)
        extra = deepcopy(payload)
        extra["requirements"].append({"id": "R-4", "status": "unsupported", "reason": "Unspecified."})
        variations.append(extra)
        duplicate = deepcopy(payload)
        duplicate["requirements"].append(deepcopy(duplicate["requirements"][0]))
        variations.append(duplicate)
        rewritten = deepcopy(payload)
        rewritten["requirements"][0]["text"] = "Motion shall always be enabled."
        variations.append(rewritten)
        for changed in variations:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_tlr(changed, sources)
        with self.assertRaises(ValueError):
            validate_tlr(payload, sources + [sources[0]])

    def test_preserves_source_order(self):
        sources, payload = fixture()
        payload["requirements"].reverse()
        self.assertEqual([x["id"] for x in validate_tlr(payload, sources)["requirements"]], [x["id"] for x in sources])

    def test_rejects_missing_and_unknown_fields(self):
        _, payload = fixture()
        for extra in ({"hash": "abc"}, {"temporal": []}, {"provenance": {}}, {"schema": "other"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_tlr({**payload, **extra})
        payload["variables"][0]["role"] = "parameter"
        with self.assertRaises(ValueError):
            validate_tlr(payload)

    def test_rejects_boolean_placeholder_and_whole_truth_constants(self):
        _, payload = fixture()
        for formula in [True, False, {"var": "r_1_holds"}, {"op": "=", "args": [{"value": "1"}, {"value": "1"}]}]:
            changed = deepcopy(payload)
            changed["variables"].append({"name": "r_1_holds", "type": "Bool"})
            changed["requirements"][0]["formula"] = formula
            with self.subTest(formula=formula), self.assertRaises(ValueError):
                validate_tlr(changed)

    def test_domain_boolean_names_are_not_rejected_by_suffix_alone(self):
        _, payload = fixture()
        payload["variables"].append({"name": "door_holds", "type": "Bool"})
        payload["requirements"][0]["formula"] = {"var": "door_holds"}
        self.assertEqual(validate_tlr(payload)["requirements"][0]["formula"]["var"], "door_holds")

    def test_rejects_nonlinear_missing_units_unknown_variables_and_next(self):
        _, payload = fixture()
        expressions = [
            {"op": "<=", "args": [{"var": "voltage"}, {"value": "28"}]},
            {"op": "<=", "args": [{"var": "voltage"}, {"value": "28", "unit": "kg"}]},
            {"op": "=", "args": [{"op": "*", "args": [{"var": "voltage"}, {"var": "voltage"}]}, {"value": "28", "unit": "V"}]},
            {"var": "missing"}, {"var": "maintenance", "at": "next"},
            {"op": "always", "args": [{"var": "maintenance"}]},
        ]
        for formula in expressions:
            changed = deepcopy(payload)
            changed["requirements"][0]["formula"] = formula
            with self.subTest(formula=formula), self.assertRaises(ValueError):
                validate_tlr(changed)

    def test_empty_vocabulary_keeps_unsupported_source_visible(self):
        model = {"schema": "mbse_tlr/1", "variables": [], "requirements": [
            {"id": "R0", "text": "Be dependable. */ part bad : Missing; /*", "status": "unresolved", "reason": "No operational criterion."}
        ]}
        normalized = validate_tlr(model)
        self.assertEqual(tlr_context(normalized), {"variables": [], "background": []})
        text = render_sysml(normalized)
        self.assertIn("formalization status: unresolved", text)
        self.assertNotIn("require constraint", text)
        self.assertNotIn("*/ part bad", text)
        model["assumptions"] = [{"id": "A1", "text": "Always true.", "predicate": True}]
        with self.assertRaises(ValueError):
            validate_tlr(model)

    def test_unsupported_cannot_smuggle_executable_formula(self):
        _, payload = fixture()
        payload["requirements"][2]["formula"] = {"var": "maintenance"}
        with self.assertRaises(ValueError):
            validate_tlr(payload)

    def test_balanced_conjunction_compiles_more_than_sixteen_requirements(self):
        _, payload = fixture()
        context = tlr_context(payload)
        joined = conjunction([{"var": "maintenance"}] * 40)
        checked = validate_formula(joined, context)
        self.assertEqual(emit_formula(checked, context).count("v_maintenance_0"), 40)
        self.assertIs(conjunction([]), True)

    def test_sysml_constraints_and_smt_use_the_same_normalized_expression(self):
        sources, payload = fixture()
        normalized = validate_tlr(payload, sources)
        sysml = render_sysml(normalized)
        self.assertIn("observedSystem.v_maintenance implies (not observedSystem.v_motion_enabled)", sysml)
        self.assertIn("observedSystem.v_voltage <= 28", sysml)
        self.assertIn("canonical unit: V", sysml)
        self.assertIn("domain_voltage_lower { v_voltage >= 0 }", sysml)
        self.assertNotIn("ISQ::", sysml)
        self.assertNotIn("satisfy ", sysml)
        self.assertIn("Unbounded eventuality", sysml)
        smt = emit_formula(normalized["requirements"][1]["formula"], tlr_context(normalized))
        self.assertEqual(smt, "(<= v_voltage_0 28)")

    @unittest.skipUnless(compiler_capability()["available"], "Local SysML pilot compiler unavailable")
    def test_real_compiler_accepts_boolean_arithmetic_background_and_unsupported(self):
        sources, payload = fixture()
        payload["variables"].append({"name": "count", "type": "Int", "bounds": {"lower": "0", "upper": "10"}})
        payload["assumptions"] = [{"id": "Environment", "text": "The observed count differs from two.", "predicate": {
            "op": "!=", "args": [{"var": "count"}, {"value": "2"}]}}]
        payload["requirements"][0]["formula"] = {"op": "and", "args": [payload["requirements"][0]["formula"],
            {"op": "=", "args": [{"var": "count"}, {"op": "ite", "args": [{"var": "maintenance"}, {"value": "0"}, {"value": "3"}]}]},
            {"op": ">=", "args": [{"op": "+", "args": [{"var": "voltage"}, {"value": "1", "unit": "V"}]}, {"value": "-1", "unit": "V"}]},
            {"op": "<=", "args": [{"op": "*", "args": [{"value": "2"}, {"var": "voltage"}]}, {"value": "56", "unit": "V"}]}
        ]}
        executable = render_sysml(validate_tlr(payload, sources), "Compiler operators")
        documentation_only = render_sysml({"schema": "mbse_tlr/1", "variables": [], "requirements": [
            {"id": "U1", "status": "unsupported", "reason": "Unbounded temporal scope.", "text": "Eventually stop."}]})
        with tempfile.TemporaryDirectory() as directory:
            for index, text in enumerate((executable, documentation_only)):
                with self.subTest(index=index):
                    path = Path(directory) / f"candidate{index}.sysml"
                    path.write_text(text)
                    result = compile_sysml(path)
                    self.assertEqual(result["status"], "passed", result)


if __name__ == "__main__":
    unittest.main()
