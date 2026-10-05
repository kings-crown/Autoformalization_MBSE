"""Versioned fidelity-assessment protocol tests; no empirical judge claims or calls."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from canonical_assertions import (FIDELITY_STATEMENT, FIDELITY_RUBRIC, build_suite,
                                  validate_suite, validate_assertion_response)
from canonical_assertion_judging import EVALUATION_POLICY, evaluate_packets, JUDGE_PROMPT
from canonical_assertion_rescore import rescore_assessment
from canonical_cli import read_json

SOURCES = [{"id": "R1", "text": "The battery voltage shall be at most 28 V."}]
MODEL = "attribute voltage : Real;\nrequire constraint limit { voltage <= 28 }\n"


def authored(sources=SOURCES, count=1):
    return [{"id": source["id"], "assertions": [
        {"id": f"{source['id']}_A{i}", "category": "obligation",
         "statement": "The generated constraint preserves the source obligation.",
         "source_basis": [{"source_id": source["id"], "quote": source["text"]}]}
        for i in range(count)]} for source in sources]


def response(prompt, fidelity_status="pass"):
    fields = json.loads(prompt)
    return {"requirement_id": fields["target_requirement_id"], "assertions": [
        {"id": item["id"],
         "status": fidelity_status if item["category"] == "fidelity" else "pass",
         "rationale": "Synthetic protocol fixture; the semantic verdict was supplied, not inferred.",
         "evidence": [{"start_line": 2, "end_line": 2}],
         "counterexample": "Constructed source/model discrepancy." if item["category"] == "fidelity" and fidelity_status == "fail" else None}
        for item in fields["assertions"]]}


def callback(fidelity_status="pass"):
    return Mock(side_effect=lambda system, prompt, *args: json.dumps(response(prompt, fidelity_status)))


class FidelitySuiteTests(unittest.TestCase):
    def test_new_suite_requires_exact_fidelity_in_addition_to_substantive_checks(self):
        suite = build_suite(SOURCES, authored())
        self.assertEqual(suite["schema"], "sysml_assertions/3")
        checks = suite["requirements"][0]["assertions"]
        self.assertEqual([a["category"] for a in checks], ["obligation", "coverage", "assumptions", "fidelity"])
        self.assertEqual(checks[-1]["statement"], FIDELITY_STATEMENT)
        self.assertEqual(checks[-1]["id"], "ASSERT_0001_FIDELITY")
        for change in ("remove", "duplicate", "replace", "substantive"):
            changed = deepcopy(suite)
            assertions = changed["requirements"][0]["assertions"]
            if change == "remove":
                assertions.pop()
            elif change == "duplicate":
                assertions.append({**assertions[-1], "id": "duplicate"})
            elif change == "replace":
                assertions[-1]["statement"] = "The source-review gate passed."
            else:
                assertions.pop(0)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_suite(changed)

    def test_v2_and_v3_cap_is_22_authored_and_three_fixed_assertions(self):
        for schema in ("sysml_assertions/2", "sysml_assertions/3"):
            with self.subTest(schema=schema):
                self.assertEqual(len(build_suite(SOURCES, authored(count=22), schema=schema)["requirements"][0]["assertions"]), 25)
                with self.assertRaisesRegex(ValueError, "1 to 22"):
                    build_suite(SOURCES, authored(count=23), schema=schema)
        rows = authored()
        rows[0]["assertions"][0]["category"] = "fidelity"
        with self.assertRaisesRegex(ValueError, "appended"):
            build_suite(SOURCES, rows)

    def test_loading_legacy_102_assertions_does_not_add_25_or_relabel_schema(self):
        sources = [{"id": f"R{i}", "text": f"Requirement {i} shall hold."} for i in range(25)]
        rows = authored(sources, count=2)
        for row in rows[:2]:
            row["assertions"].append({**deepcopy(row["assertions"][0]), "id": row["id"] + "_extra"})
        legacy = build_suite(sources, rows, schema="sysml_assertions/1")
        before = deepcopy(legacy)
        checked = validate_suite(json.loads(json.dumps(legacy)))
        self.assertEqual(checked, before)
        self.assertEqual(sum(len(r["assertions"]) for r in checked["requirements"]), 102)
        self.assertEqual(sum(len(r["assertions"]) for r in build_suite(sources, rows)["requirements"]), 127)
        self.assertNotIn("fidelity", {a["category"] for r in checked["requirements"] for a in r["assertions"]})
        wrong = deepcopy(legacy)
        wrong["requirements"][0]["assertions"][0]["category"] = "fidelity"
        with self.assertRaises(ValueError):
            validate_suite(wrong)

    def test_fidelity_cannot_pass_using_review_documentation_only(self):
        suite = build_suite(SOURCES, authored())
        requirement = suite["requirements"][0]
        fields = {"target_requirement_id": "R1", "assertions": requirement["assertions"]}
        raw = response(json.dumps(fields))
        text = MODEL + "// Source-to-rule review passed; therefore this model is faithful.\n"
        raw["assertions"][-1]["evidence"] = [{"start_line": 3, "end_line": 3}]
        validated = validate_assertion_response(raw, requirement, text)
        self.assertEqual(validated["validation"]["status"], "partial")
        self.assertEqual(validated["validation"]["accepted"], 3)
        self.assertEqual(validated["validation"]["rejected"], 1)
        self.assertIn("actual code evidence", validated["validation"]["assertion_errors"][0]["error"])


class FidelityJudgingTests(unittest.TestCase):
    def test_reports_separate_fidelity_and_preserves_failure_amid_other_passes(self):
        suite = build_suite(SOURCES, authored())
        packet = {"id": "candidate", "requirements": SOURCES, "sysml": MODEL}
        fn = callback("fail")
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_packets([packet], suite, ["one", "two"], Path(tmp)/"judges", [fn, fn])
            cfg = read_json(Path(tmp)/"judges/configuration.json")
        self.assertEqual(report["joint"]["planned"], 4)
        self.assertEqual(report["joint"]["pass"], 3)
        self.assertEqual(report["by_category"]["fidelity"]["fail"], 1)
        self.assertEqual(report["fidelity"]["joint"]["pass_rate"], 0)
        self.assertEqual(report["assertion_suite_schema"], "sysml_assertions/3")
        self.assertEqual(cfg["evaluation_policy"], EVALUATION_POLICY)
        for call in fn.call_args_list:
            prompt = json.loads(call.args[1])
            self.assertNotIn("source_review", prompt)
            self.assertNotIn("tlr", prompt)
            self.assertNotIn("admission", prompt)
            self.assertIn(FIDELITY_RUBRIC, call.args[0])

    def test_rescore_never_upgrades_old_suite_or_claims_new_review_calls(self):
        for schema, count in (("sysml_assertions/1", 3), ("sysml_assertions/2", 4), ("sysml_assertions/3", 4)):
            with self.subTest(schema=schema), tempfile.TemporaryDirectory() as tmp:
                suite = build_suite(SOURCES, authored(), schema=schema)
                directory = Path(tmp)
                packet = {"id": "candidate", "requirements": SOURCES, "sysml": MODEL}
                fn = callback()
                original = evaluate_packets([packet], suite, ["one", "two"], directory/"old", [fn, fn])
                fn.reset_mock()
                replay = rescore_assessment(directory/"old", directory/"new")
                fn.assert_not_called()
                self.assertEqual(replay["joint"]["planned"], count)
                self.assertEqual(read_json(directory/"new/assertions.json")["schema"], schema)
                self.assertEqual(replay["fidelity"], original["fidelity"])
                self.assertEqual(replay["rescore"]["assertions_added"], 0)
                self.assertFalse(replay["rescore"]["rubric_changed"])

    def test_constructed_semantic_controls_route_to_blind_fidelity_scoring(self):
        # The labels below are deliberately injected mocks: this tests calibration
        # plumbing and rubric coverage, not whether an actual LLM detects defects.
        cases = (
            ("endpoint", "Voltage shall be at most 28 V.", "voltage < 28", "fail"),
            ("guard", "If enabled, the alarm shall activate.", "alarm", "fail"),
            ("binding", "The battery voltage shall be at most 28 V.", "charger_voltage <= 28", "fail"),
            ("assumption", "Voltage shall be at most 28 V.", "voltage = 0", "fail"),
            ("temporal", "The pump shall stop and remain stopped.", "can_stop", "fail"),
            ("equivalent", "Voltage shall be at most 28 V.", "not (voltage > 28)", "pass"),
        )
        for name, source, expression, expected in cases:
            with self.subTest(control=name), tempfile.TemporaryDirectory() as tmp:
                sources = [{"id": "R1", "text": source}]
                suite = build_suite(sources, authored(sources))
                model = "attribute value : Real;\nrequire constraint target { " + expression + " }\n"
                packet = {"id": name, "requirements": sources, "sysml": model,
                          "expected_assertions": {"ASSERT_0001_FIDELITY": expected}}
                fn = callback(expected)
                report = evaluate_packets([packet], suite, ["one", "two"], Path(tmp)/"judges", [fn, fn])
                self.assertEqual(report["by_category"]["fidelity"][expected], 1)
                self.assertEqual(report["calibration"][0]["observed"], expected)
                for call in fn.call_args_list:
                    prompt = json.loads(call.args[1])
                    self.assertEqual(prompt["source_packet"], sources)
                    self.assertIn(expression, prompt["sysml_with_line_numbers"])
                    self.assertNotIn("expected_assertions", prompt)
                    self.assertNotIn("calibration", prompt)
        for dimension in ("bindings", "conditions and exceptions", "modality", "inclusive/exclusive",
                          "weakening", "strengthening", "guarantees moved into assumptions", "temporal"):
            self.assertIn(dimension, JUDGE_PROMPT)


if __name__ == "__main__":
    unittest.main()
