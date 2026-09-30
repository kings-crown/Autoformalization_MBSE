"""Canonical GUI transport parity, frozen outputs and contextual source handling."""
from concurrent.futures import Future
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import canonical_cli
from canonical_tlr import validate_tlr
from review_canonical import create_router, safe_artifact, CanonicalRunInput, create_run


class InlineExecutor:
    def submit(self, fn, *args):
        result = Future()
        try:
            result.set_result(fn(*args))
        except BaseException as exc:
            result.set_exception(exc)
        return result


class QueuedExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))
        return Future()


def fixture():
    source = {'id': 'R1', 'text': 'Battery voltage shall be at most 28 V.',
        'source': {'document': 'Synthetic source', 'location': 'section 1',
                   'context': {'definitions': [{'id': 'VOLT', 'quote': 'Voltage is expressed in volts.'}],
                               'scope': ['One battery voltage observation.']}}}
    tlr = {'schema': 'mbse_tlr/1', 'abstraction_policy': 'mbse_abstraction/1',
        'variables': [{'name': 'voltage', 'type': 'Real', 'unit': 'V', 'description': 'Observed battery voltage.'}],
        'assumptions': [], 'requirements': [{'id': 'R1', 'status': 'supported',
            'formula': {'op': '<=', 'args': [{'var': 'voltage'}, {'value': '28', 'unit': 'V'}]},
            'abstraction': {'kind': 'state_constraint', 'meaning': 'Inclusive voltage upper limit.',
                            'scope': 'One observation.', 'limitations': []}}]}
    payload = {'name': 'Canonical transport fixture', 'text': json.dumps({'requirements': [source]}),
               'format': 'json', 'condition': 'C', 'model': 'test-only', 'tlr': tlr}
    return source, tlr, payload


def compile_ok(*args, **kwargs):
    return {'status': 'passed', 'diagnostics': []}


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.provider = Mock(side_effect=AssertionError('Tests never call a provider'))
        self.router = create_router(self.root / 'runs', InlineExecutor(), generator=self.provider)
        self.store = self.router.store
        app = FastAPI()
        app.include_router(self.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.compiler = patch('canonical_cli._compile', side_effect=compile_ok)
        self.compiler.start()
        self.addCleanup(self.compiler.stop)

    def post(self, payload):
        return self.client.post('/api/workflow/runs', json=payload)

    def test_json_context_and_exact_wording_reach_same_cli_source_loader(self):
        source, tlr, payload = fixture()
        payload['condition'] = 'B'
        source['text'] = '  Battery voltage shall be at most 28 V.  '
        payload['text'] = json.dumps({'requirements': [source]})
        reply = self.post(payload)
        self.assertEqual(reply.status_code, 202, reply.text)
        run = reply.json()
        self.assertEqual(run['status'], 'completed', run)
        self.assertEqual(run['sources'], [source])
        self.assertEqual(run['tlr']['requirements'][0]['source'], source['source'])
        self.assertEqual(run['tlr']['requirements'][0]['text'], source['text'])
        stored = self.store.directory(run['id']) / 'canonical' / 'sources.json'
        self.assertEqual(json.loads(stored.read_text()), [source])
        self.provider.assert_not_called()

    def test_bad_combinations_and_inputs_fail_before_run_creation(self):
        _, _, baseline = fixture()
        invalid = [
            {'condition': 'A'},
            {'feedback_repairs': 1, 'abstention_repairs': 1},
            {'condition': 'BC', 'feedback_repairs': 1},
            {'condition': 'A', 'tlr': None, 'abstention_repairs': 1},
            {'sysml_text': 'package X {}'},
            {'feedback_repairs': 6},
            {'abstention_repairs': True},
            {'context': {'variables': [], 'background': []}},
            {'development_scenarios': {}},
            {'text': '{"requirements":[],"requirements":[]}'},
            {'name': '   '},
            {'model': '   '},
            {'unexpected': True},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                reply = self.post({**baseline, **changes})
                self.assertEqual(reply.status_code, 422, reply.text)
        self.assertEqual(self.store.all(), [])
        self.assertFalse(list(self.store.root.glob('run-*')))
        self.provider.assert_not_called()

    def test_complete_auxiliary_request_size_is_bounded_before_output(self):
        _, _, payload = fixture()
        payload['context'] = {'large': 'x' * 4_000_000}
        reply = self.post(payload)
        self.assertEqual(reply.status_code, 422)
        self.assertIn('4,000,000', reply.text)
        self.assertEqual(self.store.all(), [])
        self.provider.assert_not_called()

    def test_normalized_fixed_context_is_available_for_frozen_evaluation_and_revision(self):
        _, _, payload = fixture()
        payload['condition'] = 'B'
        payload['context'] = {'variables': [{'name': 'voltage', 'type': 'Real', 'unit': 'V'}],
            'background': [], 'symbol_meanings': {'voltage': 'Observed battery voltage.'}}
        run = self.post(payload).json()
        self.assertEqual(run['status'], 'completed', run)
        self.assertEqual(run['fixed_context'], payload['context'])
        self.assertTrue(run['request_configuration']['fixed_context'])
        self.assertIsNone(run['development_scenarios'])
        self.assertEqual(run['canonical_result']['tlr'], run['tlr'])

    def test_fixed_context_rejects_changed_initial_domains_before_generation(self):
        _, _, payload = fixture()
        payload['context'] = {'variables': [{'name': 'voltage', 'type': 'Real', 'unit': 'V',
                                            'bounds': {'upper': '20'}}], 'background': []}
        reply = self.post(payload)
        self.assertEqual(reply.status_code, 422)
        self.assertIn('fixed study context', reply.text)
        self.provider.assert_not_called()

    def test_scenario_validation_rejects_held_out_role_before_provider(self):
        _, _, payload = fixture()
        payload.update(feedback_repairs=1, development_scenarios={'schema': 'development_scenarios/1',
            'role': 'evaluation', 'origin': {}, 'context': {}, 'symbol_meanings': {}, 'scenarios': []})
        reply = self.post(payload)
        self.assertEqual(reply.status_code, 422, reply.text)
        self.assertIn('development role', reply.text)
        self.provider.assert_not_called()

    def test_malformed_generation_retains_source_response_and_error(self):
        source, _, payload = fixture()
        payload.pop('tlr')
        self.provider.side_effect = None
        self.provider.return_value = 'not valid TLR JSON'
        run = self.post(payload).json()
        self.assertEqual(run['status'], 'failed')
        self.assertIsNone(run['model_text'])
        self.assertTrue(run['errors'])
        self.assertEqual(run['sources'], [source])
        names = {row['path'] for row in run['artifacts']}
        self.assertIn('canonical/generation_response.txt', names)
        self.assertIn('canonical/result.json', names)
        self.assertEqual(self.provider.call_count, 1)
        prompt = json.loads(self.provider.call_args.args[1])
        self.assertEqual(prompt['requirements'], [source])

    def test_direct_supplied_sysml_has_no_tlr_or_invented_audit(self):
        _, _, payload = fixture()
        payload.update(condition='A', tlr=None, sysml_text='package DirectCandidate {}\n')
        run = self.post(payload).json()
        self.assertEqual(run['status'], 'completed')
        self.assertIsNone(run['tlr'])
        self.assertEqual(run['analysis']['status'], 'not_run')
        self.assertEqual(run['model_text'], payload['sysml_text'].strip())
        self.provider.assert_not_called()

    def test_review_does_not_change_canonical_admission_or_selected_model(self):
        _, _, payload = fixture()
        payload['condition'] = 'B'
        run = self.post(payload).json()
        before = deepcopy(run['canonical_result'])
        model = run['model_text']
        reply = self.client.post(f"/api/workflow/runs/{run['id']}/reviews", json={
            'reviewer': 'Engineer', 'rationale': 'Inspected the declared scope.', 'decision': 'approve'})
        self.assertEqual(reply.status_code, 201, reply.text)
        after = reply.json()
        self.assertEqual(after['canonical_result'], before)
        self.assertEqual(after['admission'], before['admission'])
        self.assertEqual(after['model_text'], model)
        self.assertEqual(after['reviews'][0]['decision'], 'approve')

    def test_artifacts_are_recursive_and_fresh_evaluation_files_are_discoverable(self):
        _, _, payload = fixture()
        payload['condition'] = 'B'
        run = self.post(payload).json()
        directory = self.store.directory(run['id'])
        nested = directory / 'evaluations/eval-0123456789abcdef/report'
        nested.mkdir(parents=True)
        (nested / 'report.json').write_text('{"status":"partial"}')
        refreshed = self.client.get(f"/api/workflow/runs/{run['id']}").json()
        artifact = next(x for x in refreshed['artifacts'] if x['path'].endswith('/report.json'))
        self.assertEqual(self.client.get(artifact['url']).json(), {'status': 'partial'})
        packet = self.client.get(f"/api/workflow/runs/{run['id']}/packet").json()
        self.assertIn(artifact['path'], packet['files'])
        self.assertFalse(any('sha256' in x for x in refreshed['artifacts']))
        self.assertEqual(packet['run']['canonical_result'], run['canonical_result'])

    def test_symlinks_parent_symlinks_and_path_traversal_are_not_served(self):
        _, _, payload = fixture()
        payload['condition'] = 'B'
        run = self.post(payload).json()
        directory = self.store.directory(run['id'])
        secret = self.root / 'external-secret.txt'
        secret.write_text('external')
        (directory / 'linked.txt').symlink_to(secret)
        outside_dir = self.root / 'outside'
        outside_dir.mkdir()
        (outside_dir / 'secret.txt').write_text('external')
        (directory / 'linked_dir').symlink_to(outside_dir, target_is_directory=True)
        for relative in ('../external-secret.txt', 'linked.txt', 'linked_dir/secret.txt', '/etc/passwd'):
            with self.subTest(path=relative), self.assertRaises(HTTPException):
                safe_artifact(directory, relative)
        names = {x['path'] for x in self.store.artifacts(run['id'])}
        self.assertNotIn('linked.txt', names)
        self.assertNotIn('linked_dir/secret.txt', names)
        self.assertEqual(self.client.get(f"/api/workflow/runs/{run['id']}/artifacts/linked_dir/secret.txt").status_code, 404)

    def test_interrupted_runs_are_retained_without_automatic_retry(self):
        _, _, payload = fixture()
        run = create_run(self.store, CanonicalRunInput(**payload))
        self.store.update(run['id'], status='running', execution_owner={'pid': 999999999, 'start_ticks': '1'})
        calls = QueuedExecutor()
        restored = create_router(self.store.root, calls, generator=self.provider)
        saved = restored.store.get(run['id'])
        self.assertEqual(saved['status'], 'failed')
        self.assertIn('not restarted', saved['errors'][0])
        self.assertEqual(calls.calls, [])
        self.provider.assert_not_called()

    def test_reviews_require_frozen_candidate_but_do_not_require_extra_provenance(self):
        _, _, payload = fixture()
        run = create_run(self.store, CanonicalRunInput(**payload))
        reply = self.client.post(f"/api/workflow/runs/{run['id']}/reviews", json={
            'reviewer': 'Engineer', 'rationale': 'Not frozen yet.', 'decision': 'defer'})
        self.assertEqual(reply.status_code, 409)


@unittest.skipUnless(shutil.which('z3'), 'Local Z3 required')
class SolverParityTests(unittest.TestCase):
    setUp = TransportTests.setUp
    post = TransportTests.post

    def test_same_tlr_compiler_and_real_solver_as_direct_cli_core(self):
        source, tlr, payload = fixture()
        run = self.post(payload).json()
        self.assertEqual(run['status'], 'completed', run)
        direct = canonical_cli.run_candidate([source], self.root / 'direct', condition='C',
            tlr=tlr, model='test-only', name=payload['name'], generator=self.provider)
        self.assertEqual(run['tlr'], direct['tlr'])
        self.assertEqual(run['model_text'], (self.root / 'direct/model.sysml').read_text())
        self.assertEqual(run['analysis']['status'], direct['analysis']['status'])
        self.assertEqual(run['analysis']['consistency_status'], 'sat')
        self.assertEqual(run['admission'], direct['admission'])
        self.assertEqual(run['representation'], direct['representation'])
        nested = 'canonical/audit/requirement_0001_violatability.smt2'
        found = next(x for x in run['artifacts'] if x['path'] == nested)
        self.assertIn('(not (<= v_voltage_0 28))', self.client.get(found['url']).text)
        self.assertEqual(run['canonical_result'], json.loads((self.store.directory(run['id']) / 'canonical/result.json').read_text()))
        self.provider.assert_not_called()

    def test_scenario_suite_and_missing_binding_are_retained_without_false_pass(self):
        source, tlr, payload = fixture()
        suite = {'schema': 'development_scenarios/1', 'role': 'development',
            'origin': {'kind': 'fixture', 'description': 'Synthetic missing-binding transport test.'},
            'context': {'variables': [{'name': 'voltage', 'type': 'Real', 'unit': 'V'},
                                     {'name': 'observed', 'type': 'Bool'}], 'background': []},
            'symbol_meanings': {'voltage': 'Observed battery voltage.',
                                'observed': 'Whether a battery voltage observation exists.'},
            'scenarios': [{'id': 'OBSERVED', 'requirement_ids': ['R1'],
                'description': 'A battery voltage observation is available.', 'predicate': {'var': 'observed'},
                'expected': 'sat', 'rationale': 'An absent binding must remain unavailable to the test.',
                'source_basis': [{'source_id': 'R1', 'quote': source['text']}]}]}
        payload.update(feedback_repairs=1, development_scenarios=suite)
        self.provider.side_effect = None
        self.provider.return_value = json.dumps({'schema': 'semantic_repair_proposal/1', 'tlr': tlr,
            'reviews': [{'id': 'R1', 'outcome': 'retained',
                         'reason': 'Preserve the original inclusive voltage limit and its declared scope.',
                         'source_basis': []}]})
        run = self.post(payload).json()
        self.assertEqual(run['status'], 'completed', run)
        self.assertEqual(run['development_scenarios']['role'], 'development')
        self.assertEqual(run['development_results']['status'], 'incomplete')
        self.assertEqual(run['development_results']['counts']['not_run'], 1)
        self.assertEqual(run['development_results']['counts']['passed'], 0)
        self.assertEqual(run['feedback_repair']['accepted_repairs'], 0)
        self.assertEqual(run['feedback_repair']['stop_reason'], 'no_change_after_review')
        self.assertEqual(self.provider.call_count, 1)
        names = {row['path'] for row in run['artifacts']}
        self.assertIn('canonical/development_scenarios/scenarios.json', names)
        self.assertIn('canonical/feedback_attempts/001/proposal_reviews.json', names)
        prompt = json.loads(self.provider.call_args.args[1])
        self.assertEqual(prompt['development_scenarios']['symbol_meanings'], suite['symbol_meanings'])

    def test_partial_coverage_is_visible_and_does_not_block_model_emission(self):
        source, tlr, payload = fixture()
        other = {'id': 'R2', 'text': 'Voltage eventually stabilizes.',
                 'source': {'document': 'Synthetic source', 'location': 'section 2'}}
        payload['text'] = json.dumps({'requirements': [source, other]})
        payload['tlr']['requirements'].append({'id': 'R2', 'status': 'unsupported',
            'reason': 'Eventuality is outside the static profile.', 'reason_code': 'profile_limit'})
        run = self.post(payload).json()
        self.assertEqual(run['status'], 'completed', run)
        self.assertEqual(run['representation']['by_status']['supported'], 1)
        self.assertEqual(run['representation']['by_status']['unsupported'], 1)
        self.assertEqual(run['analysis']['consistency_status'], 'sat')
        self.assertEqual(run['analysis']['status'], 'unsupported')
        self.assertEqual(run['admission'], 'withheld')
        self.assertIn('Voltage eventually stabilizes.', run['model_text'])
        self.assertEqual(next(x for x in run['stages'] if x['id'] == 'analysis')['status'], 'partial')


if __name__ == '__main__':
    unittest.main()
