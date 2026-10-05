"""CLI treatment isolation, failure retention and full compiler/solver execution."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import io
from contextlib import redirect_stdout
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from canonical_cli import (SYSML_INSTRUCTIONS, TLR_INSTRUCTIONS, read_json,
                           run_candidate, run_study, sources_from_file)
from canonical_tlr import render_sysml, validate_tlr
from review_sysml import compiler_capability

EXAMPLE = ROOT / "examples" / "canonical"


def fixtures():
    return (read_json(EXAMPLE / "requirements.json"), read_json(EXAMPLE / "tlr.json"),
            read_json(EXAMPLE / "context.json"))


def compiled(*args, **kwargs):
    return {"status": "passed", "diagnostics": []}


def audited(*args, **kwargs):
    return {"status": "passed", "admitted": True, "background_status": "sat", "consistency_status": "sat"}


from source_review_support import pass_source_review

@patch("canonical_cli._review_ask", new=pass_source_review)
class CanonicalCliTests(unittest.TestCase):
    def test_B_does_not_call_solver_and_uses_one_generation(self):
        sources, tlr, context = fixtures()
        generator = Mock(return_value=json.dumps(tlr))
        with tempfile.TemporaryDirectory() as tmp, patch("canonical_cli._compile", side_effect=compiled), \
                patch("canonical_audits.audit_tlr", side_effect=AssertionError("B must not audit")) as audit:
            result = run_candidate(sources, Path(tmp) / "B", "B", model="test-model", context=context, generator=generator)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["analysis"]["status"], "not_run")
        self.assertEqual(result["admission"], "not_assessed")
        generator.assert_called_once()
        audit.assert_not_called()
        self.assertEqual(generator.call_args.args[0], TLR_INSTRUCTIONS)

    def test_BC_uses_one_candidate_one_generation_and_one_audit(self):
        sources, tlr, context = fixtures()
        generator = Mock(return_value=json.dumps(tlr))
        with tempfile.TemporaryDirectory() as tmp, patch("canonical_cli._compile", side_effect=compiled), \
                patch("canonical_audits.audit_tlr", side_effect=audited) as audit:
            directory = Path(tmp) / "BC"
            result = run_candidate(sources, directory, "BC", model="test-model", context=context, generator=generator)
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(result["arm_views"]["B"]["model_file"], result["arm_views"]["C"]["model_file"])
            self.assertEqual(result["arm_views"]["B"]["tlr_file"], result["arm_views"]["C"]["tlr_file"])
            self.assertEqual(len(list(directory.glob("*.sysml"))), 1)
            self.assertEqual((directory / "model.sysml").read_text(), render_sysml(result["tlr"]))
            self.assertEqual(read_json(directory / "sources.json"), sources)
            self.assertEqual(result["admission"], "admitted_consistent_encoding")
        generator.assert_called_once()
        audit.assert_called_once()

    def test_A_direct_generation_does_not_require_TLR_or_call_solver(self):
        sources, _, context = fixtures()
        text = "package DirectCandidate { doc /* Supplied test fixture. */ }"
        generator = Mock(return_value=text)
        with tempfile.TemporaryDirectory() as tmp, patch("canonical_cli._compile", side_effect=compiled), \
                patch("canonical_tlr.validate_tlr", side_effect=AssertionError("A has no TLR")), \
                patch("canonical_audits.audit_tlr", side_effect=AssertionError("A must not audit")):
            directory = Path(tmp) / "A"
            result = run_candidate(sources, directory, "A", model="test-model", context=context, generator=generator)
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual((directory / "model.sysml").read_text(), text)
            self.assertIsNone(result["tlr"])
            self.assertFalse((directory / "tlr.json").exists())
        generator.assert_called_once()
        self.assertEqual(generator.call_args.args[0], SYSML_INSTRUCTIONS)

    def test_study_has_two_generation_calls_per_repetition_and_shared_context(self):
        sources, tlr, context = fixtures()
        seen = []
        def generate(system, prompt, model, directory, call_id):
            seen.append((system, json.loads(prompt), model))
            return "package DirectCandidate {}" if system == SYSML_INSTRUCTIONS else json.dumps(tlr)
        with tempfile.TemporaryDirectory() as tmp, patch("canonical_cli._compile", side_effect=compiled), \
                patch("canonical_audits.audit_tlr", side_effect=audited):
            directory = Path(tmp) / "study"
            result = run_study(sources, directory, repetitions=3, model="same-model", context=context, generator=generate)
            self.assertEqual(result["summary"]["unique_candidate_slots"], 6)
            self.assertEqual(result["summary"]["generated_candidates"], 6)
            self.assertEqual(result["summary"]["C_admitted"], 3)
            self.assertEqual(read_json(directory / "study_configuration.json")["planned_generation_calls"], 6)
            self.assertEqual(len(list(directory.rglob("model.sysml"))), 6)
        self.assertEqual(len(seen), 6)
        self.assertEqual([x[0] for x in seen], [SYSML_INSTRUCTIONS, TLR_INSTRUCTIONS, TLR_INSTRUCTIONS,
                                             SYSML_INSTRUCTIONS, SYSML_INSTRUCTIONS, TLR_INSTRUCTIONS])
        for _, prompt, model in seen:
            self.assertEqual(model, "same-model")
            self.assertEqual(prompt["fixed_context"], context)
            self.assertEqual(prompt["requirements"], sources)
            self.assertNotIn("condition", prompt)
            self.assertNotIn("arm", prompt)
        self.assertEqual(seen[0][1], seen[1][1])

    def test_invalid_generated_TLR_is_retained_without_fallback_or_repair(self):
        sources, tlr, _ = fixtures()
        invalid = deepcopy(tlr)
        invalid["requirements"][0]["formula"] = {"op": "*", "args": [{"var": "battery_voltage"}, {"var": "battery_voltage"}]}
        for response in ("{this is malformed JSON", json.dumps(invalid), '{"schema":"mbse_tlr/1","schema":"other"}'):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as tmp, \
                    patch("canonical_cli._compile", side_effect=AssertionError("No model to compile")), \
                    patch("canonical_audits.audit_tlr", side_effect=AssertionError("Invalid encoding cannot be audited")):
                directory = Path(tmp) / "failure"
                generator = Mock(return_value=response)
                result = run_candidate(sources, directory, "C", model="test-model", generator=generator)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["admission"], "withheld")
                self.assertTrue(result["errors"])
                self.assertEqual((directory / "generation_response.txt").read_text(), response)
                self.assertEqual((directory / "candidate_tlr.json").read_text(), response.strip())
                self.assertFalse((directory / "model.sysml").exists())
                self.assertEqual(result["configuration"]["semantic_repairs"], 0)
                generator.assert_called_once()

    def test_fixed_context_deviation_is_an_explicit_failure(self):
        sources, tlr, context = fixtures()
        tlr["variables"][0]["bounds"]["upper"] = "28"
        with tempfile.TemporaryDirectory() as tmp:
            result = run_candidate(sources, Path(tmp) / "deviation", "B", model="test-model", context=context, tlr=tlr, compile_model=False)
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("differs from the fixed study context", result["errors"][0])
        self.assertIsNone(result["tlr"])

    def test_unsupported_sources_remain_visible_and_not_admitted(self):
        sources = [{"id": "T1", "text": "The system shall eventually recover."}]
        tlr = {"schema": "mbse_tlr/1", "variables": [], "requirements": [
            {"id": "T1", "status": "unsupported", "reason": "Unbounded temporal obligation."}]}
        with tempfile.TemporaryDirectory() as tmp, patch("canonical_cli._compile", side_effect=compiled):
            directory = Path(tmp) / "unsupported"
            result = run_candidate(sources, directory, "C", model="test-model", tlr=tlr)
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(result["admission"], "withheld")
            self.assertEqual(result["analysis"]["status"], "unsupported")
            self.assertEqual(result["model_file"], "model.sysml")
            self.assertTrue((directory / "model.sysml").exists())
            self.assertNotIn("require constraint obligation", (directory / "model.sysml").read_text())
            self.assertEqual(read_json(directory / "sources.json"), sources)
            self.assertFalse(list((directory / "audit").glob("*.smt2")))

    def test_supplied_fixtures_do_not_call_provider_and_skip_compile_prevents_admission(self):
        sources, tlr, context = fixtures()
        with tempfile.TemporaryDirectory() as tmp, \
                patch("canonical_cli._ask", side_effect=AssertionError("Offline fixture must not invoke provider")), \
                patch("canonical_audits.audit_tlr", side_effect=audited):
            result = run_candidate(sources, Path(tmp) / "offline", "BC", model="test-model", context=context, tlr=tlr, compile_model=False)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["configuration"]["generation_attempts"], 0)
        self.assertEqual(result["configuration"]["generation_mode"], "supplied_artifact")
        self.assertEqual(result["compilation"]["status"], "not_run")
        self.assertEqual(result["admission"], "withheld")

    def test_existing_output_directory_is_not_overwritten(self):
        sources, tlr, _ = fixtures()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "existing"
            directory.mkdir()
            marker = directory / "original.txt"
            marker.write_text("keep this")
            with self.assertRaises(FileExistsError):
                run_candidate(sources, directory, "B", model="test-model", tlr=tlr, compile_model=False)
            self.assertEqual(marker.read_text(), "keep this")
            self.assertEqual(list(directory.iterdir()), [marker])

    def test_source_parser_drops_provenance_administration_and_keeps_source_identity(self):
        sources = sources_from_file(EXAMPLE / "requirements.json")
        original, _, _ = fixtures()
        self.assertEqual([r["id"] for r in sources], [r["id"] for r in original])
        self.assertEqual([r["text"] for r in sources], [r["text"] for r in original])
        self.assertTrue(all("provenance_status" not in r["source"] for r in sources))
        self.assertTrue(all(set(r) == {"id", "text", "source"} for r in sources))

    def test_model_content_does_not_announce_condition_or_generator(self):
        sources, tlr, _ = fixtures()
        text = render_sysml(validate_tlr(tlr, sources))
        for label in ("mbse_tlr", "TLR quantity", "Generated from", "Z3", "condition B", "condition C"):
            self.assertNotIn(label, text)

    @unittest.skipUnless(compiler_capability()["available"] and shutil.which("z3"), "Local SysML compiler and Z3 required")
    def test_full_default_CLI_BC_uses_real_solver_and_compiler_without_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "execution"
            from canonical_cli import main
            with redirect_stdout(io.StringIO()), patch("canonical_cli._ask", side_effect=AssertionError("Use explicit test reviewer only")):
                code = main(["run",
                    "--statement", str(EXAMPLE / "requirements.json"), "--tlr-file", str(EXAMPLE / "tlr.json"),
                    "--context-file", str(EXAMPLE / "context.json"), "--condition", "BC",
                    "--model", "offline-fixture", "--output-dir", str(output)])
            self.assertEqual(code, 0)
            result = read_json(output / "result.json")
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(result["compilation"]["status"], "passed", result["compilation"])
            self.assertEqual(result["analysis"]["background_status"], "sat")
            self.assertEqual(result["analysis"]["consistency_status"], "sat")
            self.assertEqual(result["admission"], "admitted_consistent_encoding")
            self.assertEqual(result["configuration"]["generation_attempts"], 0)
            self.assertNotIn("generation.json", [p.name for p in output.iterdir()])
            query = (output / "audit" / "consistency.smt2").read_text()
            self.assertNotIn("(assert false)", query)
            self.assertIn("(<= v_battery_voltage_0 28)", query)
            self.assertIn("(>= v_charging_current_0 0.5)", query)
            self.assertIn("<= 28", (output / "model.sysml").read_text())


if __name__ == "__main__":
    unittest.main()
