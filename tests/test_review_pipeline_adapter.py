"""Conservative adapter boundaries; no LLM or network calls required."""
import hashlib
import json
import os
import runpy
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import review_pipeline_adapter as adapter


class TraceCorrectionTests(unittest.TestCase):
    def test_local_definition_typing_preserves_original_bytes_and_audit(self):
        original = (
            "package Trace {\r\n"
            "part def Row;\r\n"
            "individual part def trace_R1 :> Row;\r\n"
            "part trace_R1_entry :> trace_R1;\r\n"
            "}\r\n"
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.sysml"
            path.write_bytes(original)
            changes = adapter._correct_trace_typing(path)
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["line"], 4)
            self.assertEqual(changes[0]["before"], "part trace_R1_entry :> trace_R1;")
            self.assertEqual(changes[0]["after"], "part trace_R1_entry : trace_R1;")
            self.assertEqual(path.with_suffix(".original.txt").read_bytes(), original)
            self.assertEqual(path.read_bytes(), original.replace(b"entry :>", b"entry :"))
            record = json.loads((path.parent / "corrections.json").read_text())
            self.assertEqual(record["original_sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(record["corrected_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            audit_before = (path.parent / "corrections.json").read_bytes()
            self.assertEqual(adapter._correct_trace_typing(path), [])
            self.assertEqual((path.parent / "corrections.json").read_bytes(), audit_before)

    def test_definitions_subsetting_unknown_targets_and_complex_forms_are_untouched(self):
        original = '''package Trace {
part def Base;
part def Known :> Base;
part existing : Known;
part subset :> existing;
part unknown :> Missing;
part complex :> Known, Base;
part qualified :> Other::Known;
part nested :> Known { attribute value; }
/* part fake :> Known; */
/* part def Pretend; */
part pretend :> Pretend;
attribute text = "part def Bogus;";
part bogus :> Bogus;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.sysml"
            path.write_text(original)
            self.assertEqual(adapter._correct_trace_typing(path), [])
            self.assertEqual(path.read_text(), original)
            self.assertFalse(path.with_suffix(".original.txt").exists())

    def test_operator_in_a_comment_is_not_the_replacement_target(self):
        original = "part def Known;\n/* :> preserve */ part fix :> Known; // :> preserve\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.sysml"
            path.write_text(original)
            self.assertEqual(len(adapter._correct_trace_typing(path)), 1)
            self.assertIn("/* :> preserve */ part fix : Known; // :> preserve", path.read_text())

    def test_duplicate_local_definition_names_are_not_guessed(self):
        original = "part def Shared;\npart def Shared;\npart item :> Shared;\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.sysml"
            path.write_text(original)
            self.assertEqual(adapter._correct_trace_typing(path), [])
            self.assertEqual(path.read_text(), original)


class ProposalOptInTests(unittest.TestCase):
    def test_adapter_only_requests_proposals_when_explicitly_selected(self):
        observed = []
        def child(command, **kwargs):
            observed.append(kwargs['env']['MBSE_REVIEW_PROPOSE_BEHAVIOR'])
            return Mock(poll=Mock(return_value=0), returncode=0)
        with tempfile.TemporaryDirectory() as directory, patch.object(adapter.subprocess, 'Popen', side_effect=child), patch.object(adapter, '_selected_model', return_value=('fixture-model', 'test')):
            default_run, proposal_run = Path(directory) / 'default', Path(directory) / 'proposal'
            default_run.mkdir()
            proposal_run.mkdir()
            adapter.run_existing_pipeline(default_run, 'Requirements default', [{'id': 'R1', 'text': 'battery.voltage <= 28 V'}])
            adapter.run_existing_pipeline(proposal_run, 'Explicit proposal', [{'id': 'R1', 'text': 'battery.voltage <= 28 V'}], propose_behavior=True)
        self.assertEqual(observed, ['0', '1'])

    def test_child_entry_requires_both_explicit_mode_and_proposal_flag(self):
        script = Path(adapter.__file__).with_name('review_llm_entry.py')
        requirements = [{'id': 'R1', 'text': 'battery.voltage <= 28 V'}]
        for flag, mode, expected in [('1', 'requirements', 0), ('0', 'propose_design', 0), ('1', 'propose_design', 1)]:
            with self.subTest(flag=flag, mode=mode), tempfile.TemporaryDirectory() as directory:
                (Path(directory) / 'pipeline_source_requirements.json').write_text(json.dumps(requirements))
                environment = {'MBSE_REVIEW_CAPTURE_DIR': directory, 'CODEX_MBSE_MODEL': 'fixture-model',
                               'MBSE_REVIEW_PROPOSE_BEHAVIOR': flag, 'MBSE_REVIEW_ANALYSIS_MODE': mode}
                with patch.dict(os.environ, environment), patch.object(adapter.legacy, 'legacy_main'), patch.object(adapter.legacy, '_codex_chat_text', AsyncMock()), patch.object(adapter.legacy, '_build_tlf_payload', Mock()), patch('review_behavior_proposal.propose_behavior', AsyncMock()) as proposer:
                    runpy.run_path(str(script), run_name='__main__')
                    self.assertEqual(proposer.await_count, expected)
                    if expected:
                        proposer.assert_awaited_once_with(requirements, Path(directory), 'fixture-model')


class LargeUploadTests(unittest.TestCase):
    def test_child_smt_prompt_receives_source_beyond_legacy_prefix(self):
        requirements = [{'id': f'R{i:03d}', 'text': f'The subsystem {i} shall preserve this distinct source clause ' + 'with supporting context ' * 8}
                        for i in range(240)]
        observed = {}
        def child(command, **kwargs):
            source = Path(command[command.index('--statement') + 1]).read_text()
            limit = int(kwargs['env']['MBSE_STATEMENT_PROMPT_MAX_CHARS'])
            observed.update(source=source, retained=adapter.legacy._clip_text(source, limit))
            return Mock(poll=Mock(return_value=0), returncode=0)
        with tempfile.TemporaryDirectory() as directory, patch.object(adapter.subprocess, 'Popen', side_effect=child), patch.object(adapter, '_selected_model', return_value=('fixture-model', 'test')):
            adapter.run_existing_pipeline(Path(directory), 'Large fixture', requirements, propose_behavior=False)
            coverage = json.loads((Path(directory) / 'prompt_coverage.json').read_text())
        self.assertGreater(len(observed['source']), 12000)
        self.assertEqual(observed['retained'], observed['source'].strip())
        self.assertIn('R239', observed['retained'])
        self.assertTrue(requirements[-1]['text'].strip() in observed['retained'], 'The final source clause was truncated.')
        self.assertEqual(coverage['requirements'], 240)
        self.assertFalse(coverage['source_truncated'])


class SemanticGateDiagnosticsTests(unittest.TestCase):
    def test_symbol_drift_is_distinct_from_blocking_semantic_checks(self):
        semantic = {"passed": True, "checks": {"symbol_drift": {"passed": False, "unknown_symbols": ["introduced"]}}}
        self.assertIsNone(adapter._semantic_gate_summary(semantic))
        semantic.update(passed=False)
        semantic["checks"]["vacuity"] = {"passed": False, "solver_errors": [{"diagnostics": "unknown constant helper"}] * 9}
        summary = adapter._semantic_gate_summary(semantic)
        self.assertIn("vacuity checks (9 solver probe errors)", summary)
        self.assertNotIn("symbol", summary)

    def test_skipped_trigger_checks_remain_visible_as_partial_analysis(self):
        progress = []
        semantic = {"passed": True, "checks": {"vacuity": {"passed": True, "skipped_administrative_guards": [{"requirement_id": "R1"}]}}}
        def child(command, **kwargs):
            directory = Path(kwargs["env"]["MBSE_REVIEW_CAPTURE_DIR"])
            (directory / "pipeline_model_semantic_checks.json").write_text(json.dumps(semantic))
            (directory / "pipeline_model_sat.smt2").write_text("(assert (! true :named req_R1))\n(check-sat)\n")
            Path(command[command.index("--sysml-output") + 1]).write_text("package Fixture {}")
            return Mock(poll=Mock(return_value=0), returncode=0)
        with tempfile.TemporaryDirectory() as directory, patch.object(adapter.subprocess, "Popen", side_effect=child), patch.object(adapter, "_selected_model", return_value=("fixture-model", "test")), patch.object(adapter, "_recheck", return_value={"status": "ok", "result": "sat"}):
            result = adapter.run_existing_pipeline(Path(directory), "Partial trigger coverage", [{"id": "R1", "text": "Source clause"}], lambda *args: progress.append(args), propose_behavior=False)
        self.assertEqual(result["analysis"]["solver_status"], "sat")
        self.assertIsNone(result["analysis"]["generation_blocker"])
        self.assertIn("remains unchecked", result["analysis"]["semantic_limitations"][0])
        self.assertIsNotNone(result["model"])
        self.assertTrue(any(stage == "analysis" and status == "partial" for stage, status, _ in progress))
        self.assertTrue(any(stage == "generation" and status == "complete" for stage, status, _ in progress))

    def test_sat_result_does_not_hide_failed_generation_gate(self):
        progress = []
        semantic = {"passed": False, "checks": {"vacuity": {"passed": False, "solver_errors": [{"diagnostics": "unknown constant helper"}]}}}
        def child(command, **kwargs):
            directory = Path(kwargs["env"]["MBSE_REVIEW_CAPTURE_DIR"])
            (directory / "pipeline_model_semantic_checks.json").write_text(json.dumps(semantic))
            (directory / "pipeline_model_sat.smt2").write_text("(assert (! true :named req_R1))\n(check-sat)\n")
            return Mock(poll=Mock(return_value=1), returncode=1)
        with tempfile.TemporaryDirectory() as directory, patch.object(adapter.subprocess, "Popen", side_effect=child), patch.object(adapter, "_selected_model", return_value=("fixture-model", "test")), patch.object(adapter, "_recheck", return_value={"status": "ok", "result": "sat"}):
            result = adapter.run_existing_pipeline(Path(directory), "Failed gate", [{"id": "R1", "text": "Source clause"}], lambda *args: progress.append(args), propose_behavior=False)
        self.assertEqual(result["analysis"]["solver_status"], "sat")
        self.assertIn("vacuity", result["analysis"]["generation_blocker"])
        self.assertTrue(result["analysis"]["approval_blocked"])
        self.assertIsNone(result["model"])
        self.assertTrue(any("SysML generation blocked" in error for error in result["errors"]))
        for stage in ("analysis", "generation"):
            status, detail = [(status, detail) for key, status, detail in progress if key == stage][-1]
            self.assertEqual(status, "failed")
            self.assertIn("solver probe error", detail)


class SmtReplayTests(unittest.TestCase):
    def test_unsupported_or_misleading_command_sequences_never_reach_solver(self):
        fragments = [
            "(check-sat)\n(assert false)\n",
            "(check-sat)\n(check-sat)\n",
            "(assert true)\n(reset)\n(check-sat)\n",
            "(push 1)\n(assert true)\n(check-sat)\n",
            "(set-option :regular-output-channel \"hidden.log\")\n(check-sat)\n",
            "(assert true)\n",
        ]
        with tempfile.TemporaryDirectory() as directory:
            for fragment in fragments:
                with self.subTest(fragment=fragment), patch.object(adapter.legacy, "run_z3_fragment") as runner:
                    result = adapter._recheck(fragment, Path(directory))
                    self.assertEqual(result["status"], "error")
                    self.assertTrue(result["diagnostics"])
                    runner.assert_not_called()

    def test_replay_keeps_all_assertions_and_binds_original_hash(self):
        fragment = "(set-logic QF_NIA)\n(declare-const x Int)\n(assert (< (* x x) 0))\n(check-sat)\n"
        responses = [
            {"status": "ok", "result": "unsat", "solver": "z3"},
            {"status": "ok", "result": "unsat\n()", "solver": "z3"},
        ]
        with tempfile.TemporaryDirectory() as directory, patch.object(adapter.legacy, "run_z3_fragment", side_effect=responses) as runner:
            result = adapter._recheck(fragment, Path(directory))
            self.assertEqual(runner.call_args_list[0].args[0], fragment)
            self.assertIn("(assert (< (* x x) 0))", runner.call_args_list[1].args[0])
            self.assertEqual(result["artifact_sha256"], hashlib.sha256(fragment.encode()).hexdigest())
            self.assertTrue((Path(directory) / "pipeline_review_evidence.smt2").is_file())

    @unittest.skipUnless(shutil.which("z3"), "Real Z3 executable unavailable")
    def test_real_solver_witness_and_core(self):
        cases = [
            ("(set-logic QF_LRA)\n(declare-const voltage Real)\n(assert (>= voltage 10.5))\n(check-sat)\n", "sat", "voltage"),
            ("(declare-const x Int)\n(assert (! (> x 5) :named req_R1))\n(assert (! (< x 2) :named req_R2))\n(check-sat)\n", "unsat", "req_R1 req_R2"),
        ]
        with tempfile.TemporaryDirectory() as directory, patch.object(adapter.legacy, "_SOLVER_RUNNER", adapter.legacy.Z3Runner()):
            for fragment, verdict, expected in cases:
                with self.subTest(verdict=verdict):
                    result = adapter._recheck(fragment, Path(directory))
                    self.assertEqual(adapter.legacy._solver_verdict(result), verdict)
                    self.assertEqual(result["evidence"]["status"], "ok")
                    self.assertIn(expected, result["evidence"]["result"])


if __name__ == "__main__":
    unittest.main()
