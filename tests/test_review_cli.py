"""CLI/API workflow equivalence, explicit design review, and immutable exports.

All saved runs live in temporary directories. Provider output and compilation
are controlled fixtures; satisfiability/behavior queries use the real local Z3.
The API comparison uses an in-process TestClient, never a listening server.
"""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from fastapi.testclient import TestClient
import requirements_pipeline as solver
import review_cli as cli
import review_workflow as workflow
from review_server import create_app
from review_behavior import validate_behavior
from review_profile import interpret, analyze
from review_sysml import generate_sysml


def candidate():
    return json.loads((ROOT / 'examples/synthetic/contracts_voltage.json').read_text())['behavior']


def reviewed():
    return {'reviewer': 'CLI fixture engineer', 'rationale': 'Independent voltage setting reviewed against the source.',
            'acknowledge': True}


def invoke(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            status = cli.main(argv)
        except SystemExit as exc:
            status = exc.code
    output = json.loads(stdout.getvalue()) if stdout.getvalue().strip() else None
    return status, output, stderr.getvalue()


class CLIRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='review_cli_request_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'requirements.csv'
        self.source.write_bytes(b'id,text\r\nREQ-001,battery.voltage <= 28 V\r\n')
        self.store = self.root / 'runs'

    def test_inference_defaults_and_source_newlines_match_gui(self):
        args = cli.argument_parser().parse_args(['--statement', str(self.source)])
        payload = workflow.RunInput.model_validate(cli.request_payload(args))
        self.assertEqual(payload.name, 'Requirements model')
        self.assertEqual(payload.engine, 'local')
        self.assertEqual(payload.analysis_mode, 'requirements')
        self.assertEqual(payload.format, 'csv')
        self.assertEqual(payload.text.encode(), self.source.read_bytes())
        self.assertIsNone(payload.behavior)
        self.assertIsNone(payload.design_review)
        self.assertEqual(payload.candidate_provenance, {})

    def test_all_three_formats_and_explicit_override(self):
        for suffix, expected in [('TXT', 'text'), ('csv', 'csv'), ('json', 'json')]:
            path = self.root / ('source.' + suffix)
            path.write_text('[]' if expected == 'json' else 'text')
            args = cli.argument_parser().parse_args(['--statement', str(path)])
            self.assertEqual(cli.request_payload(args)['format'], expected)
        args = cli.argument_parser().parse_args(['--statement', str(self.source), '--format', 'text'])
        self.assertEqual(cli.request_payload(args)['format'], 'text')

    def test_request_json_rejects_even_explicit_default_input_options(self):
        path = self.root / 'request.json'
        path.write_text(json.dumps({'text': 'battery.voltage <= 28 V'}))
        for extra in [('--name', 'Requirements model'), ('--engine', 'local'), ('--format', 'text'),
                      ('--analysis-mode', 'requirements'), ('--reviewer', 'Engineer'),
                      ('--acknowledge-design',), ('--parent-source-hash', 'old')]:
            with self.subTest(extra=extra):
                status, output, error = invoke(['--request-json', str(path), '--data-dir', str(self.store), *extra])
                self.assertEqual(status, 2)
                self.assertIsNone(output)
                self.assertIn('cannot be combined', error)
        self.assertFalse(self.store.exists())

    def test_request_json_is_exact_and_data_dir_is_separate(self):
        body = {'text': 'battery.voltage <= 28 V', 'name': 'Exact JSON', 'format': 'text'}
        path = self.root / 'request.json'
        path.write_text(json.dumps(body))
        args = cli.argument_parser().parse_args(['--request-json', str(path), '--data-dir', str(self.store)])
        self.assertEqual(cli.request_payload(args), body)
        with patch.dict(os.environ, {'MBSE_REVIEW_DATA_DIR': str(self.store)}):
            self.assertEqual(cli.argument_parser().parse_args(['--request-json', str(path)]).data_dir, self.store)

    def test_invalid_or_nonobject_inputs_do_not_allocate(self):
        cases = [('broken.json', '{oops', '--request-json'), ('list.json', '[]', '--behavior')]
        for name, text, flag in cases:
            path = self.root / name
            path.write_text(text)
            args = [flag, str(path)] if flag == '--request-json' else ['--statement', str(self.source), flag, str(path)]
            status, _, error = invoke([*args, '--data-dir', str(self.store)])
            self.assertEqual(status, 2, error)
        self.assertFalse(self.store.exists())

    def test_unsafe_output_paths_rejected_before_run_creation(self):
        for target in (self.source, self.store / 'model.sysml'):
            with self.subTest(target=target):
                status, _, error = invoke(['--statement', str(self.source), '--data-dir', str(self.store), '--sysml-output', str(target)])
                self.assertEqual(status, 2, error)
        self.assertFalse(self.store.exists())
        self.assertEqual(self.source.read_bytes(), b'id,text\r\nREQ-001,battery.voltage <= 28 V\r\n')

    def test_legacy_options_receive_explicit_dispatch_guidance(self):
        status, _, error = invoke(['--statement', str(self.source), '--llm-provider', 'codex'])
        self.assertEqual(status, 2)
        self.assertIn('requirements_pipeline.py legacy --help', error)

    def test_default_dispatch_is_canonical_and_review_is_explicit(self):
        for argv in (['--statement', 'input.csv'], ['run', '--statement', 'input.csv']):
            with patch('canonical_cli.main', return_value=7) as canonical, patch('review_cli.main') as review, patch.object(solver, 'legacy_main') as legacy:
                with self.assertRaises(SystemExit) as result:
                    solver.main(argv)
                self.assertEqual(result.exception.code, 7)
                canonical.assert_called_once_with(argv)
                review.assert_not_called()
                legacy.assert_not_called()
        with patch('review_cli.main', return_value=8) as review, patch('canonical_cli.main') as canonical:
            with self.assertRaises(SystemExit) as result:
                solver.main(['review', '--statement', 'input.csv'])
            self.assertEqual(result.exception.code, 8)
            review.assert_called_once_with(['--statement', 'input.csv'])
            canonical.assert_not_called()
        with patch('review_cli.main') as review, patch('canonical_cli.main') as canonical, patch.object(solver, 'legacy_main') as legacy:
            solver.main(['legacy', '--help'])
            legacy.assert_called_once_with(['--help'])
            review.assert_not_called()
            canonical.assert_not_called()


@unittest.skipUnless(shutil.which('z3'), 'Real local Z3 executable unavailable')
class CLIExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='review_cli_execution_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / 'runs'
        self.source = self.root / 'requirements.txt'
        self.source.write_text('battery.voltage <= 28 V')
        compile_patch = patch('review_workflow.compile_sysml', side_effect=lambda path, **kwargs: {'status': 'passed', 'diagnostics': [], 'model_sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest()})
        compile_patch.start()
        self.addCleanup(compile_patch.stop)
        solver_patch = patch.object(solver, '_SOLVER_RUNNER', solver.Z3Runner())
        solver_patch.start()
        self.addCleanup(solver_patch.stop)
        self.provider_calls = []

    def run_cli(self, *extra):
        return invoke(['--statement', str(self.source), '--data-dir', str(self.store), *map(str, extra)])

    def test_default_run_is_complete_but_never_checks_or_approves_design(self):
        with patch('review_behavior.analyze_behavior') as checker:
            status, output, error = self.run_cli()
        self.assertEqual(status, 0, error)
        run = output['run']
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['analysis']['status'], 'sat')
        self.assertEqual(run['behavioral_analysis']['status'], 'not_run')
        self.assertIsNone(run['behavior'])
        self.assertEqual(run['reviews'], [])
        self.assertTrue(run['baseline']['blockers'])
        self.assertEqual(output['exports'], {})
        checker.assert_not_called()
        self.assertEqual(run, workflow.RunStore(self.store).get(run['id']))

    def test_conflict_is_completed_workflow_not_execution_failure(self):
        self.source.write_text('battery.voltage <= 28 V\nbattery.voltage >= 30 V')
        status, output, error = self.run_cli()
        self.assertEqual(status, 0, error)
        self.assertEqual(output['run']['status'], 'completed')
        self.assertEqual(output['run']['analysis']['status'], 'unsat')
        self.assertTrue(output['run']['analysis']['unsat_core'])
        self.assertTrue(output['run']['baseline']['blockers'])

    def test_compiler_execution_failure_returns_failed_saved_run(self):
        with patch('review_workflow.compile_sysml', return_value={'status': 'failed', 'diagnostics': [{'severity': 'error', 'message': 'Fixture compilation failure'}]}):
            status, output, error = self.run_cli()
        self.assertEqual(status, 1, error)
        self.assertEqual(output['run']['status'], 'failed')
        self.assertTrue(output['run']['artifacts'])
        self.assertTrue((self.store / output['run']['id'] / 'run.json').is_file())

    def test_design_requires_explicit_review_before_allocating_run(self):
        path = self.root / 'behavior.json'
        path.write_text(json.dumps(candidate()))
        with patch('review_workflow.run_job') as job:
            for args in [('--behavior', path), ('--analysis-mode', 'check_design', '--behavior', path),
                         ('--analysis-mode', 'check_design', '--behavior', path, '--reviewer', 'Engineer', '--rationale', 'Reviewed')]:
                status, output, error = self.run_cli(*args)
                self.assertEqual(status, 2, error)
                self.assertIsNone(output)
            job.assert_not_called()
        self.assertEqual(list(self.store.glob('run-*')), [])

    def test_checked_run_carries_provenance_and_review_but_no_acceptance(self):
        from review_candidate import inspect_candidate
        model = candidate()
        requirements = workflow.profile.parse_requirements(self.source.read_text(), 'text', 'Requirements model')
        inspection = inspect_candidate(model, requirements)
        row = next(r for r in inspection['rows'] if r['pointer'] == '/transitions/0')
        provenance = {row['pointer']: {'expression_sha256': row['expression_sha256'], 'origin': 'design',
                                     'requirement_ids': [], 'rationale': 'Fixed regulator design supplied by the engineer.'}}
        behavior_path, provenance_path = self.root / 'behavior.json', self.root / 'provenance.json'
        behavior_path.write_text(json.dumps(model)); provenance_path.write_text(json.dumps(provenance))
        status, output, error = self.run_cli('--analysis-mode', 'check_design', '--behavior', behavior_path,
                                            '--candidate-provenance', provenance_path, '--reviewer', 'Engineer',
                                            '--rationale', 'Reviewed fixed voltage design', '--acknowledge-design')
        self.assertEqual(status, 0, error)
        run = output['run']
        self.assertEqual(run['behavioral_analysis']['status'], 'bounded_pass')
        self.assertEqual(run['candidate_provenance']['/transitions/0']['origin'], 'design')
        self.assertTrue(run['design_review']['acknowledge'])
        self.assertEqual(run['design_review']['behavior_sha256'], run['contracts']['behavior_sha256'])
        self.assertEqual(run['reviews'], [])
        self.assertTrue(any('architecture binding' in b for b in run['baseline']['blockers']))

    def test_exports_copy_exact_manifest_bytes_and_do_not_change_saved_run(self):
        prefix = self.root / 'exports' / 'demo'
        model_path = self.root / 'exports' / 'battery.sysml'
        status, output, error = self.run_cli('--output-prefix', prefix, '--sysml-output', model_path)
        self.assertEqual(status, 0, error)
        run = output['run']
        self.assertEqual(len(output['exports']), len(run['artifacts']) + 1)
        for destination, ref in output['exports'].items():
            data = Path(destination).read_bytes()
            self.assertEqual(data, (self.store / run['id'] / ref['artifact']).read_bytes())
            self.assertEqual(hashlib.sha256(data).hexdigest(), ref['sha256'])
        saved = workflow.RunStore(self.store).get(run['id'])
        self.assertEqual(saved, run)
        self.assertNotIn('exports', saved)
        self.assertEqual(model_path.read_text(), run['model']['text'])

    def test_missing_trace_export_is_explicit_and_no_partial_exports_are_written(self):
        model, trace = self.root / 'domain.sysml', self.root / 'trace.sysml'
        status, output, error = self.run_cli('--sysml-output', model, '--traceability-output', trace)
        self.assertEqual(status, 1)
        self.assertEqual(output['run']['status'], 'completed')
        self.assertIn('No traceability SysML artifact', error)
        self.assertEqual(output['exports'], {})
        self.assertTrue(output['export_errors'])
        self.assertFalse(model.exists())
        self.assertFalse(trace.exists())

    def test_prefix_never_overwrites_previous_copy_and_is_all_or_nothing(self):
        prefix = self.root / 'previous'
        protected = self.root / 'previous_analysis.json'
        protected.write_bytes(b'previous export')
        status, output, error = self.run_cli('--output-prefix', prefix)
        self.assertEqual(status, 1, error)
        self.assertEqual(protected.read_bytes(), b'previous export')
        self.assertEqual(list(self.root.glob('previous_*')), [protected])
        self.assertEqual(output['exports'], {})

    def test_export_refuses_tampered_recorded_artifact(self):
        status, output, error = self.run_cli()
        self.assertEqual(status, 0, error)
        run = output['run']
        artifact = self.store / run['id'] / run['artifacts'][0]['name']
        artifact.write_bytes(artifact.read_bytes() + b'changed')
        args = cli.argument_parser().parse_args(['--statement', str(self.source), '--data-dir', str(self.store), '--output-prefix', str(self.root / 'bad')])
        with self.assertRaisesRegex(cli.ExportError, 'changed after analysis'):
            cli.export_artifacts(workflow.RunStore(self.store), run, args)
        self.assertEqual(list(self.root.glob('bad_*')), [])

    def replay_provider_capture(self, directory, name, requirements, progress, propose_behavior=False):
        """Replay identical typed/SMT/model/provider output for both front ends."""
        capture = {'schema': 'test_provider_capture/1', 'response': {'interpretation': 'battery.voltage <= 28 V',
                   'behavior': candidate(), 'reason': 'Captured fixed regulator candidate; review required.'}}
        self.provider_calls.append({'name': name, 'requirements': deepcopy(requirements), 'propose_behavior': propose_behavior})
        (directory / 'provider_capture.json').write_text(json.dumps(capture, indent=2) + '\n')
        typed = interpret(requirements)
        analysis = analyze(typed, directory)
        model = generate_sysml(name, requirements, typed)
        model['path'] = str(directory / 'Captured.sysml')
        Path(model['path']).write_text(model['text'])
        model['trace_path'] = str(directory / 'Captured_trace.sysml')
        Path(model['trace_path']).write_text('package CapturedTrace { doc /* Captured traceability candidate. */ }\n')
        for stage, detail in [('interpretation', 'Replay recorded typed interpretation'), ('analysis', 'Run exact captured scalar constraints'), ('generation', 'Replay captured SysML candidate')]:
            progress(stage, 'passed', detail)
        proposal = None
        if propose_behavior:
            proposal = {'status': 'proposed', 'origin': 'llm', 'summary': capture['response']['reason'],
                        'candidate': validate_behavior(capture['response']['behavior'], [r['id'] for r in requirements])}
            (directory / 'llm_behavior_proposal.json').write_text(json.dumps(proposal, indent=2) + '\n')
        return {'tlr': {'schema_version': 'review-pipeline-1', 'raw': typed,
                        'requirements': [{**r, 'status': 'unsupported'} for r in requirements]},
                'analysis': analysis, 'model': model, 'behavior_proposal': proposal, 'errors': [], 'log': 'Replayed controlled provider capture.'}

    def equivalent_pair(self, payload):
        """Hold time/id/path constant so even evidence hashes must match exactly."""
        fixed_id = uuid.UUID('12345678-1234-5678-1234-567812345678')
        with patch('review_workflow.now', return_value='2026-09-18T12:00:00+00:00'), patch('review_workflow.uuid.uuid4', return_value=fixed_id):
            app = create_app(self.store)
            with TestClient(app) as client:
                response = client.post('/api/runs', json=payload)
                self.assertEqual(response.status_code, 202, response.text)
                run_id = response.json()['id']
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    api_run = app.state.store.get(run_id)
                    if api_run['status'] in ('completed', 'failed'):
                        break
                    time.sleep(.01)
                self.assertEqual(api_run['status'], 'completed', api_run.get('errors'))
                api_files = {p.name: p.read_bytes() for p in (self.store / run_id).iterdir() if p.is_file()}
            app.state.executor.shutdown(wait=True)
            shutil.rmtree(self.store)
            request = self.root / 'request.json'
            request.write_text(json.dumps(payload))
            status, output, error = invoke(['--request-json', str(request), '--data-dir', str(self.store)])
            self.assertEqual(status, 0, error)
            cli_run = output['run']
            cli_files = {p.name: p.read_bytes() for p in (self.store / cli_run['id']).iterdir() if p.is_file()}
        # No normalization or weakened comparison: paths, IDs and timestamps were
        # identical inputs; source, stages, contracts, queries, hashes and SysML match.
        self.assertEqual(cli_run, api_run)
        self.assertEqual(cli_files, api_files)
        self.assertEqual(cli_run['reviews'], [])
        self.assertTrue(cli_run['baseline']['blockers'])
        return cli_run

    def test_local_gui_and_cli_have_identical_complete_evidence(self):
        run = self.equivalent_pair({'text': self.source.read_text()})
        self.assertEqual(run['behavioral_analysis']['status'], 'not_run')
        self.assertTrue(any(a['name'].endswith('.smt2') for a in run['artifacts']))

    def test_checked_gui_and_cli_have_identical_queries_contracts_and_review_gates(self):
        run = self.equivalent_pair({'text': self.source.read_text(), 'analysis_mode': 'check_design',
                                   'behavior': candidate(), 'design_review': reviewed()})
        self.assertEqual(run['behavioral_analysis']['status'], 'bounded_pass')
        self.assertTrue(run['candidate_inspection']['rows'])
        self.assertIn('FiniteTrace', run['model']['text'])

    def test_pipeline_gui_and_cli_replay_identical_provider_capture_and_proposal_gates(self):
        actual = shutil.which
        with patch('review_workflow.shutil.which', side_effect=lambda name: '/fixture/codex' if name == 'codex' else actual(name)), \
             patch('review_pipeline_adapter.run_existing_pipeline', side_effect=self.replay_provider_capture):
            run = self.equivalent_pair({'text': self.source.read_text(), 'engine': 'pipeline', 'analysis_mode': 'propose_design'})
        self.assertEqual(self.provider_calls[0], self.provider_calls[1])
        self.assertTrue(self.provider_calls[0]['propose_behavior'])
        self.assertEqual(run['behavioral_analysis']['status'], 'pending_review')
        self.assertIsNone(run['behavior'])
        self.assertNotIn('FiniteTrace', run['model']['text'])
        self.assertTrue(run['candidate_inspection']['rows'])

    def test_pipeline_trace_export_copies_separate_recorded_artifact(self):
        actual = shutil.which
        trace = self.root / 'trace.sysml'
        with patch('review_workflow.shutil.which', side_effect=lambda name: '/fixture/codex' if name == 'codex' else actual(name)), \
             patch('review_pipeline_adapter.run_existing_pipeline', side_effect=self.replay_provider_capture):
            status, output, error = self.run_cli('--engine', 'pipeline', '--traceability-output', trace)
        self.assertEqual(status, 0, error)
        self.assertEqual(trace.read_bytes(), Path(output['run']['model']['trace_path']).read_bytes())
        self.assertFalse(self.provider_calls[0]['propose_behavior'])

    def test_pipeline_requirements_gui_and_cli_preserve_identical_configuration(self):
        actual = shutil.which
        with patch.dict(os.environ, {'CODEX_MBSE_MODEL': 'controlled-capture-model', 'SMT_MAX_SEMANTIC_REPAIRS': '9'}), \
             patch('review_workflow.shutil.which', side_effect=lambda name: '/fixture/codex' if name == 'codex' else actual(name)), \
             patch('review_pipeline_adapter.run_existing_pipeline', side_effect=self.replay_provider_capture):
            run = self.equivalent_pair({'text': self.source.read_text(), 'engine': 'pipeline'})
        self.assertEqual(self.provider_calls[0], self.provider_calls[1])
        self.assertFalse(self.provider_calls[0]['propose_behavior'])
        self.assertEqual(run['execution_config']['generation']['model'], 'controlled-capture-model')
        self.assertEqual(run['execution_config']['generation']['semantic_repair_attempts'], 0)
        self.assertEqual(run['behavioral_analysis']['status'], 'not_run')
        self.assertEqual(run['formalization_quality']['engine'], 'pipeline')

    def test_pipeline_checked_gui_and_cli_share_exact_behavior_and_quality(self):
        actual = shutil.which
        with patch('review_workflow.shutil.which', side_effect=lambda name: '/fixture/codex' if name == 'codex' else actual(name)), \
             patch('review_pipeline_adapter.run_existing_pipeline', side_effect=self.replay_provider_capture):
            run = self.equivalent_pair({'text': self.source.read_text(), 'engine': 'pipeline',
                                       'analysis_mode': 'check_design', 'behavior': candidate(), 'design_review': reviewed()})
        self.assertEqual(self.provider_calls[0], self.provider_calls[1])
        self.assertFalse(self.provider_calls[0]['propose_behavior'])
        self.assertEqual(run['behavioral_analysis']['status'], 'bounded_pass')
        self.assertTrue(run['formalization_quality']['dimensions'])
        self.assertTrue(run['baseline']['blockers'])

    def test_quality_and_configuration_cannot_disappear_from_saved_snapshot(self):
        status, output, error = self.run_cli()
        self.assertEqual(status, 0, error)
        for key in ('formalization_quality', 'execution_config'):
            altered = deepcopy(output['run'])
            altered.pop(key)
            with self.subTest(key=key):
                self.assertTrue(any(key + '.json' in issue for issue in workflow.evidence_integrity(altered)))

    def test_checked_revision_requires_exact_parent_hashes(self):
        status, first, error = self.run_cli()
        self.assertEqual(status, 0, error)
        parent = first['run']
        path = self.root / 'behavior.json'; path.write_text(json.dumps(candidate()))
        arguments = ['--analysis-mode', 'check_design', '--behavior', path, '--reviewer', 'Engineer',
                     '--rationale', 'Reviewed candidate', '--acknowledge-design', '--parent-run-id', parent['id'],
                     '--revision-rationale', 'Introduce a separately reviewed fixed regulator model']
        status, _, error = self.run_cli(*arguments, '--parent-source-hash', parent['source_hash'], '--parent-evidence-hash', 'stale')
        self.assertEqual(status, 2, error)
        self.assertEqual(len(list(self.store.glob('run-*'))), 1)
        status, child, error = self.run_cli(*arguments, '--parent-source-hash', parent['source_hash'], '--parent-evidence-hash', parent['evidence_hash'])
        self.assertEqual(status, 0, error)
        self.assertEqual(child['run']['parent_run_id'], parent['id'])
        self.assertEqual(child['run']['design_review']['parent_evidence_hash'], parent['evidence_hash'])
        self.assertIsNotNone(child['run']['correction_summary'])


if __name__ == '__main__':
    unittest.main()
