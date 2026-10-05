"""Source-only inventory repair retains authority, bounds and independent review."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from canonical_obligations import (INVENTORY_INSTRUCTIONS, REVIEW_INSTRUCTIONS, POLICY_VERSION,
                                  prepare_inventory, validate_inventory)
from test_canonical_obligations import fixture, accepted_review


def revision_review(sources, inventory):
    review = accepted_review(sources, inventory)
    review["requirements"][0].update(disposition="revise", reason="The proposed decomposition needs a source-grounded revision.")
    review["requirements"][0]["dimensions"][0].update(status="fail", explanation="A source component needs clearer separation.")
    return review


class InventoryCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.sources, self.inventory = fixture()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def test_context_excerpt_may_have_two_distinct_roles(self):
        row = self.inventory["requirements"][0]
        row["context"].append({"source_id": "CTX1", "role": "exception", "reason": "Also qualifies the stated capability scope."})
        result = validate_inventory(self.inventory, self.sources)
        self.assertEqual(len(result["requirements"][0]["context"]), 2)

    def test_duplicate_context_role_pair_still_rejected(self):
        row = self.inventory["requirements"][0]
        row["context"].append(deepcopy(row["context"][0]))
        with self.assertRaisesRegex(ValueError, "unique.*source_id, role"):
            validate_inventory(self.inventory, self.sources)

    def test_own_requirement_cannot_be_context_even_with_a_different_role(self):
        row = self.inventory["requirements"][0]
        row["context"].append({"source_id": "R1", "role": "definition", "reason": "Attempted self-reference."})
        with self.assertRaisesRegex(ValueError, "distinct from their own"):
            validate_inventory(self.inventory, self.sources)

    def test_stage_instructions_defer_symbols_and_allow_multiple_context_roles(self):
        for prompt in (INVENTORY_INSTRUCTIONS, REVIEW_INSTRUCTIONS):
            self.assertIn("do not introduce a Boolean symbol", prompt)
            self.assertIn("absence is therefore not an inventory defect", prompt)
            self.assertIn("(source_id, role) pair once", prompt)
        self.assertEqual(POLICY_VERSION, "source_obligation_preparation/2")

    def test_invalid_inventory_gets_one_correction_and_fresh_review(self):
        bad = deepcopy(self.inventory)
        bad["requirements"].pop()
        generator = Mock(side_effect=[bad, self.inventory])
        reviewer = Mock(return_value=accepted_review(self.sources, self.inventory))
        report = prepare_inventory(self.sources, self.path, "test", generator, reviewer, correction_attempts=1)
        self.assertTrue(report["complete"])
        self.assertEqual(report["call_count"], 3)
        self.assertEqual((report["generation_call_count"], report["review_call_count"]), (2, 1))
        self.assertEqual(report["correction_attempts_used"], 1)
        self.assertEqual(report["attempts"][0]["failure"]["kind"], "invalid_inventory")
        self.assertEqual(report["attempts"][0]["status"], "correction_requested")
        self.assertEqual(report["attempts"][1]["status"], "passed")
        revised = json.loads(generator.call_args_list[1].args[1])
        self.assertEqual(revised["source_packet"], self.sources)
        self.assertEqual(revised["previous_inventory"], bad)
        self.assertNotIn("expected_status", revised)
        self.assertTrue((self.path / "obligation_inventory" / "correction_1_generation_call.json").exists())
        self.assertTrue((self.path / "obligation_inventory" / "correction_1_review_call.json").exists())

    def test_revision_review_causes_rewrite_and_separate_unanchored_review(self):
        generator = Mock(return_value=self.inventory)
        first = revision_review(self.sources, self.inventory)
        reviewer = Mock(side_effect=[first, accepted_review(self.sources, self.inventory)])
        report = prepare_inventory(self.sources, self.path, "test", generator, reviewer, correction_attempts=1)
        self.assertEqual(report["call_count"], 4)
        self.assertEqual(report["stop_reason"], "accepted")
        revised = json.loads(generator.call_args_list[1].args[1])
        self.assertEqual(revised["correction_feedback"]["review"]["requirements"][0]["disposition"], "revise")
        independent = json.loads(reviewer.call_args_list[1].args[1])
        self.assertNotIn("correction_feedback", independent)
        self.assertNotIn("previous_inventory", independent)
        self.assertNotIn("candidate_tlr", independent)
        self.assertEqual(independent["source_packet"], self.sources)
        self.assertEqual(report["attempts"][0]["response"], first)

    def test_genuine_clarification_is_withheld_without_inventing_an_answer(self):
        review = revision_review(self.sources, self.inventory)
        review["requirements"][0].update(disposition="needs_clarification", reason="Scope cannot be determined from the source.")
        review["requirements"][0]["dimensions"][0]["status"] = "unresolved"
        generator = Mock(return_value=self.inventory)
        reviewer = Mock(return_value=review)
        report = prepare_inventory(self.sources, self.path, "test", generator, reviewer, correction_attempts=1)
        self.assertEqual(report["status"], "withheld")
        self.assertEqual(report["call_count"], 2)
        self.assertEqual(report["correction_attempts_used"], 0)
        self.assertFalse(report["complete"])
        generator.assert_called_once()

    def test_malformed_review_is_not_treated_as_a_semantic_revision_decision(self):
        generator = Mock(return_value=self.inventory)
        reviewer = Mock(return_value="invalid JSON")
        report = prepare_inventory(self.sources, self.path, "test", generator, reviewer, correction_attempts=1)
        self.assertEqual(report["status"], "withheld")
        self.assertEqual(report["failure"]["kind"], "invalid_response")
        self.assertEqual(report["call_count"], 2)
        self.assertEqual(report["correction_attempts_used"], 0)

    def test_second_revision_request_exhausts_budget_without_third_generation(self):
        generator = Mock(return_value=self.inventory)
        reviewer = Mock(return_value=revision_review(self.sources, self.inventory))
        report = prepare_inventory(self.sources, self.path, "test", generator, reviewer, correction_attempts=1)
        self.assertEqual(report["status"], "withheld")
        self.assertEqual(report["call_count"], 4)
        self.assertEqual(generator.call_count, 2)
        self.assertEqual(reviewer.call_count, 2)
        self.assertEqual(report["correction_attempts_used"], 1)

    def test_explicit_inventory_is_not_silently_changed_even_with_correction_budget(self):
        generator = Mock(side_effect=AssertionError("Explicit inventory must not be rewritten"))
        reviewer = Mock(return_value=revision_review(self.sources, self.inventory))
        report = prepare_inventory(self.sources, self.path, "test", generator, reviewer,
                                   supplied=self.inventory, correction_attempts=1)
        self.assertEqual(report["status"], "withheld")
        self.assertEqual(report["call_count"], 1)
        self.assertEqual(report["correction_attempts_used"], 0)
        generator.assert_not_called()

    def test_transport_failure_does_not_consume_source_correction_opportunity(self):
        generator = Mock(side_effect=RuntimeError("Transport unavailable"))
        report = prepare_inventory(self.sources, self.path, "test", generator, None, correction_attempts=1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["call_count"], 1)
        self.assertEqual(report["failure"]["kind"], "transport_error")
        self.assertEqual(report["correction_attempts_used"], 0)

    def test_accepted_corrected_inventory_reuses_only_with_matching_policy_and_budget(self):
        generator = Mock(return_value=self.inventory)
        reviewer = Mock(side_effect=[revision_review(self.sources, self.inventory),
                                     accepted_review(self.sources, self.inventory)])
        first = prepare_inventory(self.sources, self.path / "first", "test", generator, reviewer, correction_attempts=1)
        second = prepare_inventory(self.sources, self.path / "second", "test", None, None,
                                   supplied=first["inventory"], previous=first, correction_attempts=1)
        self.assertTrue(second["complete"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["call_count"], 0)

    def test_correction_default_remains_zero_and_invalid_budget_is_rejected(self):
        generator = Mock(return_value={"schema": "mbse_obligation_inventory/1", "requirements": []})
        report = prepare_inventory(self.sources, self.path, "test", generator, None)
        self.assertEqual(report["call_count"], 1)
        self.assertEqual(report["correction_budget"], 0)
        for value in (True, -1, 2, 1.0, "1"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "0 or 1"):
                prepare_inventory(self.sources, self.path, "test", None, None, correction_attempts=value)


if __name__ == "__main__":
    unittest.main()
