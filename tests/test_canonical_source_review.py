"""Source-to-rule gate invariants without model calls or solver feedback."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
from canonical_source_review import (review_candidate, review_feedback, validate_review,
                                     source_review_available, review_has_repair_findings)
from canonical_tlr import validate_tlr
from source_review_support import pass_source_review, review_response


def fixture():
    sources = [{"id": "R1", "text": "Battery voltage shall be at most 28 V.",
                "source": {"definition": "Voltage is the battery terminal voltage."}},
               {"id": "R2", "text": "Battery voltage shall be at least 20 V."}]
    tlr = {"schema": "mbse_tlr/1", "abstraction_policy": "mbse_abstraction/1",
           "variables": [{"name": "voltage", "type": "Real", "unit": "V",
                          "description": "Battery terminal voltage."}], "assumptions": [],
           "requirements": [{"id": row["id"], "status": "supported",
                             "formula": {"op": op, "args": [{"var": "voltage"}, {"value": value, "unit": "V"}]},
                             "abstraction": {"kind": "state_constraint", "meaning": row["text"],
                                             "scope": "Battery terminal voltage in a single observation.", "limitations": []}}
                            for row, op, value in zip(sources, ["<=", ">="], ["28", "20"])]}
    return sources, validate_tlr(tlr, sources)


def response_for(sources, tlr):
    return review_response({"source_packet": sources, "candidate_tlr": tlr})


class SourceReviewTests(unittest.TestCase):
    def test_complete_separate_review_records_exact_input_and_actual_expressions(self):
        sources, tlr = fixture()
        # Unrelated source-record metadata must not leak to the semantic reviewer.
        sources[0]["judge_verdict"] = "hidden reference answer"
        callback = Mock(side_effect=pass_source_review)
        context = {"variables": [], "background": []}
        with tempfile.TemporaryDirectory() as tmp:
            result = review_candidate(sources, tlr, context, tmp, "review-model", callback)
            payload = json.loads(callback.call_args.args[1])
            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["complete"])
            self.assertEqual(result["eligible_ids"], ["R1", "R2"])
            self.assertEqual(result["call_count"], 1)
            self.assertEqual(payload["candidate_tlr"], tlr)
            self.assertEqual(payload["fixed_context"], context)
            self.assertEqual(payload["readable_formulas"][0]["expression"], "(<= voltage 28 [V])")
            self.assertNotIn("hidden reference answer", callback.call_args.args[1])
            self.assertNotIn("solver_feedback", payload)
            self.assertNotIn("condition", payload)
            self.assertEqual(callback.call_args.args[2], "review-model")
            self.assertEqual(json.loads((Path(tmp) / "source_review/report.json").read_text()), result)
            self.assertEqual(json.loads((Path(tmp) / "source_review/input.json").read_text())["binding"], result["binding"])
            self.assertTrue((Path(tmp) / "source_review/response.txt").is_file())

    def test_boundary_defect_withholds_only_reviewed_defective_rule(self):
        sources, tlr = fixture()
        tlr["requirements"][0]["formula"]["op"] = "<"
        raw = response_for(sources, tlr)
        row = raw["requirements"][0]
        row["disposition"] = "revise"
        row["reason"] = "The source permits the 28 V endpoint but the strict inequality excludes it."
        row["distinguishing_case"] = "28 V is permitted by the source but prohibited by the rule."
        for dimension in row["dimensions"]:
            if dimension["dimension"] == "precision":
                dimension.update(status="fail", explanation=row["reason"])
        report = validate_review(raw, sources, tlr)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["eligible_ids"], ["R2"])
        self.assertEqual(report["requirements"][0]["disposition"], "revise")
        self.assertFalse(report["complete"])

    def test_bad_or_failed_shared_background_blocks_all_without_erasing_rows(self):
        sources, tlr = fixture()
        for malformed in (False, True):
            raw = response_for(sources, tlr)
            if malformed:
                raw["background"]["ast_paths"] = ["/nonexistent"]
            else:
                raw["background"]["disposition"] = "revise"
                raw["background"]["reason"] = "A generated domain restriction is unsupported by the source."
            result = validate_review(raw, sources, tlr)
            self.assertEqual(result["eligible_ids"], [])
            self.assertEqual([row["disposition"] for row in result["requirements"]], ["pass", "pass"])
            self.assertFalse(any(row["eligible"] for row in result["requirements"]))

    def test_partial_response_retains_valid_companions(self):
        sources, tlr = fixture()
        for corrupt in (lambda row: row.update(source_basis=[]),
                        lambda row: row.update(ast_paths=["/requirements/1/formula"]),
                        lambda row: row.update(dimensions=[]),
                        lambda row: row.update(reason=""),
                        lambda row: row.update(disposition="approved")):
            raw = response_for(sources, tlr)
            corrupt(raw["requirements"][0])
            result = validate_review(raw, sources, tlr)
            self.assertEqual(result["eligible_ids"], ["R2"])
            self.assertEqual(result["requirements"][0]["disposition"], "unreviewed")

    def test_missing_and_duplicate_rows_never_inherit_acceptance(self):
        sources, tlr = fixture()
        for records in ([response_for(sources, tlr)["requirements"][1]],
                        [response_for(sources, tlr)["requirements"][0]] * 2 + [response_for(sources, tlr)["requirements"][1]]):
            raw = response_for(sources, tlr)
            raw["requirements"] = records
            report = validate_review(raw, sources, tlr)
            self.assertEqual(report["eligible_ids"], ["R2"])
            self.assertIn("missing or duplicated", report["requirements"][0]["reason"])

    def test_fabricated_quotes_and_unresolvable_or_foreign_ast_paths_rejected(self):
        sources, tlr = fixture()
        bad_fields = [("source_basis", [{"source_id": "R1", "quote": "at most 30 V"}]),
                      ("source_basis", [{"source_id": "R99", "quote": sources[0]["text"]}]),
                      ("ast_paths", ["/requirements/0/formula/args/9"]),
                      ("ast_paths", ["/requirements/00/formula"]),
                      ("ast_paths", ["/requirements/0/formula/~3"]),
                      ("ast_paths", ["/requirements/0/abstraction"])]
        for key, value in bad_fields:
            raw = response_for(sources, tlr)
            raw["requirements"][0][key] = value
            self.assertEqual(validate_review(raw, sources, tlr)["eligible_ids"], ["R2"])

    def test_named_shared_context_supports_background_and_requirement_evidence(self):
        sources, tlr = fixture()
        excerpt = {"id": "DEF_VOLTAGE", "quote": "Voltage is the battery terminal voltage."}
        sources[0]["source"]["context"] = {"shared_document_context": {"excerpts": [excerpt]}}
        raw = response_for(sources, tlr)
        citation = {"source_id": excerpt["id"], "quote": "battery terminal voltage"}
        raw["background"]["source_basis"] = [citation]
        # Named shared context also resolves from a different requirement.
        raw["requirements"][1]["source_basis"].append(citation)
        report = validate_review(raw, sources, tlr)
        self.assertEqual(report["background"]["disposition"], "pass")
        self.assertEqual(report["eligible_ids"], ["R1", "R2"])

    def test_named_context_text_alias_and_identical_repetitions_are_valid(self):
        sources, tlr = fixture()
        quote = "Voltage is the battery terminal voltage."
        sources[0]["source"]["context"] = [{"id": "DEF_VOLTAGE", "quote": quote}]
        sources[1]["source"] = {"context": [{"id": "DEF_VOLTAGE", "text": quote}]}
        raw = response_for(sources, tlr)
        raw["background"]["source_basis"] = [{"source_id": "DEF_VOLTAGE", "quote": quote}]
        self.assertTrue(validate_review(raw, sources, tlr)["complete"])

    def test_named_context_requires_literal_quote_from_the_named_record(self):
        sources, tlr = fixture()
        sources[0]["source"]["context"] = [
            {"id": "DEF_VOLTAGE", "quote": "Voltage is the battery terminal voltage.",
             "description": "This description is not the named excerpt."},
            {"id": "OTHER", "quote": "Another source excerpt."}]
        for quote in ("Voltage is nominally 24 V.", "Another source excerpt.",
                      "This description is not the named excerpt."):
            with self.subTest(quote=quote):
                raw = response_for(sources, tlr)
                raw["background"]["source_basis"] = [{"source_id": "DEF_VOLTAGE", "quote": quote}]
                report = validate_review(raw, sources, tlr)
                self.assertEqual(report["eligible_ids"], [])
                self.assertIn("not a literal", report["background"]["reason"])

    def test_context_ids_must_exist_beneath_source_metadata(self):
        sources, tlr = fixture()
        quote = sources[0]["text"]
        sources[0]["unrelated_metadata"] = {"id": "OUTSIDE_SOURCE", "quote": quote}
        sources[0]["source"]["context"] = {"id": "WITHOUT_EXCERPT", "description": quote}
        for sid in ("INVENTED", "OUTSIDE_SOURCE", "WITHOUT_EXCERPT"):
            with self.subTest(sid=sid):
                raw = response_for(sources, tlr)
                raw["background"]["source_basis"] = [{"source_id": sid, "quote": quote}]
                report = validate_review(raw, sources, tlr)
                self.assertEqual(report["eligible_ids"], [])
                self.assertIn("unknown requirement or context ID", report["background"]["reason"])

    def test_conflicting_named_context_ids_are_ambiguous_in_any_order(self):
        sources, tlr = fixture()
        excerpts = [{"id": "DEF_VOLTAGE", "quote": "Voltage is measured at the battery terminals."},
                    {"id": "DEF_VOLTAGE", "quote": "Voltage is measured at the regulator output."}]
        for ordered in (excerpts, list(reversed(excerpts))):
            with self.subTest(ordered=ordered):
                sources[0]["source"]["context"] = ordered
                raw = response_for(sources, tlr)
                raw["background"]["source_basis"] = [{"source_id": "DEF_VOLTAGE", "quote": "Voltage is measured"}]
                report = validate_review(raw, sources, tlr)
                self.assertEqual(report["eligible_ids"], [])
                self.assertIn("ambiguous context ID", report["background"]["reason"])

    def test_context_id_collision_with_requirement_is_rejected_even_for_identical_text(self):
        sources, tlr = fixture()
        sources[0]["source"]["context"] = {"id": "R1", "quote": sources[0]["text"]}
        raw = response_for(sources, tlr)
        raw["background"]["source_basis"] = [{"source_id": "R2", "quote": sources[1]["text"]}]
        report = validate_review(raw, sources, tlr)
        self.assertEqual(report["eligible_ids"], ["R2"])
        self.assertIn("collides", report["requirements"][0]["reason"])

    def test_named_context_cannot_replace_own_requirement_quotation(self):
        sources, tlr = fixture()
        sources[0]["source"]["context"] = {"id": "DEF_VOLTAGE", "quote": sources[0]["text"]}
        raw = response_for(sources, tlr)
        raw["requirements"][0]["source_basis"] = [{"source_id": "DEF_VOLTAGE", "quote": sources[0]["text"]}]
        report = validate_review(raw, sources, tlr)
        self.assertEqual(report["eligible_ids"], ["R2"])
        self.assertIn("own requirement text", report["requirements"][0]["reason"])

    def test_pass_disposition_cannot_override_dimension_failure(self):
        sources, tlr = fixture()
        raw = response_for(sources, tlr)
        raw["requirements"][0]["dimensions"][0]["status"] = "fail"
        result = validate_review(raw, sources, tlr)
        self.assertEqual(result["eligible_ids"], ["R2"])
        self.assertIn("cannot receive pass", result["requirements"][0]["reason"])

    def test_abstention_disclosure_does_not_receive_executable_credit(self):
        sources, tlr = fixture()
        for row in tlr["requirements"]:
            row.pop("formula")
            row.pop("abstraction")
            row.update(status="unsupported", reason="A profile limitation is declared.", reason_code="profile_limit")
        raw = response_for(sources, tlr)
        result = validate_review(raw, sources, tlr)
        self.assertEqual(result["status"], "withheld")
        self.assertEqual(result["eligible_ids"], [])
        self.assertTrue(all(row["disposition"] == "unsupported" for row in result["requirements"]))
        raw["requirements"][0]["disposition"] = "pass"
        result = validate_review(raw, sources, tlr)
        self.assertEqual(result["requirements"][0]["disposition"], "unreviewed")

    def test_replacement_tlr_or_wrong_schema_fails_closed(self):
        sources, tlr = fixture()
        for key, value in (("tlr", tlr), ("schema", "generator_approval/1")):
            raw = response_for(sources, tlr)
            raw[key] = value
            result = validate_review(raw, sources, tlr)
            self.assertEqual(result["eligible_ids"], [])
            self.assertEqual(result["background"]["disposition"], "unreviewed")

    def test_transport_and_json_failures_are_recorded_without_implicit_retry(self):
        sources, tlr = fixture()
        callbacks = [Mock(side_effect=TimeoutError("provider timeout")), Mock(return_value="{broken JSON"),
                     Mock(return_value='{"schema":"source_rule_review/1","schema":"different"}'),
                     Mock(return_value='{"schema":"source_rule_review/1","requirements":NaN}')]
        for callback in callbacks:
            with tempfile.TemporaryDirectory() as tmp:
                result = review_candidate(sources, tlr, None, tmp, "review-model", callback)
                self.assertEqual(result["status"], "withheld")
                self.assertEqual(result["call_count"], 1)
                self.assertIsNotNone(result["failure"])
                callback.assert_called_once()
                self.assertTrue((Path(tmp) / "source_review/report.json").is_file())

    def test_review_reuse_is_exact_bound_and_revalidates_acceptance(self):
        sources, tlr = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            callback = Mock(side_effect=pass_source_review)
            original = review_candidate(sources, tlr, None, Path(tmp) / "first", "review-model", callback)
            original["eligible_ids"] = []
            original["complete"] = False
            reused = review_candidate(sources, tlr, None, Path(tmp) / "same", "review-model", callback, previous=original)
            self.assertTrue(reused["reused"])
            self.assertEqual(reused["call_count"], 0)
            self.assertEqual(reused["eligible_ids"], ["R1", "R2"])
            self.assertEqual(callback.call_count, 1)
            # A tampered stored response is revalidated, not copied as approval.
            original["response"]["requirements"][0]["ast_paths"] = ["/nonexistent"]
            refused = review_candidate(sources, tlr, None, Path(tmp) / "tampered", "review-model", callback, previous=original)
            self.assertEqual(refused["eligible_ids"], ["R2"])

    def test_old_bound_context_response_reuses_without_prompt_or_model_change(self):
        sources, tlr = fixture()
        quote = "Voltage is the battery terminal voltage."
        sources[0]["source"]["context"] = {"id": "DEF_VOLTAGE", "quote": quote}
        raw = response_for(sources, tlr)
        raw["background"]["source_basis"] = [{"source_id": "DEF_VOLTAGE", "quote": quote}]
        with tempfile.TemporaryDirectory() as tmp:
            original = review_candidate(sources, tlr, None, Path(tmp) / "old", "review-model",
                                        Mock(return_value=json.dumps(raw)))
            # Simulate the old validator's report while retaining its exact raw
            # response and binding. A validation bugfix requires no model retry.
            original.pop("validation_revision")
            original.update(eligible_ids=[], complete=False, status="withheld")
            original["background"] = {"disposition": "unreviewed"}
            callback = Mock(side_effect=AssertionError("No new reviewer call is needed"))
            report = review_candidate(sources, tlr, None, Path(tmp) / "new", "review-model", callback,
                                      previous=original)
            self.assertTrue(report["reused"])
            self.assertTrue(report["complete"])
            self.assertEqual(report["call_count"], 0)
            self.assertEqual(report["response"], raw)
            self.assertEqual(report["binding"], original["binding"])
            self.assertNotIn("validation_revision", report["binding"])
            callback.assert_not_called()

    def test_source_context_symbols_model_settings_and_policy_changes_invalidate_reuse(self):
        sources, tlr = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            original = review_candidate(sources, tlr, None, Path(tmp) / "original", "review-model", pass_source_review)
            cases = []
            other_sources = deepcopy(sources)
            other_sources[0]["source"]["definition"] = "Voltage includes a different observation context."
            cases.append((other_sources, tlr, None, "review-model", None))
            other_tlr = deepcopy(tlr)
            other_tlr["variables"][0]["description"] = "A revised symbol meaning."
            cases.extend([(sources, other_tlr, None, "review-model", None),
                          (sources, tlr, {"variables": [], "background": []}, "review-model", None),
                          (sources, tlr, None, "other-model", None),
                          (sources, tlr, None, "review-model", {"reasoning_effort": "high"})])
            for i, (src, candidate, context, model, config) in enumerate(cases):
                callback = Mock(side_effect=pass_source_review)
                result = review_candidate(src, candidate, context, Path(tmp) / str(i), model, callback,
                                          configuration=config, previous=original)
                self.assertFalse(result["reused"])
                callback.assert_called_once()
            stale = deepcopy(original)
            stale["binding"]["policy"] = "older-policy"
            result = review_candidate(sources, tlr, None, Path(tmp) / "policy", "review-model", pass_source_review, previous=stale)
            self.assertFalse(result["reused"])

    def test_effective_instructions_and_profile_invalidate_review_reuse(self):
        import canonical_source_review as module
        sources, tlr = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            original = review_candidate(sources, tlr, None, Path(tmp) / "original", "review-model", pass_source_review)
            # A version label alone cannot describe the effective composed prompt.
            for field in ("REVIEW_INSTRUCTIONS", "PROFILE"):
                callback = Mock(side_effect=pass_source_review)
                with self.subTest(field=field), patch.object(module, field, getattr(module, field) + "\nChanged interpretation policy."):
                    result = review_candidate(sources, tlr, None, Path(tmp) / field, "review-model", callback, previous=original)
                    self.assertFalse(result["reused"])
                    self.assertEqual(result["call_count"], 1)
                    callback.assert_called_once()
                    self.assertEqual(result["binding"]["reviewer_instructions"], callback.call_args.args[0])
                    self.assertEqual(result["binding"]["representation_profile"], json.loads(callback.call_args.args[1])["representation_profile"])
            legacy = deepcopy(original)
            legacy["binding"].pop("reviewer_instructions", None)
            legacy["binding"].pop("representation_profile", None)
            callback = Mock(side_effect=pass_source_review)
            result = review_candidate(sources, tlr, None, Path(tmp) / "legacy", "review-model", callback, previous=legacy)
            self.assertFalse(result["reused"])
            callback.assert_called_once()

    def test_environment_settings_participate_in_binding(self):
        sources, tlr = fixture()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODEX_REASONING_EFFORT": "low"}):
            old = review_candidate(sources, tlr, None, Path(tmp) / "old", "review-model", pass_source_review)
            with patch.dict(os.environ, {"CODEX_REASONING_EFFORT": "high"}):
                result = review_candidate(sources, tlr, None, Path(tmp) / "new", "review-model", pass_source_review, previous=old)
            self.assertFalse(result["reused"])

    def test_feedback_contains_findings_without_bindings_transport_or_external_metadata(self):
        sources, tlr = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            result = review_candidate(sources, tlr, None, tmp, "review-model", pass_source_review)
        result["judgments"] = "private final evaluation"
        result["requirements"][0]["solver_result"] = "sat"
        feedback = review_feedback(result)
        self.assertNotIn("binding", feedback)
        self.assertNotIn("response", feedback)
        self.assertNotIn("reviewer_model", feedback)
        self.assertNotIn("judgments", feedback)
        self.assertNotIn("solver_result", feedback["requirements"][0])
        self.assertEqual(feedback["eligible_ids"], ["R1", "R2"])

    def test_review_availability_does_not_confuse_missing_response_with_semantic_defect(self):
        sources, tlr = fixture()
        raw = response_for(sources, tlr)
        passed = validate_review(raw, sources, tlr)
        self.assertTrue(source_review_available(passed))
        self.assertFalse(review_has_repair_findings(passed))
        raw["requirements"][0]["source_basis"] = []
        incomplete = validate_review(raw, sources, tlr)
        self.assertFalse(source_review_available(incomplete))
        self.assertFalse(review_has_repair_findings(incomplete))
        # An invalid companion must not suppress a concrete valid repair finding.
        raw["requirements"][1]["disposition"] = "revise"
        actionable = validate_review(raw, sources, tlr)
        self.assertTrue(source_review_available(actionable))
        self.assertTrue(review_has_repair_findings(actionable))
        self.assertEqual(actionable["requirements"][0]["disposition"], "unreviewed")
        # Shared unknown premises still block all continuation.
        raw["background"]["source_basis"] = []
        shared_missing = validate_review(raw, sources, tlr)
        self.assertFalse(source_review_available(shared_missing))
        self.assertFalse(review_has_repair_findings(shared_missing))

    def test_valid_background_repair_finding_can_be_used_without_inventing_reviewed_rows(self):
        sources, tlr = fixture()
        raw = response_for(sources, tlr)
        raw["background"]["disposition"] = "revise"
        raw["requirements"] = []
        report = validate_review(raw, sources, tlr)
        self.assertTrue(source_review_available(report))
        self.assertTrue(review_has_repair_findings(report))
        self.assertEqual(report["eligible_ids"], [])
        raw["background"]["disposition"] = "needs_clarification"
        report = validate_review(raw, sources, tlr)
        self.assertFalse(review_has_repair_findings(report))
        self.assertFalse(source_review_available(report))
        self.assertFalse(source_review_available(None))
        self.assertFalse(review_has_repair_findings({"schema": "other"}))

    def test_malicious_previous_approval_fields_cannot_replace_revalidation_of_retained_response(self):
        sources, tlr = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            previous = review_candidate(sources, tlr, None, Path(tmp) / "original", "model", pass_source_review)
            previous["eligible_ids"] = ["R1", "R2", "INVENTED"]
            previous["complete"] = True
            previous["requirements"][0]["eligible"] = True
            previous["response"]["requirements"][0]["source_basis"] = [{"source_id": "R1", "quote": "an invented quotation"}]
            callback = Mock(side_effect=AssertionError("An unchanged retained response needs validation, not a model call"))
            report = review_candidate(sources, tlr, None, Path(tmp) / "reused", "model", callback, previous=previous)
            self.assertTrue(report["reused"])
            self.assertEqual(report["eligible_ids"], ["R2"])
            self.assertFalse(report["complete"])
            callback.assert_not_called()
            # Forged top-level identity never compensates for a changed binding.
            previous["binding"]["candidate_tlr"]["requirements"][0]["formula"]["op"] = "<"
            previous["reviewer_model"] = "model"
            callback = Mock(side_effect=pass_source_review)
            result = review_candidate(sources, tlr, None, Path(tmp) / "changed", "model", callback, previous=previous)
            self.assertFalse(result["reused"])
            callback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
