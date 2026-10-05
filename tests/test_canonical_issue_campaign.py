"""Offline source-fault campaign tests with real Z3 and no model calls."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from canonical_audits import audit_tlr
from canonical_issue_campaign import (preflight, reference_rows, run_campaign,
                                      score_candidate, source_packet, validate_manifest)


def op(name, *args):
    return {"op": name, "args": list(args)}


def fixture():
    source = {"document": "Example communication system", "context": {
        "scope": "Availability of the named operations; execution is not asserted.",
        "excerpts": [{"id": "TERM", "quote": "A unicast message has one intended recipient."}]}}
    return {"schema": "source_issue_campaign/1", "id": "communications_fixture",
            "context": {"variables": [{"name": "send_available", "type": "Bool"},
                                       {"name": "receive_available", "type": "Bool"}],
                        "background": [], "symbol_meanings": {
                            "send_available": "Ability to send a unicast message is available.",
                            "receive_available": "Ability to receive a unicast message is available."}},
            "baseline": {"expected_status": "sat", "requirements": [
                {"id": "SEND", "text": "The system shall provide the ability to send a unicast message.",
                 "source": deepcopy(source), "formula": {"var": "send_available"}},
                {"id": "RECEIVE", "text": "The system shall provide the ability to receive a unicast message.",
                 "source": deepcopy(source), "formula": {"var": "receive_available"}}]},
            "variants": [
                {"id": "conflict", "operation": "append", "kind": "conflict",
                 "expected_status": "unsat", "target_requirement_id": "SEND",
                 "expected_conflict_ids": ["SEND", "ADDED"], "requirement": {
                     "id": "ADDED", "text": "The system shall not provide the ability to send a unicast message.",
                     "source": deepcopy(source), "formula": op("not", {"var": "send_available"})}},
                {"id": "control", "operation": "append", "kind": "consistent_control",
                 "expected_status": "sat", "target_requirement_id": "SEND",
                 "expected_conflict_ids": [], "requirement": {
                     "id": "ADDED", "text": "Unicast sending shall be available in the system.",
                     "source": deepcopy(source), "formula": {"var": "send_available"}}}]}


def candidate_tlr(manifest, variant=None):
    return {"schema": "mbse_tlr/1", "variables": deepcopy(manifest["context"]["variables"]),
            "assumptions": deepcopy(manifest["context"]["background"]),
            "requirements": [{**row, "status": "supported"} for row in reference_rows(manifest, variant)]}


@unittest.skipUnless(shutil.which("z3"), "A local Z3 executable is required.")
class SourceIssueCampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.manifest = validate_manifest(fixture())

    def score(self, variant=None, tlr=None, eligible=None):
        tlr = candidate_tlr(self.manifest, variant) if tlr is None else tlr
        if eligible is None:
            eligible = [r["id"] for r in tlr["requirements"] if r["status"] == "supported"]
        audit = audit_tlr(tlr, self.directory / "candidate_audit", eligible_ids=eligible)
        result = {"status": "completed", "analysis": audit,
                  "source_review": {"eligible_ids": eligible}, "tlr": tlr,
                  "compilation": {"status": "not_run"}}
        return score_candidate(self.manifest, variant, result, self.directory / "score")

    def test_preflight_checks_clean_conflict_control_and_restoration(self):
        result = preflight(self.manifest, self.directory / "preflight")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["background"]["status"], "sat")
        self.assertEqual(result["baseline"]["status"], "sat")
        fault, control = result["variants"]
        self.assertEqual(fault["variant"]["status"], "unsat")
        self.assertEqual(fault["expected_core"]["status"], "unsat")
        self.assertTrue(all(x["result"]["status"] == "sat" for x in fault["core_deletions"]))
        self.assertEqual(fault["restoration"]["status"], "sat")
        self.assertEqual(control["variant"]["status"], "sat")

    def test_invalid_reference_behavior_blocks_generation(self):
        self.manifest["variants"][0]["requirement"]["formula"] = {"var": "send_available"}
        calls = []
        result = run_campaign(self.manifest, self.directory / "campaign",
                              runner=lambda *args, **kwargs: calls.append((args, kwargs)))
        self.assertEqual(result["status"], "reference_preflight_failed")
        self.assertEqual(result["samples"], [])
        self.assertEqual(calls, [])

    def test_removed_inventory_budget_rejected_before_output_or_calls(self):
        with self.assertRaisesRegex(ValueError, "historical"):
            run_campaign(self.manifest, self.directory / "campaign", inventory_repairs=1,
                         runner=lambda *a, **k: self.fail("Unexpected conversion"))
        self.assertFalse((self.directory / "campaign").exists())

    def test_faithful_conflict_gets_reference_supported_localized_detection(self):
        score = self.score(self.manifest["variants"][0])
        self.assertEqual(score["diagnostic_outcome"], "detected")
        self.assertEqual(score["reported_conflict_ids"], ["ADDED", "SEND"])
        self.assertTrue(score["exact_localization"])
        self.assertTrue(score["reference_supported_detection"])
        self.assertTrue(score["coverage_complete"])
        self.assertTrue(score["all_formulas_preserved"])

    def test_complete_clean_packet_is_a_correct_negative(self):
        score = self.score()
        self.assertEqual(score["diagnostic_outcome"], "correct_negative")
        self.assertTrue(score["coverage_complete"])
        self.assertTrue(score["all_formulas_preserved"])

    def test_partial_sat_clean_packet_does_not_get_correct_negative_credit(self):
        score = self.score(eligible=["SEND"])
        self.assertEqual(score["consistency_status"], "sat")
        self.assertFalse(score["coverage_complete"])
        self.assertEqual(score["diagnostic_outcome"], "inconclusive")

    def test_dropping_conflicting_clause_yields_inconclusive_not_success(self):
        variant = self.manifest["variants"][0]
        tlr = candidate_tlr(self.manifest, variant)
        row = tlr["requirements"][-1]
        row.pop("formula")
        row.update(status="unsupported", reason="The proposed candidate abstained.")
        score = self.score(variant, tlr)
        self.assertEqual(score["consistency_status"], "sat")
        self.assertEqual(score["diagnostic_outcome"], "inconclusive")
        self.assertFalse(score["reference_supported_detection"])
        self.assertFalse(score["all_formulas_preserved"])

    def test_non_equivalent_unsat_is_not_reference_supported_detection(self):
        variant = self.manifest["variants"][0]
        tlr = candidate_tlr(self.manifest, variant)
        tlr["requirements"][0]["formula"] = op("and", {"var": "send_available"}, {"var": "receive_available"})
        score = self.score(variant, tlr)
        self.assertEqual(score["diagnostic_outcome"], "detected")
        self.assertTrue(score["exact_localization"])
        self.assertFalse(score["reference_supported_detection"])
        self.assertFalse(score["all_formulas_preserved"])
        self.assertEqual(next(r for r in score["reference_comparisons"] if r["requirement_id"] == "SEND")["classification"], "strengthened")

    def test_complete_sat_translation_of_conflict_is_a_miss(self):
        variant = self.manifest["variants"][0]
        tlr = candidate_tlr(self.manifest, variant)
        tlr["requirements"][-1]["formula"] = {"var": "send_available"}
        score = self.score(variant, tlr)
        self.assertEqual(score["diagnostic_outcome"], "missed")
        self.assertFalse(score["all_formulas_preserved"])

    def test_invented_conflict_in_clean_source_is_a_false_alarm(self):
        tlr = candidate_tlr(self.manifest)
        tlr["requirements"][1]["formula"] = op("not", {"var": "send_available"})
        score = self.score(tlr=tlr)
        self.assertEqual(score["diagnostic_outcome"], "false_alarm")
        self.assertFalse(score["all_formulas_preserved"])

    def test_nested_answer_metadata_is_rejected(self):
        for key in ("formula", "expected_status", "expected_conflict_ids", "expected_answer",
                    "expected_formula", "reference_formula", "canonical_formula", "ground_truth",
                    "expected_classification", "treatment", "mutation_kind", "fault_category"):
            with self.subTest(key=key):
                manifest = fixture()
                manifest["baseline"]["requirements"][0]["source"]["nested"] = [{key: "evaluator answer"}]
                with self.assertRaisesRegex(ValueError, "Evaluation answers"):
                    validate_manifest(manifest)

    def test_replacement_rejects_stale_quote_in_neighbor_metadata(self):
        manifest = fixture()
        original = manifest["baseline"]["requirements"][0]
        manifest["baseline"]["requirements"][1]["source"]["related_text"] = original["text"]
        mutation = manifest["variants"][0]
        mutation.update(operation="replace", expected_conflict_ids=["SEND", "RECEIVE"])
        mutation["requirement"]["id"] = "SEND"
        with self.assertRaisesRegex(ValueError, "stale original quotation"):
            validate_manifest(manifest)

    def test_converter_receives_no_reference_formulas_or_treatment_labels(self):
        sources = source_packet(self.manifest, self.manifest["variants"][0])
        self.assertEqual(set(sources[0]), {"id", "text", "source"})
        serialized = json.dumps(sources)
        for field in ("expected_status", "expected_conflict_ids", "formula", '"kind": "conflict"'):
            self.assertNotIn(field, serialized)
        self.assertEqual(sources[-1]["text"], self.manifest["variants"][0]["requirement"]["text"])
        self.assertIn("one intended recipient", serialized)

    def test_full_offline_campaign_freezes_three_candidates_and_keeps_denominators(self):
        calls = []

        def runner(sources, output_dir, **kwargs):
            calls.append((deepcopy(sources), deepcopy(kwargs)))
            variant = None if len(sources) == 2 else next(v for v in self.manifest["variants"]
                                                        if v["requirement"]["text"] == sources[-1]["text"])
            tlr = candidate_tlr(self.manifest, variant)
            ids = [r["id"] for r in sources]
            return {"status": "completed", "tlr": tlr,
                    "analysis": audit_tlr(tlr, Path(output_dir) / "audit")}

        result = run_campaign(self.manifest, self.directory / "campaign", runner=runner, workers=2)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("require_obligation_inventory" not in kwargs and "inventory_repairs" not in kwargs
                            and "reviewer" not in kwargs for _, kwargs in calls))
        config = json.loads((self.directory / "campaign/configuration.json").read_text())
        self.assertEqual(config["max_model_transport_invocations_per_packet"], 3)
        self.assertEqual(result["summary"]["planned_packets"], 3)
        self.assertEqual(result["summary"]["reference_supported_detections"], 1)
        self.assertEqual(result["summary"]["correct_negatives"], 2)
        frozen = json.loads((self.directory / "campaign" / "frozen_candidates.json").read_text())
        self.assertEqual(len(frozen), 3)

    def test_unexpected_conversion_failures_remain_in_all_packet_denominators(self):
        calls = []

        def runner(sources, output_dir, **kwargs):
            calls.append(sources)
            raise RuntimeError("A transport failure occurred.")

        result = run_campaign(self.manifest, self.directory / "campaign", runner=runner, workers=2)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["summary"]["planned_packets"], 3)
        self.assertEqual(result["summary"]["inconclusive_packets"], 3)
        self.assertEqual(result["summary"]["reference_supported_detections"], 0)
        for sample in result["samples"]:
            self.assertEqual(sample["generation"]["status"], "failed")
            self.assertTrue((self.directory / "campaign" / sample["packet"] / "conversion_failure.json").exists())

    def test_invalid_tlr_context_is_retained_as_not_comparable(self):
        tlr = candidate_tlr(self.manifest)
        tlr["variables"][0]["type"] = "UnsupportedType"
        result = {"status": "failed", "tlr": tlr, "analysis": {"status": "not_run"}}
        score = score_candidate(self.manifest, None, result, self.directory / "score")
        self.assertEqual(score["diagnostic_outcome"], "inconclusive")
        self.assertFalse(score["context_matches"])
        self.assertFalse(score["all_formulas_preserved"])
        self.assertTrue(all(r["classification"] == "not_comparable" for r in score["reference_comparisons"]))


if __name__ == "__main__":
    unittest.main()
