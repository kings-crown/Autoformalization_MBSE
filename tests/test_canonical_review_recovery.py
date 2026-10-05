"""Offline recovery keeps usable semantic decisions and the reviewed inputs fixed."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from test_canonical_source_review import fixture, response_for
from test_canonical_obligations import fixture as inventory_fixture, accepted_review
from test_canonical_obligation_review import fixture as component_fixture, response as component_response
from canonical_source_review import review_candidate, validate_review
from canonical_obligations import prepare_inventory, validate_inventory_review


def reject(row, disposition="revise"):
    row["disposition"] = disposition
    row["reason"] = "The source meaning requires correction or clarification."
    if row.get("dimensions"):
        row["dimensions"][0]["status"] = "fail" if disposition == "revise" else "unresolved"
    return row


class SourceReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.sources, self.tlr = fixture()
        self.good = response_for(self.sources, self.tlr)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_review(self, callback, **options):
        return review_candidate(self.sources, self.tlr, {"fixed": "context"}, self.tmp.name,
                                "offline", callback, review_repairs=1, **options)

    def test_invalid_json_recovery_records_both_calls_and_usage(self):
        calls = []
        def callback(system, prompt, model, directory, name):
            payload = json.loads(prompt)
            calls.append(payload)
            (directory / f"{name}.json").write_text(json.dumps({"input_tokens": 17, "output_tokens": 11}))
            return "{invalid" if len(calls) == 1 else json.dumps(self.good)
        report = self.run_review(callback)
        self.assertTrue(report["complete"])
        self.assertEqual((report["call_count"], report["review_repair_calls"]), (2, 1))
        self.assertIsNone(report["failure"])
        self.assertEqual(calls[0]["source_packet"], calls[1]["source_packet"])
        self.assertEqual(calls[0]["candidate_tlr"], calls[1]["candidate_tlr"])
        self.assertEqual(calls[0]["fixed_context"], calls[1]["fixed_context"])
        target = Path(self.tmp.name) / "source_review"
        for name in ("original_report.json", "response.txt", "review_correction_1_response.txt", "merged_response.json"):
            self.assertTrue((target / name).is_file())
        for name in ("review_call.json", "review_correction_1_call.json"):
            self.assertEqual(json.loads((target / name).read_text())["input_tokens"], 17)

    def test_bad_citation_recovers_only_affected_row_and_preserves_valid_rejection(self):
        bad = deepcopy(self.good)
        reject(bad["requirements"][0], "needs_clarification")
        bad["requirements"][1]["source_basis"][0]["quote"] = "not source text"
        callback = Mock(side_effect=[json.dumps(bad), json.dumps(self.good)])
        report = self.run_review(callback)
        request = json.loads(callback.call_args.args[1])["review_recovery"]
        self.assertEqual([r["id"] for r in request["requirements"]], ["R2"])
        self.assertFalse(request["background"])
        self.assertEqual(report["response"]["requirements"][0], bad["requirements"][0])
        self.assertEqual(report["eligible_ids"], ["R2"])

    def test_semantic_rejection_and_uncertainty_do_not_trigger_response_recovery(self):
        for disposition in ("revise", "needs_clarification", "unsupported"):
            raw = deepcopy(self.good)
            reject(raw["requirements"][0], disposition)
            callback = Mock(return_value=json.dumps(raw))
            report = self.run_review(callback)
            self.assertEqual(report["review_repair_calls"], 0)
            callback.assert_called_once()

    def test_transport_errors_are_not_retried_and_invalid_recovery_is_bounded(self):
        callback = Mock(side_effect=TimeoutError("offline transport"))
        report = self.run_review(callback)
        self.assertEqual(report["call_count"], 1)
        self.assertEqual(report["failure"]["kind"], "transport_error")
        callback = Mock(return_value="{invalid")
        report = self.run_review(callback)
        self.assertEqual(report["call_count"], 2)
        self.assertFalse(report["complete"])
        self.assertEqual(callback.call_count, 2)

    def test_failed_recovery_retains_original_valid_companion_and_no_global_failure(self):
        bad = deepcopy(self.good)
        bad["requirements"].pop()
        callback = Mock(side_effect=[json.dumps(bad), TimeoutError("recovery unavailable")])
        report = self.run_review(callback)
        self.assertEqual(report["eligible_ids"], ["R1"])
        self.assertIsNone(report["failure"])
        self.assertEqual(report["review_recovery"]["failure"]["kind"], "transport_error")

    def test_exact_reuse_preserves_recovered_response_without_calls(self):
        callback = Mock(side_effect=["{invalid", json.dumps(self.good)])
        first = self.run_review(callback)
        second = self.run_review(callback, previous=first)
        self.assertTrue(second["complete"])
        self.assertEqual(second["call_count"], 0)
        self.assertEqual(callback.call_count, 2)

    def test_invalid_parent_preserves_usable_component_negative_during_recovery(self):
        self.sources, self.tlr, inventory = component_fixture()
        good = component_response(self.sources, self.tlr, inventory)
        bad = deepcopy(good)
        reject(bad["requirements"][0]["obligations"][0])
        bad["requirements"][0]["source_basis"][0]["quote"] = "not source text"
        before = validate_review(bad, self.sources, self.tlr, inventory)
        self.assertEqual(before["requirements"][0]["obligations"][0]["disposition"], "revise")
        callback = Mock(side_effect=[json.dumps(bad), json.dumps(good)])
        report = self.run_review(callback, obligation_inventory=inventory)
        self.assertEqual(report["response"]["requirements"][0]["obligations"][0], bad["requirements"][0]["obligations"][0])
        self.assertEqual(report["requirements"][0]["disposition"], "revise")
        self.assertEqual(report["eligible_ids"], ["R2"])


class InventoryReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.sources, self.inventory = inventory_fixture()
        self.good = accepted_review(self.sources, self.inventory)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_review(self, callback, **options):
        return prepare_inventory(self.sources, self.tmp.name, "offline", None, callback,
                                 supplied=self.inventory, review_repairs=1, **options)

    def test_invalid_json_recovers_same_frozen_inventory_with_actual_counts(self):
        callback = Mock(side_effect=["{invalid", self.good])
        report = self.run_review(callback)
        self.assertTrue(report["complete"])
        self.assertEqual((report["generation_call_count"], report["review_call_count"], report["call_count"], report["review_repair_calls"]), (0, 2, 2, 1))
        first, second = [json.loads(call.args[1]) for call in callback.call_args_list]
        self.assertEqual(first["obligation_inventory"], second["obligation_inventory"])
        self.assertEqual(first["source_packet"], second["source_packet"])
        self.assertIsNotNone(report["attempts"][0]["original_failure"])
        self.assertIsNone(report["failure"])

    def test_partial_recovery_cannot_flip_valid_parent_or_component_findings(self):
        bad = deepcopy(self.good)
        reject(bad["requirements"][0], "needs_clarification")
        bad["requirements"][1]["source_basis"][0]["quote"] = "invented source"
        reject(bad["requirements"][1]["obligations"][0])
        before = validate_inventory_review(bad, self.sources, self.inventory)
        self.assertEqual(before["requirements"][1]["obligations"][0]["disposition"], "revise")
        callback = Mock(side_effect=[bad, self.good])
        report = self.run_review(callback)
        request = json.loads(callback.call_args.args[1])["review_recovery"]
        self.assertEqual([r["id"] for r in request["requirements"]], ["R2"])
        self.assertEqual(report["response"]["requirements"][0], bad["requirements"][0])
        self.assertEqual(report["response"]["requirements"][1]["obligations"][0], bad["requirements"][1]["obligations"][0])
        self.assertFalse(report["complete"])

    def test_malformed_component_does_not_resubmit_valid_parent_and_sibling(self):
        bad = deepcopy(self.good)
        del bad["requirements"][0]["obligations"][1]["reason"]
        callback = Mock(side_effect=[bad, self.good])
        report = self.run_review(callback)
        self.assertTrue(report["complete"])
        request = json.loads(callback.call_args.args[1])["review_recovery"]["requirements"][0]
        self.assertFalse(request["parent"])
        self.assertEqual(request["obligation_ids"], ["R1.O2"])

    def test_malformed_record_id_keeps_other_rows_available(self):
        bad = deepcopy(self.good)
        bad["requirements"].append({"id": []})
        report = validate_inventory_review(bad, self.sources, self.inventory)
        self.assertEqual([r["disposition"] for r in report["requirements"]], ["pass", "pass"])
        self.assertFalse(report["complete"])

    def test_unusable_component_cannot_spend_semantic_inventory_revision_budget(self):
        bad = deepcopy(self.good)
        reject(bad["requirements"][0])
        del bad["requirements"][0]["obligations"][1]["reason"]
        generator = Mock(return_value=self.inventory)
        reviewer = Mock(return_value=bad)
        report = prepare_inventory(self.sources, self.tmp.name, "offline", generator, reviewer,
                                   correction_attempts=1)
        self.assertFalse(report["complete"])
        self.assertEqual(report["generation_call_count"], 1)
        self.assertEqual(report["review_call_count"], 1)

    def test_valid_semantic_review_and_transport_failure_do_not_retry(self):
        for value in ("revise", "needs_clarification"):
            bad = deepcopy(self.good)
            reject(bad["requirements"][0], value)
            callback = Mock(return_value=bad)
            self.assertEqual(self.run_review(callback)["review_repair_calls"], 0)
            callback.assert_called_once()
        callback = Mock(side_effect=TimeoutError("offline transport"))
        self.assertEqual(self.run_review(callback)["call_count"], 1)

    def test_second_malformed_response_stops_and_records_original_evidence(self):
        callback = Mock(return_value="{invalid")
        report = self.run_review(callback)
        self.assertFalse(report["complete"])
        self.assertEqual(report["call_count"], 2)
        self.assertEqual(report["attempts"][0]["review_recovery"]["failure"]["kind"], "invalid_response")

    def test_invalid_budgets_fail_before_transport(self):
        for budget in (-1, 2, True, "1"):
            with self.assertRaisesRegex(ValueError, "review_repairs"):
                prepare_inventory(self.sources, self.tmp.name, "offline", None, None,
                                  supplied=self.inventory, review_repairs=budget)
            sources, tlr = fixture()
            with self.assertRaisesRegex(ValueError, "review_repairs"):
                review_candidate(sources, tlr, None, self.tmp.name, "offline", None, review_repairs=budget)


if __name__ == "__main__":
    unittest.main()
