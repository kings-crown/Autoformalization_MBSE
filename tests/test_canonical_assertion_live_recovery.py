"""Up-front receipt-bound parse recovery never recalls judges or edits verdicts."""
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
from canonical_assertion_judging import evaluate_packets, evaluate_study
from canonical_cli import main, read_json, write_json
from test_canonical_assertion_judging import MODELS, callback, config, packet, study, suite, verdict


def malformed_callback(states=None, receipt=True, stop_reason="end_turn", mismatch=False):
    def generate(system, prompt, model, directory, call_id):
        raw = " \r\n{\n" + json.dumps(verdict(prompt, states)) + "\r\n "
        if receipt:
            path = directory / "bedrock_calls" / "offline-receipt" / "judgment" / "result.json"
            path.parent.mkdir(parents=True)
            write_json(path, {"status": "ok", "stop_reason": stop_reason,
                              "text": "different provider text" if mismatch else raw})
        return raw
    return Mock(side_effect=generate)


class LiveAssertionRecoveryTests(unittest.TestCase):
    def test_opt_in_recovers_one_character_preserving_raw_and_negative_verdicts(self):
        fn = malformed_callback(["fail", "unresolved", "fail"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "judging"
            report = evaluate_packets([packet()], suite(), MODELS, path, [fn, fn],
                                      workers=2, recover_leading_brace=True)
            self.assertEqual(fn.call_count, 2)
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["parse_recovery"]["counts"]["recovered"], 2)
            self.assertEqual(report["parse_recovery"]["additional_calls"], 0)
            self.assertEqual(report["joint"]["fail"], 2)
            self.assertEqual(report["joint"]["unresolved"], 1)
            self.assertEqual(report["joint"]["pass"], 0)
            for slot in (1, 2):
                call = path / "candidate/requirement-0001" / f"judge-{slot}"
                original = (call / "response.txt").read_bytes()
                receipt = read_json(call / "bedrock_calls/offline-receipt/judgment/result.json")
                self.assertEqual(original, receipt["text"].encode())
                self.assertTrue(original.startswith(b" \r\n{\n{"))
                self.assertTrue((call / "response.json").read_text().startswith("{\n{"))
                audit = read_json(call / "parse_recovery.json")
                self.assertEqual(audit["status"], "recovered")
                recovered = (call / "recovered_response.json").read_bytes()
                offset = audit["removed_offset_in_original_text"]
                self.assertEqual(recovered, original[:offset] + original[offset + 1:])
            self.assertTrue(read_json(path / "configuration.json")["parse_recovery"]["enabled"])

    def test_default_remains_strict_and_does_not_retry(self):
        fn = malformed_callback()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "judging"
            report = evaluate_packets([packet()], suite(), MODELS, path, [fn, fn])
            self.assertEqual(fn.call_count, 2)
            self.assertEqual(report["status"], "incomplete")
            self.assertEqual(report["summary"]["failed_calls"], 2)
            self.assertFalse(list(path.rglob("recovered_response.json")))
            self.assertFalse(report["parse_recovery"]["enabled"])

    def test_unsafe_receipts_rejected_without_extra_calls(self):
        for kwargs in ({"receipt": False}, {"stop_reason": "max_tokens"}, {"mismatch": True}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as tmp:
                fn = malformed_callback(**kwargs)
                path = Path(tmp) / "judging"
                report = evaluate_packets([packet()], suite(), MODELS, path, [fn, fn], recover_leading_brace=True)
                self.assertEqual(fn.call_count, 2)
                self.assertEqual(report["summary"]["failed_calls"], 2)
                self.assertEqual(report["parse_recovery"]["counts"]["rejected"], 2)
                self.assertFalse(list(path.rglob("recovered_response.json")))

    def test_strict_valid_responses_need_no_receipt_with_opt_in(self):
        fn = callback()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "judging"
            report = evaluate_packets([packet()], suite(), MODELS, path, [fn, fn], recover_leading_brace=True)
            self.assertEqual(fn.call_count, 2)
            self.assertEqual(report["parse_recovery"]["counts"]["strict"], 2)
            self.assertFalse(list(path.rglob("recovered_response.json")))
            self.assertEqual(report["joint"]["pass"], 3)

    def test_study_threads_policy_to_each_actual_candidate(self):
        fn = malformed_callback()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = study(root / "study")
            report = evaluate_study(directory, suite(), MODELS, root / "judging", [fn, fn],
                                    recover_leading_brace=True)
        self.assertEqual(fn.call_count, 4)  # A and shared B/C, two judges each.
        self.assertEqual(report["parse_recovery"]["counts"]["recovered"], 4)
        self.assertEqual(report["by_condition"]["C"]["joint"]["pass"], 3)

    def test_cli_threads_policy_and_rejects_legacy_mode(self):
        for route, evaluator in (("packets", "evaluate_packets"), ("study", "evaluate_study")):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_json(root / "config.json", config())
                write_json(root / "suite.json", suite())
                write_json(root / "packets.json", [packet()])
                transport = Mock(config=config(), judge_configs=config()["judges"])
                transport.for_judge.return_value = callback()
                with patch("bedrock_judging.BedrockTransport", return_value=transport), \
                        patch("canonical_assertion_judging." + evaluator, return_value={"status": "completed"}) as evaluate, \
                        redirect_stdout(io.StringIO()):
                    args = ["judge", "--judge-config", str(root / "config.json"),
                            "--assertions-file", str(root / "suite.json"), "--output-dir", str(root / "output"),
                            "--recover-leading-brace"]
                    args += ["--packets-file", str(root / "packets.json")] if route == "packets" else ["--study-dir", str(root / "study")]
                    self.assertEqual(main(args), 0)
                self.assertTrue(evaluate.call_args.kwargs["recover_leading_brace"])
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["judge", "--judge-model", "one", "--judge-model", "two",
                "--packets-file", "unused.json", "--output-dir", "unused", "--recover-leading-brace"]), 2)


if __name__ == "__main__":
    unittest.main()
