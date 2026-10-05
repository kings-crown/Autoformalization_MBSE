"""Frozen source-issue assessment, including partial malformed-response salvage."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from bedrock_judging import BedrockTransport
from canonical_issue_judging import (
    DIMENSIONS, MODEL_DIMENSIONS, assess_issue_candidate, judgment_packet, validate_judgment,
)


def sources():
    return [{"id": "R1", "text": "The voltage shall be at most 28 V.", "condition": "HIDDEN_CONDITION",
        "source": {"context": [{"id": "CTX1", "quote": "Voltage refers to the same battery."}],
                   "questions": [{"id": "QUESTION", "question": "HIDDEN_QUESTION", "text": "Not source authority"}]}}]


def diagnostics():
    return {"status": "passed", "consistency_status": "sat", "scope": "Complete source-reviewed requirement set",
            "findings": [], "condition": "HIDDEN_CONDITION", "expected_result": "HIDDEN_EXPECTED",
            "reference_formula": "HIDDEN_REFERENCE", "consistency": {"status": "sat", "artifacts": {"query": "HIDDEN_PATH"}}}


def config():
    return {"schema": "bedrock_judges/1", "judges": [
        {"model": "judge-one", "region": "us-east-1", "structured_output": True,
         "prices_per_million": {"input_tokens": 1, "output_tokens": 2}},
        {"model": "judge-two", "region": "us-east-1", "structured_output": True,
         "prices_per_million": {"input_tokens": 1, "output_tokens": 2}}]}


MODEL = "package Candidate { require constraint { voltage <= 28 } }"


def response(status="pass"):
    return {"dimensions": [{"id": dim, "status": status, "rationale": "Compared the source and relevant candidate evidence.",
        "source_basis": [{"source_id": "R1", "quote": "at most 28 V"}],
        "artifact_basis": [{"artifact": "sysml", "quote": "voltage <= 28"}] if dim in MODEL_DIMENSIONS else [
            {"artifact": "diagnostics", "quote": "Complete source-reviewed requirement set"}]} for dim in DIMENSIONS]}


class SourceIssueJudgingTests(unittest.TestCase):
    def test_prompt_whitelists_source_context_and_diagnostics_without_labels_references_or_paths(self):
        packet = judgment_packet(sources(), MODEL, diagnostics())
        prompt = json.dumps(packet)
        self.assertNotIn("HIDDEN", prompt)
        self.assertNotIn("Not source authority", prompt)
        self.assertEqual(packet["requirements"][0]["context_ids"], ["CTX1"])
        self.assertIn("Voltage refers to the same battery.", prompt)
        self.assertIn("consistency_status", prompt)

    def test_one_bad_source_citation_does_not_reject_other_dimensions(self):
        value = response()
        value["dimensions"][0]["source_basis"][0]["quote"] = "at least 28 V"
        result = validate_judgment(value, judgment_packet(sources(), MODEL, diagnostics()))
        self.assertEqual([d["status"] for d in result["dimensions"]], ["unreviewed", "pass", "pass", "pass", "pass"])
        self.assertIn("Invalid literal source", result["dimensions"][0]["errors"][0])
        self.assertEqual(result["dimensions"][0]["raw_judgment"], value["dimensions"][0])

    def test_shared_storage_preserves_explicit_scope_applicability_and_related_ids(self):
        rows = sources()
        rows[0]["source"]["context"] = {
            "scope": "Document context applies across the packet; highlights are nonexclusive.",
            "applicable_context_ids": ["CTX1"], "related_requirement_ids": ["R2"],
            "shared_document_context": {"scope": "Shared by all requirements.", "source_excerpts": [
                {"id": "CTX1", "quote": "The voltage concerns the same battery.", "role": "definition"},
                {"id": "CTX2", "quote": "The mass excludes packaging.", "role": "definition"}]},
            "expected_result": "HIDDEN_EXPECTED",
            "unresolved_interpretation_questions": [{"question": "HIDDEN_QUESTION"}]}
        rows.append({"id": "R2", "text": "The mass shall be at most 2 kg.", "source": {"context": {
            "scope": "Use the shared context in R1.", "applicable_context_ids": ["CTX2"],
            "related_requirement_ids": ["R1"]}}})
        packet = judgment_packet(rows, MODEL, diagnostics())
        self.assertEqual(packet["requirements"][0]["context_ids"], ["CTX1"])
        self.assertEqual(packet["requirements"][1]["context_ids"], ["CTX2"])
        metadata = packet["requirements"][0]["source"]["context"]
        self.assertEqual(metadata["related_requirement_ids"], ["R2"])
        self.assertEqual(metadata["shared_document_context"]["scope"], "Shared by all requirements.")
        self.assertEqual(metadata["shared_document_context"]["source_excerpts"][1]["role"], "definition")
        self.assertNotIn("HIDDEN", json.dumps(packet))
        del rows[0]["source"]["context"]["applicable_context_ids"]
        self.assertEqual(judgment_packet(rows, MODEL, diagnostics())["requirements"][0]["context_ids"], [])

    def test_not_run_audit_cannot_earn_diagnostic_pass_and_unresolved_is_preserved(self):
        packet = judgment_packet(sources(), MODEL, {"status": "not_run"})
        value = response()
        for row in value["dimensions"]:
            if row["id"] not in MODEL_DIMENSIONS:
                row["artifact_basis"] = [{"artifact": "diagnostics", "quote": "not_run"}]
        result = validate_judgment(value, packet)
        self.assertEqual([row["status"] for row in result["dimensions"]],
                         ["pass", "pass", "unreviewed", "unreviewed", "pass"])
        for row in value["dimensions"]:
            if row["id"] not in MODEL_DIMENSIONS:
                row["status"] = "unresolved"
        result = validate_judgment(value, packet)
        self.assertEqual([row["status"] for row in result["dimensions"]],
                         ["pass", "pass", "unresolved", "unresolved", "pass"])

    def test_duplicate_and_missing_dimensions_stay_in_planned_denominator(self):
        value = response()
        value["dimensions"].append(deepcopy(value["dimensions"][0]))
        del value["dimensions"][1]
        result = validate_judgment(value, judgment_packet(sources(), MODEL, diagnostics()))
        self.assertEqual(len(result["dimensions"]), 5)
        self.assertEqual(sum(row["status"] == "unreviewed" for row in result["dimensions"]), 2)

    def test_literal_artifact_evidence_is_validated_and_omission_failure_can_lack_it(self):
        value = response()
        value["dimensions"][0]["artifact_basis"][0]["quote"] = "voltage < 28"
        value["dimensions"][1].update(status="fail", artifact_basis=[])
        result = validate_judgment(value, judgment_packet(sources(), MODEL, diagnostics()))
        self.assertEqual(result["dimensions"][0]["status"], "unreviewed")
        self.assertEqual(result["dimensions"][1]["status"], "fail")

    def test_missing_model_cannot_be_scored_as_pass_but_diagnostic_review_survives(self):
        value = response()
        for row in value["dimensions"]:
            if row["id"] in MODEL_DIMENSIONS:
                row["artifact_basis"] = []
        result = validate_judgment(value, judgment_packet(sources(), None, diagnostics()))
        self.assertEqual([row["status"] for row in result["dimensions"]],
                         ["unreviewed", "unreviewed", "pass", "pass", "unreviewed"])

    def test_artifact_type_must_match_dimension_for_pass(self):
        value = response()
        value["dimensions"][0]["artifact_basis"] = [{"artifact": "diagnostics", "quote": "sat"}]
        result = validate_judgment(value, judgment_packet(sources(), MODEL, diagnostics()))
        self.assertEqual(result["dimensions"][0]["status"], "unreviewed")

    def test_two_independent_calls_preserve_disagreement_unknown_cost_and_exact_inputs(self):
        calls = []
        def callback(system, prompt, model, directory, call_id):
            calls.append((prompt, directory, call_id))
            result = response()
            if model == "judge-two":
                result["dimensions"][0]["status"] = "fail"
                result["dimensions"][1]["status"] = "unresolved"
            return json.dumps(result)
        with tempfile.TemporaryDirectory() as tmp:
            result = assess_issue_candidate(sources(), MODEL, diagnostics(), tmp,
                bedrock_config=config(), judge_callbacks=[callback, callback])
            self.assertEqual(json.loads((Path(tmp) / "input.json").read_text()), json.loads(calls[0][0]))
            self.assertEqual(result["summary"]["planned_judgments"], 10)
            self.assertEqual(result["summary"]["joint_pass"], 3)
            self.assertEqual(result["summary"]["disagreement"], 1)
            self.assertEqual(result["summary"]["unresolved"], 1)
            self.assertEqual(result["summary"]["joint_pass_rate"], 0.6)
            self.assertIsNone(result["execution"]["estimated_cost_usd"])
            self.assertEqual(result["execution"]["calls_with_unknown_cost"], 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertNotEqual(calls[0][1], calls[1][1])
        self.assertTrue(all(call[2] == "issue-review" for call in calls))

    def test_failed_call_does_not_discard_other_judge_and_remains_in_denominator(self):
        def fail(*args):
            raise RuntimeError("offline test error")
        with tempfile.TemporaryDirectory() as tmp:
            result = assess_issue_candidate(sources(), MODEL, diagnostics(), tmp, bedrock_config=config(),
                judge_callbacks=[fail, lambda *args: json.dumps(response())])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["summary"]["unreviewed"], 5)
        self.assertEqual(result["judges"][1]["status"], "completed")
        self.assertIn("offline test error", result["judges"][0]["error"])

    def test_non_json_response_is_retained_without_retry_or_fabricated_verdicts(self):
        callback = Mock(return_value="not json")
        with tempfile.TemporaryDirectory() as tmp:
            result = assess_issue_candidate(sources(), MODEL, diagnostics(), tmp, bedrock_config=config(),
                                            judge_callbacks=[callback, callback])
            self.assertEqual((Path(tmp) / "judge-1" / "response.txt").read_text(), "not json")
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(result["summary"]["unreviewed"], 5)

    def test_bedrock_transport_preserves_token_cost_and_does_not_apply_assertion_schema(self):
        payload = {"output": {"message": {"role": "assistant", "content": [{"text": json.dumps(response())}]}},
                   "stopReason": "end_turn", "usage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150}}
        client = Mock(converse=Mock(return_value=payload))
        transport = BedrockTransport(config(), client_factory=lambda _: client)
        with tempfile.TemporaryDirectory() as tmp, patch("canonical_issue_judging.BedrockTransport", return_value=transport):
            result = assess_issue_candidate(sources(), MODEL, diagnostics(), tmp, bedrock_config=config())
            self.assertEqual(result["execution"]["usage"]["total_tokens"], 300)
            self.assertAlmostEqual(result["execution"]["estimated_cost_usd"], 0.0004)
            self.assertEqual(result["execution"]["calls_with_unknown_cost"], 0)
            self.assertTrue((Path(tmp) / result["judges"][0]["transport_artifact"]).is_file())
        self.assertEqual(client.converse.call_count, 2)
        self.assertTrue(all("outputConfig" not in call.kwargs for call in client.converse.call_args_list))

    def test_invalid_configuration_and_nonempty_outputs_fail_before_calls(self):
        callback = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "existing.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "empty"):
                assess_issue_candidate(sources(), MODEL, diagnostics(), tmp, bedrock_config=config(),
                                       judge_callbacks=[callback, callback])
        callback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
