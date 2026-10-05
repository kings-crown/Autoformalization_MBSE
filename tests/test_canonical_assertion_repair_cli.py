"""CLI recovery plans are offline; execution retains the saved judge settings."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from canonical_cli import main


class JudgmentRepairCliTests(unittest.TestCase):
    def test_dry_run_never_constructs_provider_or_executes_repair(self):
        with patch("canonical_assertion_repair.prepare_repair_plan", return_value={"affected_cells": 3}) as plan, \
                patch("canonical_assertion_repair.repair_assessment", side_effect=AssertionError("dry run")), \
                patch("bedrock_judging.BedrockTransport", side_effect=AssertionError("no provider")), \
                redirect_stdout(io.StringIO()) as output:
            status = main(["repair-judgments", "--assessment-dir", "saved", "--dry-run"])
        self.assertEqual(status, 0)
        plan.assert_called_once_with(Path("saved"), continue_budget=False)
        self.assertTrue(json.loads(output.getvalue())["dry_run"])

    def test_missing_output_is_rejected_before_provider_construction(self):
        with patch("bedrock_judging.BedrockTransport", side_effect=AssertionError("no provider")), \
                redirect_stderr(io.StringIO()) as errors:
            status = main(["repair-judgments", "--assessment-dir", "saved"])
        self.assertEqual(status, 2)
        self.assertIn("--output-dir", errors.getvalue())

    def test_execution_uses_saved_configuration_and_keeps_incomplete_status(self):
        frozen = {"schema": "bedrock_judges/1", "judges": [{"model": "first"}, {"model": "second"}]}
        transport = Mock(config=frozen)
        callbacks = [Mock(), Mock()]
        transport.for_judge.side_effect = callbacks
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "saved"
            source.mkdir()
            (source / "configuration.json").write_text(json.dumps({"provider": frozen}))
            target = Path(temp) / "new"
            with patch("bedrock_judging.BedrockTransport", return_value=transport) as factory, \
                    patch("canonical_assertion_repair.repair_assessment", return_value={"status": "incomplete"}) as repair, \
                    redirect_stdout(io.StringIO()):
                status = main(["repair-judgments", "--assessment-dir", str(source),
                               "--output-dir", str(target), "--max-attempts", "1", "--judge-workers", "1"])
        self.assertEqual(status, 1)
        factory.assert_called_once_with(frozen)
        repair.assert_called_once_with(source, target, callbacks, frozen, max_attempts=1, workers=1,
                                       continue_budget=False)

    def test_continuation_is_explicit_even_for_offline_plan(self):
        with patch("canonical_assertion_repair.prepare_repair_plan", return_value={}) as plan, \
                patch("bedrock_judging.BedrockTransport", side_effect=AssertionError("no provider")), \
                redirect_stdout(io.StringIO()):
            status = main(["repair-judgments", "--assessment-dir", "saved", "--dry-run", "--continue-budget"])
        self.assertEqual(status, 0)
        plan.assert_called_once_with(Path("saved"), continue_budget=True)

    def test_missing_provider_config_cannot_silently_select_new_judges(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "saved"
            source.mkdir()
            (source / "configuration.json").write_text("{}")
            with patch("bedrock_judging.BedrockTransport", side_effect=AssertionError("no fallback")), \
                    redirect_stderr(io.StringIO()):
                status = main(["repair-judgments", "--assessment-dir", str(source),
                               "--output-dir", str(Path(temp) / "new")])
        self.assertEqual(status, 2)


if __name__ == "__main__":
    unittest.main()
