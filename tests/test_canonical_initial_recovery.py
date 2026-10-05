"""Budgeted initial draft recovery and paired-study accounting; no paid inference."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import canonical_cli as cli
from canonical_tlr import render_sysml, validate_tlr
from source_review_support import pass_source_review

EXAMPLE = ROOT / "examples" / "canonical"


def fixtures():
    return tuple(cli.read_json(EXAMPLE / name) for name in
                 ("requirements.json", "tlr.json", "context.json", "obligation_inventory.json"))


class InitialRecoveryTests(unittest.TestCase):
    def test_valid_draft_does_not_spend_format_budget(self):
        sources, tlr, context, _ = fixtures()
        generate = Mock(return_value=json.dumps(tlr))
        with tempfile.TemporaryDirectory() as tmp:
            result = cli.run_candidate(sources, Path(tmp) / "run", "B", model="offline",
                context=context, generator=generate,
                compile_model=False, format_repairs=1)
        self.assertEqual(result["status"], "completed", result["errors"])
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(result["configuration"]["format_repair_calls"], 0)
        self.assertEqual(result["configuration"]["model_transport_invocations"], 1)

    def test_malformed_draft_corrected_once_with_original_evidence(self):
        sources, tlr, context, _ = fixtures()
        malformed = '{"requirements":'
        generate = Mock(side_effect=[malformed, json.dumps(tlr)])
        review = Mock(side_effect=pass_source_review)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run"
            result = cli.run_candidate(sources, path, "B", model="offline", context=context,
                generator=generate, compile_model=False, format_repairs=1)
            self.assertEqual(result["status"], "completed", result["errors"])
            self.assertEqual((path / "generation_response.txt").read_text(), malformed)
            self.assertEqual((path / "format_attempts/000/response.txt").read_text(), malformed)
            self.assertEqual(json.loads((path / "format_attempts/001/response.txt").read_text()), tlr)
            self.assertEqual(cli.read_json(path / "format_recovery.json")["correction_calls"], 1)
        self.assertEqual(result["tlr"], validate_tlr(tlr, sources))
        self.assertEqual(result["configuration"]["generation_attempts"], 1)
        self.assertEqual(result["configuration"]["format_repair_calls"], 1)
        self.assertEqual(result["configuration"]["semantic_repairs"], 0)
        self.assertEqual(result["configuration"]["model_transport_invocations"], 2)
        review.assert_not_called()
        corrective = json.loads(generate.call_args_list[1].args[1])
        self.assertEqual(corrective["original_request"], json.loads(generate.call_args_list[0].args[1]))
        self.assertEqual(corrective["previous_response"], malformed)
        self.assertNotIn("solver_feedback", corrective)

    def test_correction_cannot_skip_source_or_fixed_context_guards(self):
        sources, tlr, context, _ = fixtures()
        variants = []
        dropped = deepcopy(tlr)
        dropped["requirements"].pop()
        variants.append(dropped)
        changed = deepcopy(tlr)
        changed["requirements"][0]["text"] = "Invented replacement source."
        variants.append(changed)
        changed_context = deepcopy(tlr)
        changed_context["variables"][0]["bounds"]["upper"] = "28"
        variants.append(changed_context)
        for invalid in variants:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                review = Mock(side_effect=AssertionError("Invalid candidate must not reach review"))
                generate = Mock(side_effect=["{}", json.dumps(invalid)])
                path = Path(tmp) / "run"
                result = cli.run_candidate(sources, path, "B", model="offline", context=context,
                    generator=generate, compile_model=False, format_repairs=1)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(generate.call_count, 2)
                review.assert_not_called()
                self.assertFalse((path / "model.sysml").exists())
                self.assertEqual(cli.read_json(path / "format_recovery.json")["stop_reason"], "budget_exhausted")

    def test_transport_error_and_supplied_draft_do_not_use_format_correction(self):
        sources, tlr, _, _ = fixtures()
        with tempfile.TemporaryDirectory() as tmp:
            generate = Mock(side_effect=TimeoutError("offline timeout"))
            failed = cli.run_candidate(sources, Path(tmp) / "transport", "B", model="offline",
                generator=generate, compile_model=False, format_repairs=1)
            self.assertEqual(generate.call_count, 1)
            self.assertEqual(failed["configuration"]["format_repair_calls"], 0)
            imported = cli.run_candidate(sources, Path(tmp) / "imported", "B", model="offline",
                tlr={}, generator=generate, compile_model=False, format_repairs=1)
            self.assertEqual(imported["status"], "failed")
            self.assertEqual(generate.call_count, 1)






if __name__ == "__main__":
    unittest.main()
