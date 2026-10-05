"""Offline replay of frozen judge evidence with no transport calls or overwrites."""
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from canonical_assertions import assertion_metrics, build_suite
from canonical_assertion_rescore import _reply, rescore_assessment
from canonical_cli import _strip_fence, argument_parser, main, read_json, write_json
import canonical_assertion_judging as judging


SOURCES = [{"id": "R1", "text": "Voltage shall be at most 28 V.",
            "source": {"context": {"scope": "Normal operation."}}}]
MODEL = ("package Battery {\n"
         "  doc /* Voltage shall be at most 28 V. */\n"
         "  attribute voltage : Real;\n"
         "  require constraint { voltage <= 28; }\n"
         "}\n")
MODELS = ["judge-one", "judge-two"]


def legacy_build_suite(*args, **kwargs):
    """Freeze existing assertion fixtures at v1; new v2 behavior has separate tests."""
    return build_suite(*args, **kwargs, schema="sysml_assertions/1")


def fixture_suite():
    return legacy_build_suite(SOURCES, [{"id": "R1", "assertions": [{
        "id": "R1-bound", "statement": "The upper bound is inclusive and 28 V.",
        "category": "boundary", "source_basis": [{"source_id": "R1", "quote": "at most 28 V"}]}]}])


def response(system, prompt, model, directory, call_id):
    p = json.loads(prompt)
    return json.dumps({"requirement_id": p["target_requirement_id"], "assertions": [
        {"id": assertion["id"], "status": "pass", "rationale": "The target constraint preserves this obligation.",
         "evidence": [{"start_line": 4, "end_line": 4}], "counterexample": None}
        for assertion in p["assertions"]]})


def fixture(root, packet_ids=("candidate",), calibration=False, empty=False):
    suite = fixture_suite()
    packets = [{"id": pid, "requirements": deepcopy(SOURCES), "fixed_context": None,
                "sysml": "" if empty else MODEL} for pid in packet_ids]
    if calibration:
        packets[0]["expected_assertions"] = {"R1-bound": "pass"}
    callback = Mock(side_effect=response)
    old = root / "old"
    report = judging.evaluate_packets(packets, suite, MODELS, old, [callback, callback])
    return old, report, callback


def call_path(old, pid="candidate", slot=1):
    return old / pid / "requirement-0001" / f"judge-{slot}"


def set_reply(call, value):
    text = json.dumps(value)
    (call / "response.txt").write_text(text)
    (call / "response.json").write_text(text)


def legacy_reject(old, report, packet_index=0, slot=1):
    """Emulate the old all-or-nothing validator on this saved raw reply."""
    r = report["results"][packet_index]["requirements"][0]
    r["judges"][slot - 1] = {"judge": slot, "model": MODELS[slot - 1], "status": "failed",
                            "error": "ValueError: Passed assertions need actual code evidence, not only documentation/comments",
                            "metrics": assertion_metrics(["unreviewed"] * 3)}
    report["results"][packet_index]["requirements"][0] = judging._requirement_result(fixture_suite()["requirements"][0], r["judges"])
    for control in report["calibration"]:
        if control["judge"] == slot:
            control["observed"] = "unreviewed"
    judging._summaries(report)
    write_json(old / "judgments.json", report)


def bytes_snapshot(directory):
    return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()}


def provider_reply(call, text, **overrides):
    """Record the same raw artifacts as a completed Bedrock judge callback."""
    (call / "response.txt").write_bytes(text.encode("utf-8"))
    (call / "response.json").write_bytes(_strip_fence(text).encode("utf-8"))
    result = call / "bedrock_calls" / "judge-1" / "judgment" / "result.json"
    result.parent.mkdir(parents=True, exist_ok=True)
    write_json(result, {"status": "ok", "stop_reason": "end_turn", "text": text, **overrides})
    return result


class AssertionRescoreTests(unittest.TestCase):
    def test_mixed_reply_retains_valid_verdicts_originals_and_cost_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, report, callback = fixture(root, calibration=True)
            call = call_path(old)
            raw = read_json(call / "response.json")
            raw["assertions"][1]["evidence"] = [{"start_line": 2, "end_line": 2}]
            set_reply(call, raw)
            legacy_reject(old, report)
            costs = call / "bedrock_calls"
            costs.mkdir()
            write_json(costs / "response.json", {"usage": {"inputTokens": 42}, "cost_usd": 0.001})
            frozen = bytes_snapshot(old)
            callback.reset_mock()
            with patch("bedrock_judging.BedrockTransport", side_effect=AssertionError("No inference allowed")):
                new = rescore_assessment(old, root / "new")
            callback.assert_not_called()
            self.assertEqual(bytes_snapshot(old), frozen)
            self.assertEqual(new["joint"]["planned"], 3)
            self.assertEqual(new["joint"]["pass"], 2)
            self.assertEqual(new["joint"]["unreviewed"], 1)
            self.assertEqual(new["summary"]["partial_calls"], 1)
            self.assertEqual(new["calibration_summary"][0]["faithful_acceptance_yield_planned"], 1)
            self.assertEqual(new["rescore"]["new_calls"], 0)
            self.assertEqual(new["rescore"]["added_cost_usd"], 0)
            self.assertEqual(new["rescore"]["original_provider_records"], [str(costs.resolve())])
            self.assertFalse(list((root / "new").rglob("bedrock_calls")))
            diagnostic = new["results"][0]["requirements"][0]["assertions"][1]["judge_errors"][0]
            self.assertIn("code evidence", diagnostic)
            self.assertIn("code evidence", (root / "new" / "assertions.csv").read_text())
            comparison = read_json(root / "new" / "comparison.json")
            self.assertEqual(comparison["old"]["joint"]["pass"], 0)
            self.assertEqual(comparison["new"]["joint"]["pass"], 2)
            self.assertEqual(comparison["changed_assertion_count"], 2)

    def test_distinct_and_legacy_shared_conditions_are_preserved(self):
        for legacy in (True, False):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                ids = ("A", "BC") if legacy else ("A", "B", "C")
                old, report, _ = fixture(root, ids)
                views = [{"condition": c, "repetition": 1, "packet_id": "BC" if legacy and c != "A" else c,
                          "artifact_arm": "BC" if legacy and c != "A" else c,
                          "admission": "admitted_consistent_encoding" if c == "C" else "not_assessed"}
                         for c in ("A", "B", "C")]
                judging._study_summaries(report, views)
                write_json(old / "judgments.json", report)
                new = rescore_assessment(old, root / "new")
                self.assertEqual(new["by_condition"], report["by_condition"])
                self.assertEqual(new["summary"]["planned_calls"], 4 if legacy else 6)
                self.assertEqual(len(new["observations"]), 3)
                self.assertEqual(new["by_condition"]["C"]["admitted"]["joint"]["pass"], 3)
                self.assertEqual(new["metrics"], report["metrics"])
                self.assertEqual(new["results"][0]["requirements"][0]["judges"][0]["assessment"],
                                 report["results"][0]["requirements"][0]["judges"][0]["assessment"])

    def test_malformed_or_missing_response_never_invents_a_verdict(self):
        for defect in ("json", "missing", "wrong_requirement", "mismatched_raw"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); old, _, _ = fixture(root)
                call = call_path(old)
                if defect == "missing":
                    (call / "response.json").unlink(); (call / "response.txt").unlink()
                elif defect == "json":
                    (call / "response.txt").write_text("not JSON")
                else:
                    raw = read_json(call / "response.json")
                    raw["requirement_id"] = "other-source"
                    if defect == "wrong_requirement":
                        set_reply(call, raw)
                    else:
                        write_json(call / "response.json", raw)
                new = rescore_assessment(old, root / "new")
                self.assertEqual(new["joint"]["planned"], 3)
                self.assertEqual(new["joint"]["unreviewed"], 3)
                self.assertEqual(new["summary"]["failed_calls"], 1)
                self.assertEqual(new["metrics"]["pass"], 3)

    def test_prompt_binding_checks_all_fixed_inputs(self):
        for field in ("source_packet", "target_requirement_id", "fixed_context", "assertions",
                      "sysml_with_line_numbers", "abstraction_policy"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); old, _, _ = fixture(root)
                prompt_path = call_path(old) / "prompt.json"
                prompt = read_json(prompt_path)
                prompt[field] = "different source or candidate"
                write_json(prompt_path, prompt)
                new = rescore_assessment(old, root / "new")
                self.assertEqual(new["joint"]["unreviewed"], 3)
                judgment = new["results"][0]["requirements"][0]["judges"][0]
                self.assertEqual(judgment["status"], "failed")
                self.assertIn("frozen", judgment["error"])

    def test_json_only_response_can_be_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _, _ = fixture(root)
            (call_path(old) / "response.txt").unlink()
            new = rescore_assessment(old, root / "new")
            self.assertEqual(new["joint"]["pass"], 3)

    def test_skipped_calls_are_not_resurrected_by_stray_responses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _, callback = fixture(root, empty=True)
            callback.assert_not_called()
            set_reply(call_path(old), {"requirement_id": "R1", "assertions": []})
            new = rescore_assessment(old, root / "new")
            self.assertEqual(new["summary"]["not_run_calls"], 2)
            self.assertEqual(new["joint"]["unreviewed"], 3)

    def test_frozen_source_mismatch_is_fatal_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _, _ = fixture(root)
            packets = read_json(old / "packets.json")
            packets[0]["requirements"][0]["source"]["context"]["scope"] = "Different domain"
            write_json(old / "packets.json", packets)
            with self.assertRaisesRegex(ValueError, "source packet differs"):
                rescore_assessment(old, root / "new")
            self.assertFalse((root / "new").exists())

    def test_missing_saved_assertion_is_fatal_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, report, _ = fixture(root)
            report["results"][0]["requirements"][0]["assertions"].pop()
            write_json(old / "judgments.json", report)
            with self.assertRaisesRegex(ValueError, "every planned assertion"):
                rescore_assessment(old, root / "new")
            self.assertFalse((root / "new").exists())

    def test_cli_replays_without_transport_and_propagates_status_and_errors(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); old, _, _ = fixture(root)
                if invalid:
                    raw = read_json(call_path(old) / "response.json")
                    raw["assertions"][1]["evidence"] = [{"start_line": 2, "end_line": 2}]
                    set_reply(call_path(old), raw)
                arguments = ["rescore-judgments", "--assessment-dir", str(old),
                             "--output-dir", str(root / "new")]
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("bedrock_judging.BedrockTransport", side_effect=AssertionError("No transport initialization")), \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(arguments)
                self.assertEqual(code, 1 if invalid else 0)
                self.assertEqual(json.loads(stdout.getvalue())["status"], "incomplete" if invalid else "completed")
                self.assertEqual(stderr.getvalue(), "")
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(arguments)
                self.assertEqual(code, 2)
                self.assertIn("Rescore output already exists", stderr.getvalue())

    def test_output_cannot_overwrite_or_overlap_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _, _ = fixture(root)
            for output in (old, old / "nested", root):
                with self.subTest(output=output), self.assertRaises(ValueError):
                    rescore_assessment(old, output)
            output = root / "exists"; output.mkdir()
            with self.assertRaises(FileExistsError):
                rescore_assessment(old, output)

    def test_opt_in_recovers_exact_verdicts_and_preserves_primary_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, original, callback = fixture(root)
            call = call_path(old)
            raw = read_json(call / "response.json")
            # Recovery must not select only favorable outcomes or bypass the
            # unchanged evidence validator for individual verdicts.
            raw["assertions"][0].update(status="fail", rationale="A known source mismatch.", counterexample="Voltage 29 V.")
            raw["assertions"][1]["evidence"] = [{"start_line": 2, "end_line": 2}]
            body = json.dumps(raw, indent=2)
            malformed = " \t\r\n{\r\n" + body + "\r\n"
            provider_reply(call, malformed)
            original["parse_recovery"]["counts"] = {
                "strict": 1, "recovered": 0, "rejected": 1, "not_attempted": 0}
            legacy_reject(old, original)
            frozen = bytes_snapshot(old)
            callback.reset_mock()
            with patch("bedrock_judging.BedrockTransport", side_effect=AssertionError("No inference allowed")):
                default = rescore_assessment(old, root / "default")
                recovered = rescore_assessment(old, root / "recovered", recover_leading_brace=True)
            callback.assert_not_called()
            self.assertEqual(bytes_snapshot(old), frozen)
            self.assertEqual(default["joint"]["unreviewed"], 3)
            self.assertEqual(default["rescore"]["parse_recovery"]["counts"]["recovered_responses"], 0)
            self.assertFalse(default["parse_recovery"]["enabled"])
            self.assertEqual(default["parse_recovery"]["policy"], "strict_json/1")
            self.assertEqual(default["parse_recovery"]["counts"], original["parse_recovery"]["counts"])
            actual = recovered["results"][0]["requirements"][0]["judges"][0]
            expected = judging.assess_response(raw, fixture_suite()["requirements"][0], MODEL, 1, MODELS[0])
            self.assertEqual(actual, expected)
            self.assertEqual(actual["status"], "partial")
            self.assertEqual(actual["metrics"]["fail"], 1)
            self.assertEqual(actual["metrics"]["unreviewed"], 1)
            self.assertEqual(recovered["joint"]["planned"], 3)
            self.assertEqual(recovered["joint"]["pass"], 1)
            self.assertEqual(recovered["joint"]["disputed"], 1)
            self.assertEqual(recovered["joint"]["unreviewed"], 1)
            new_call = call_path(root / "recovered")
            audit = read_json(new_call / "parse_recovery.json")
            self.assertEqual(audit["status"], "recovered")
            self.assertEqual(audit["assessment_status"], "partial")
            self.assertEqual(audit["removed_offset_in_original_text"], 4)
            self.assertEqual((new_call / "recovered_response.json").read_bytes(),
                             (malformed[:4] + malformed[5:]).encode())
            self.assertEqual(read_json(new_call / "recovered_response.json"), raw)
            self.assertEqual((new_call / "response.txt").read_bytes(), (call / "response.txt").read_bytes())
            self.assertEqual((new_call / "response.json").read_bytes(), (call / "response.json").read_bytes())
            counts = recovered["rescore"]["parse_recovery"]["counts"]
            self.assertEqual(counts, {"planned_responses": 2, "original_parse_passed": 1, "original_parse_failed": 1,
                                    "recovered_responses": 1, "strict_responses": 1, "rejected_responses": 0,
                                    "not_attempted_responses": 0})
            summary = recovered["parse_recovery"]
            self.assertTrue(summary["enabled"])
            self.assertEqual(summary["policy"], recovered["rescore"]["parse_recovery"]["policy"])
            self.assertEqual(summary["additional_calls"], 0)
            self.assertEqual(summary["counts"], {
                "strict": 1, "recovered": 1, "rejected": 0, "not_attempted": 0})
            self.assertEqual(sum(summary["counts"].values()), counts["planned_responses"])
            self.assertEqual(recovered["rescore"]["original_parse_recovery"], original["parse_recovery"])
            self.assertEqual(read_json(root / "recovered/judgments.json")["parse_recovery"], summary)
            self.assertEqual(read_json(root / "recovered/rescore.json")["original_parse_recovery"],
                             original["parse_recovery"])
            self.assertFalse(list((root / "recovered").rglob("bedrock_calls")))
            self.assertEqual(recovered["rescore"]["new_calls"], 0)
            self.assertEqual(recovered["rescore"]["added_cost_usd"], 0)
            self.assertIn("original assessment remains primary", read_json(root / "recovered/comparison.json")["interpretation"])

    def test_recovery_rejects_ambiguous_invalid_or_unbound_payloads(self):
        defects = ("truncated", "multiple_objects", "two_extra_braces", "extra_closing_brace", "prose", "fenced",
                   "non_json_whitespace", "duplicate", "nested_duplicate", "nonfinite", "overflow",
                   "wrong_target", "wrong_root", "assertions_not_list", "provider_failed", "provider_truncated",
                   "provider_mismatch", "provider_missing", "provider_multiple", "decoded_mismatch", "json_only")
        for defect in defects:
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); old, _, _ = fixture(root)
                call = call_path(old)
                body = (call / "response.txt").read_text()
                text = "{\n" + body
                if defect == "truncated": text = text[:-1]
                elif defect == "multiple_objects": text += body
                elif defect == "two_extra_braces": text = "{\n" + text
                elif defect == "extra_closing_brace": text += "}"
                elif defect == "prose": text = "Answer: " + text
                elif defect == "fenced": text = "```json\n" + text + "\n```"
                elif defect == "non_json_whitespace": text = "\u00a0" + text
                elif defect == "duplicate": text = text.replace('"requirement_id": "R1"', '"requirement_id": "R1", "requirement_id": "R1"')
                elif defect == "nested_duplicate": text = text.replace('"status": "pass"', '"status": "pass", "status": "fail"', 1)
                elif defect == "nonfinite": text = text.replace('"counterexample": null', '"counterexample": NaN', 1)
                elif defect == "overflow": text = text.replace('"start_line": 4', '"start_line": 1e999', 1)
                elif defect == "wrong_target": text = text.replace('"requirement_id": "R1"', '"requirement_id": "R2"')
                elif defect == "wrong_root": text = '{\n{"requirement_id":"R1","assertions":[],"extra":0}'
                elif defect == "assertions_not_list": text = '{\n{"requirement_id":"R1","assertions":{}}'
                result = provider_reply(call, text)
                provider = read_json(result)
                if defect == "provider_failed": provider["status"] = "api_error"
                elif defect == "provider_truncated": provider["stop_reason"] = "max_tokens"
                elif defect == "provider_mismatch": provider["text"] += " "
                write_json(result, provider)
                if defect == "provider_missing": result.unlink()
                elif defect == "provider_multiple":
                    other = call / "bedrock_calls/judge-2/judgment/result.json"
                    other.parent.mkdir(parents=True); write_json(other, provider)
                elif defect == "decoded_mismatch": (call / "response.json").write_text(body)
                elif defect == "json_only": (call / "response.txt").unlink()
                new = rescore_assessment(old, root / "new", recover_leading_brace=True)
                self.assertEqual(new["joint"]["unreviewed"], 3)
                self.assertEqual(new["summary"]["failed_calls"], 1)
                self.assertEqual(new["rescore"]["parse_recovery"]["counts"]["recovered_responses"], 0)
                self.assertFalse((call_path(root / "new") / "recovered_response.json").exists())

    def test_strict_first_rejects_duplicate_and_nonfinite_json_in_all_saved_forms(self):
        for form in ("both", "text_only", "json_only"):
            for defect in ("duplicate", "nested_duplicate", "NaN", "Infinity", "-Infinity", "1e999"):
                with self.subTest(form=form, defect=defect), tempfile.TemporaryDirectory() as tmp:
                    call = Path(tmp)
                    text = ('{"requirement_id":"R1","requirement_id":"R1","assertions":[]}' if defect == "duplicate" else
                            '{"requirement_id":"R1","assertions":[{"id":"x","id":"x"}]}' if defect == "nested_duplicate" else
                            '{"requirement_id":"R1","assertions":[' + defect + ']}')
                    if form != "json_only": (call / "response.txt").write_text(text)
                    if form != "text_only": (call / "response.json").write_text(text)
                    with self.assertRaises(ValueError):
                        _reply(call, recover_leading_brace=True, requirement_id="R1")

    def test_cli_recovery_remains_explicit_for_offline_and_live_assessment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _, _ = fixture(root)
            call = call_path(old)
            provider_reply(call, "{\n" + (call / "response.txt").read_text())
            args = ["rescore-judgments", "--assessment-dir", str(old), "--output-dir", str(root / "new"), "--recover-leading-brace"]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(args), 0)
            self.assertEqual(read_json(root / "new/rescore.json")["parse_recovery"]["counts"]["recovered_responses"], 1)
            live = ["judge", "--study-dir", "unused", "--judge-config", "unused.json", "--output-dir", "unused"]
            self.assertFalse(argument_parser().parse_args(live).recover_leading_brace)
            self.assertTrue(argument_parser().parse_args(live + ["--recover-leading-brace"]).recover_leading_brace)


if __name__ == "__main__":
    unittest.main()
