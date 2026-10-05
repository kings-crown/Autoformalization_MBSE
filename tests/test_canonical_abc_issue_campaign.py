"""Full-packet study boundaries, scoped scoring, and restart safety; no paid calls."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from canonical_abc_issue_campaign import (collect_issue_report, issue_packet, packet_plan,
    preflight, run_campaign, score_issue_report, score_solver, source_packet,
    validate_issue_report, validate_manifest)


def fixture():
    return {"schema": "abc_source_issue_campaign/1", "id": "full_document",
        "baseline": {"requirements": [
            {"id": "SEND", "text": "The system shall provide sending.", "source": {"document": "Example"}},
            {"id": "OTHER", "text": "The system shall preserve ordered delivery.", "source": {"document": "Example"}}]},
        "evaluator": {"baseline_global_status": "unknown",
            "context": {"variables": [{"name": "send", "type": "Bool"}], "background": []},
            "references": [{"id": "SEND", "formula": {"var": "send"}}]},
        "variants": [
            {"id": "opposition", "operation": "append",
             "requirement": {"id": "ADDED", "text": "The system shall not provide sending.", "source": {"document": "Constructed"}},
             "evaluator": {"kind": "conflict", "target_requirement_id": "SEND",
                           "expected_pair_ids": ["SEND", "ADDED"], "formula": {"op": "not", "args": [{"var": "send"}]}}},
            {"id": "restatement", "operation": "append",
             "requirement": {"id": "ADDED", "text": "Sending shall be provided by the system.", "source": {"document": "Constructed"}},
             "evaluator": {"kind": "consistent_control", "target_requirement_id": "SEND",
                           "expected_pair_ids": ["SEND", "ADDED"], "formula": {"var": "send"}}}]}


def issue_response(packet, ids=None):
    sources = packet["sources"]
    by_id = {row["id"]: row for row in sources}
    conflicts = []
    if ids:
        conflicts = [{"source_ids": ids, "explanation": "The same scoped availability is required and forbidden.",
                      "evidence": [{"artifact": "source", "source_id": rid, "quote": by_id[rid]["text"]} for rid in ids],
                      "uncertainty": "This is a source interpretation, not engineer approval."}]
    return {"schema": "source_issue_report/1", "status": "conflict_reported" if ids else "no_conflict_reported",
            "scope": {"assessed_requirement_ids": list(by_id), "coverage": "complete",
                      "limitations": "Model scope may be partial."}, "conflicts": conflicts,
            "explanation": "All source rows considered.", "uncertainty": "Global consistency is not established."}


def collected(report):
    return {"status": "completed", "report": report, "actual_calls": 1}


class IssueBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.manifest = validate_manifest(fixture())

    def test_full_original_packet_preserved_for_every_variant(self):
        plan = packet_plan(self.manifest)
        self.assertEqual([len(p["sources"]) for p in plan], [2, 3, 3])
        for item in plan:
            self.assertEqual(item["sources"][:2], self.manifest["baseline"]["requirements"])
            self.assertNotIn("formula", json.dumps(item["sources"]))
        self.assertIsNone(self.manifest["generator_context"])

    def test_new_issue_packets_do_not_invent_separate_source_review_evidence(self):
        for result in ({}, {"source_review": {"status": "not_run"}}):
            for arm in "BC":
                packet = issue_packet(source_packet(self.manifest), "package Example {}", result, arm)
                self.assertNotIn("source_review", packet["artifacts"])

    def test_removed_gate_budgets_rejected_before_output_or_calls(self):
        for options in ({"inventory_repairs": 1}, {"review_repairs": 1}):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "historical"):
                run_campaign(self.manifest, self.path / "campaign", **options,
                             runner=lambda *a, **k: self.fail("Unexpected conversion"))
        self.assertFalse((self.path / "campaign").exists())

    def test_current_solver_coverage_comes_from_executed_rows_without_review(self):
        result = {"analysis": {"background_status": "sat", "consistency_status": "sat",
                               "supported_requirement_ids": [row["id"] for row in source_packet(self.manifest)]}}
        score = score_solver(self.manifest, None, result, self.path / "score")
        self.assertTrue(score["coverage_complete"])
        self.assertIsNone(score["correct_negative"])

    def test_labels_and_reference_formulas_rejected_in_nested_source_metadata(self):
        for key in ("expected_pair_ids", "formula", "ground_truth", "treatment"):
            raw = fixture()
            raw["baseline"]["requirements"][0]["source"]["nested"] = [{key: "leak"}]
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "Evaluator answers"):
                validate_manifest(raw)

    def test_full_baseline_cannot_inherit_scoped_sat_label(self):
        raw = fixture()
        raw["evaluator"]["baseline_global_status"] = "sat"
        with self.assertRaisesRegex(ValueError, "unknown"):
            validate_manifest(raw)

    def test_arm_packets_have_only_own_permitted_artifacts_without_labels(self):
        source = source_packet(self.manifest)
        result = {"condition": "C", "tlr": {"schema": "mbse_tlr/1"},
                  "source_review": {"eligible_ids": ["SEND"], "condition": "B", "binding": {"secret": True}},
                  "analysis": {"consistency_status": "sat", "expected_pair_ids": ["leak"], "condition": "C"}}
        expected = [{"sysml"}, {"sysml", "tlr", "source_review"}, {"sysml", "tlr", "source_review", "solver_audit"}]
        for arm, artifacts in zip("ABC", expected):
            packet = issue_packet(source, "package Example {}", result, arm)
            self.assertEqual(set(packet["artifacts"]), artifacts)
            encoded = json.dumps(packet)
            self.assertNotIn('"condition"', encoded)
            self.assertNotIn("expected_pair_ids", encoded)
            self.assertNotIn("secret", encoded)

    def test_reports_require_literal_source_evidence_for_both_members(self):
        packet = issue_packet(source_packet(self.manifest, self.manifest["variants"][0]), None, {}, "A")
        response = issue_response(packet, ["SEND", "ADDED"])
        self.assertEqual(validate_issue_report(response, packet), response)
        response["conflicts"][0]["evidence"].pop()
        with self.assertRaisesRegex(ValueError, "every reported"):
            validate_issue_report(response, packet)
        response = issue_response(packet, ["SEND", "ADDED"])
        response["conflicts"][0]["evidence"][0]["quote"] = "Invented source quotation."
        with self.assertRaisesRegex(ValueError, "not literal"):
            validate_issue_report(response, packet)

    def test_policy_metadata_is_not_source_evidence_but_shared_applicable_excerpt_is(self):
        sources = source_packet(self.manifest, self.manifest["variants"][0])
        for row in sources:
            row["source"]["obligation_assignment"] = "Policy guidance is not source text."
            row["source"]["context"] = {"applicable_context_ids": ["DEF"]}
        sources[0]["source"]["context"]["excerpts"] = [{"id": "DEF", "quote": "Sending is a capability."}]
        packet = issue_packet(sources, None, {}, "A")
        response = issue_response(packet, ["SEND", "ADDED"])
        for item in response["conflicts"][0]["evidence"]:
            item["quote"] = "Policy guidance is not source text."
        with self.assertRaisesRegex(ValueError, "not literal"):
            validate_issue_report(response, packet)
        for item in response["conflicts"][0]["evidence"]:
            item["quote"] = "Sending is a capability."
        self.assertEqual(validate_issue_report(response, packet), response)

    def test_complete_scope_cannot_skip_an_original_requirement(self):
        packet = issue_packet(source_packet(self.manifest), None, {}, "A")
        response = issue_response(packet)
        response["scope"]["assessed_requirement_ids"] = ["SEND"]
        with self.assertRaisesRegex(ValueError, "every source"):
            validate_issue_report(response, packet)

    def test_common_conflict_detection_does_not_require_a_tlr_or_solver(self):
        variant = self.manifest["variants"][0]
        packet = issue_packet(source_packet(self.manifest, variant), "package Direct {}", {}, "A")
        score = score_issue_report(variant, collected(issue_response(packet, ["SEND", "ADDED"])))
        self.assertTrue(score["expected_pair_reported"])
        self.assertTrue(score["exact_localization"])
        self.assertEqual(score["outcome"], "detected_exact_pair")
        larger = score_issue_report(variant, collected(issue_response(packet, ["SEND", "ADDED", "OTHER"])))
        self.assertFalse(larger["expected_pair_reported"])
        self.assertTrue(larger["expected_pair_in_larger_report"])

    def test_control_exact_pair_false_alarm_is_separate_from_unrelated_conflict(self):
        variant = self.manifest["variants"][1]
        packet = issue_packet(source_packet(self.manifest, variant), None, {}, "A")
        exact = score_issue_report(variant, collected(issue_response(packet, ["SEND", "ADDED"])))
        unrelated = score_issue_report(variant, collected(issue_response(packet, ["SEND", "OTHER"])))
        larger = score_issue_report(variant, collected(issue_response(packet, ["SEND", "ADDED", "OTHER"])))
        self.assertTrue(exact["scoped_control_false_alarm"])
        self.assertFalse(unrelated["scoped_control_false_alarm"])
        self.assertEqual(larger["outcome"], "control_pair_in_larger_report_requires_review")
        for score in (exact, unrelated, larger):
            self.assertEqual(score["global_consistency"], "unknown")
            self.assertIsNone(score["correct_negative"])

    def test_baseline_report_stays_unadjudicated(self):
        packet = issue_packet(source_packet(self.manifest), None, {}, "A")
        score = score_issue_report(None, collected(issue_response(packet, ["SEND", "OTHER"])))
        self.assertEqual(score["outcome"], "unadjudicated_baseline_report")
        self.assertIsNone(score["correct_negative"])

    def test_one_format_recovery_is_saved_and_resume_makes_no_calls(self):
        packet = issue_packet(source_packet(self.manifest), None, {}, "A")
        calls = []
        def ask(system, prompt, model, directory, call_id):
            calls.append(prompt)
            return "invalid json" if len(calls) == 1 else json.dumps(issue_response(packet))
        result = collect_issue_report(packet, self.path / "issues", model="fake", ask=ask)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["actual_calls"], 2)
        repeated = collect_issue_report(packet, self.path / "issues", model="fake", ask=lambda *a: self.fail("Unexpected call"))
        self.assertEqual(result, repeated)
        self.assertTrue((self.path / "issues" / "attempt-00.json").exists())

    def test_transport_failure_is_not_retried_as_format_failure(self):
        packet = issue_packet(source_packet(self.manifest), None, {}, "A")
        calls = []
        def ask(*args):
            calls.append(args)
            raise RuntimeError("Unavailable transport")
        result = collect_issue_report(packet, self.path / "issues", model="fake", ask=ask)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(len(calls), 1)

    def test_citation_recovery_cannot_change_well_formed_issue_findings(self):
        packet = issue_packet(source_packet(self.manifest, self.manifest["variants"][0]), None, {}, "A")
        broken = issue_response(packet, ["SEND", "ADDED"])
        broken["conflicts"][0]["evidence"][0]["quote"] = "Nonliteral quotation."
        replies = iter([broken, issue_response(packet)])
        result = collect_issue_report(packet, self.path / "issues", model="fake", ask=lambda *a: json.dumps(next(replies)))
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("locked", result["error"])

    def test_changed_frozen_artifact_rejects_issue_resume(self):
        packet = issue_packet(source_packet(self.manifest), None, {}, "A")
        collect_issue_report(packet, self.path / "issues", model="fake", ask=lambda *a: json.dumps(issue_response(packet)))
        packet["artifacts"]["sysml"] = "changed"
        with self.assertRaisesRegex(ValueError, "changed"):
            collect_issue_report(packet, self.path / "issues", model="fake", ask=lambda *a: self.fail("Unexpected call"))

    def test_partial_sat_is_never_a_correct_negative(self):
        result = {"analysis": {"background_status": "sat", "consistency_status": "sat"},
                  "source_review": {"eligible_ids": ["SEND"]}}
        score = score_solver(self.manifest, None, result, self.path / "score")
        self.assertFalse(score["coverage_complete"])
        self.assertIsNone(score["correct_negative"])

    def test_free_generated_vocabulary_never_receives_invented_reference_mapping(self):
        variant = self.manifest["variants"][0]
        result = {"tlr": {"variables": [{"name": "renamed", "type": "Bool"}], "assumptions": [],
                          "requirements": [{"id": "SEND", "status": "supported", "formula": {"var": "renamed"}}]},
                  "source_review": {"eligible_ids": ["SEND", "ADDED"]},
                  "analysis": {"background_status": "sat", "consistency_status": "unsat",
                               "consistency": {"unsat_core": {"status": "available", "requirement_ids": ["SEND", "ADDED"]}}}}
        score = score_solver(self.manifest, variant, result, self.path / "score")
        self.assertTrue(score["exact_expected_pair_core"])
        self.assertFalse(score["reference_context_matches"])
        self.assertFalse(score["reference_supported_detection"])
        self.assertTrue(all(row["classification"] == "not_comparable" for row in score["reference_comparisons"]))


@unittest.skipUnless(shutil.which("z3"), "Local Z3 required")
class CampaignExecution(unittest.TestCase):
    setUp = IssueBoundaries.setUp
    def fake_runner(self, sources, directory, **kwargs):
        self.generated.append((sources, kwargs))
        self.assertEqual(kwargs["feedback_repairs"], 2)
        self.assertNotIn("inventory_repairs", kwargs)
        self.assertEqual(kwargs["format_repairs"], 1)
        self.assertNotIn("review_repairs", kwargs)
        self.assertNotIn("require_obligation_inventory", kwargs)
        self.assertIsNone(kwargs["context"])
        self.assertNotIn("formula", json.dumps(sources))
        row = {"repetition": 1}
        for arm in "ABC":
            path = Path(directory) / "rep-001" / arm
            path.mkdir(parents=True)
            (path / "model.sysml").write_text("package Example {}")
            row[arm] = {"status": "completed", "model_file": "model.sysml", "analysis": {"status": "not_run"}}
        study = {"schema": "canonical_study/2", "status": "completed", "rows": [row]}
        (Path(directory) / "study.json").write_text(json.dumps(study))
        return study

    def fake_ask(self, system, prompt, model, directory, call_id):
        self.assertEqual(len(self.generated), 3, "All conversions must freeze before issue reporting")
        self.report_calls.append(prompt)
        packet = json.loads(prompt)
        ids = ["SEND", "ADDED"] if "shall not" in json.dumps(packet["sources"]) else None
        return json.dumps(issue_response(packet, ids))

    def launch(self, **kwargs):
        self.generated, self.report_calls = [], []
        return run_campaign(self.manifest, self.path / "campaign", runner=self.fake_runner,
                            issue_ask=self.fake_ask, compile_model=False, workers=1, **kwargs)

    def test_preflight_is_scoped_and_validates_control_equivalence(self):
        result = preflight(self.manifest, self.path / "preflight")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["baseline_global_status"], "unknown")
        self.assertEqual(result["variants"][0]["pair"]["status"], "unsat")
        self.assertEqual(result["variants"][1]["control_equivalence"]["classification"], "equivalent")

    def test_invalid_expected_conflict_blocks_every_model_call(self):
        self.manifest["variants"][0]["evaluator"]["formula"] = {"var": "send"}
        result = self.launch()
        self.assertEqual(result["status"], "reference_preflight_failed")
        self.assertEqual(self.generated, [])
        self.assertEqual(self.report_calls, [])

    def test_campaign_full_packet_common_reports_and_safe_resume(self):
        result = self.launch()
        self.assertEqual(len(self.generated), 3)
        self.assertEqual(len(self.report_calls), 9)
        for arm in "ABC":
            self.assertEqual(result["summary"]["by_condition"][arm]["exact_pair_localizations"], 1)
        repeated = run_campaign(self.manifest, self.path / "campaign", runner=lambda *a, **k: self.fail("Regenerated"),
                                issue_ask=lambda *a: self.fail("Repeated issue call"),
                                compile_model=False, workers=1, resume=True)
        self.assertEqual(result["summary"], repeated["summary"])
        self.assertTrue((self.path / "campaign" / "packet-001" / "study" / "study.json").exists())

    def test_changed_source_or_configuration_rejects_resume(self):
        self.launch()
        changed = deepcopy(self.manifest)
        changed["baseline"]["requirements"][0]["text"] += " New scope."
        with self.assertRaisesRegex(ValueError, "Resume rejected"):
            run_campaign(changed, self.path / "campaign", compile_model=False, workers=1, resume=True)
        changed_capacity = "32" if os.getenv("MBSE_STATIC_MAX_VARIABLES", "24") == "64" else "64"
        with patch.dict("os.environ", {"MBSE_STATIC_MAX_VARIABLES": changed_capacity}), self.assertRaisesRegex(ValueError, "Resume rejected"):
            run_campaign(self.manifest, self.path / "campaign", compile_model=False, workers=1, resume=True)

    def test_changed_model_cannot_reuse_saved_diagnostics(self):
        self.launch()
        (self.path / "campaign" / "packet-001" / "study" / "rep-001" / "A" / "model.sysml").write_text("Changed model")
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            run_campaign(self.manifest, self.path / "campaign", compile_model=False, workers=1, resume=True)

    def test_failed_runner_keeps_standard_study_inputs_and_every_arm(self):
        def fail(*args, **kwargs):
            raise RuntimeError("Failed before study creation")
        def ask(system, prompt, *args):
            return json.dumps(issue_response(json.loads(prompt)))
        result = run_campaign(self.manifest, self.path / "campaign", runner=fail, issue_ask=ask,
                              compile_model=False, workers=1)
        self.assertEqual(result["summary"]["planned_arm_results"], 9)
        for index in range(1, 4):
            study = self.path / "campaign" / f"packet-{index:03d}" / "study"
            for name in ("study.json", "study_configuration.json", "sources.json"):
                self.assertTrue((study / name).exists())
            self.assertEqual(json.loads((study / "study.json").read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
