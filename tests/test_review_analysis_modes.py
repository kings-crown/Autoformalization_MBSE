"""Explicit design-analysis opt-in; provider and compiler calls are controlled fixtures.

The local requirement and behavior checks still use Z3. No listening server or
model provider is started, and proposal-only runs must never invoke the checker.
"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from review_server import create_app
from review_behavior import validate_behavior
from review_profile import digest, interpret
from review_sysml import generate_sysml
from test_review_behavior_proposal import voltage_behavior


def reviewed(parent=None):
    record = {'reviewer': 'Mode boundary fixture', 'rationale': 'Reviewed independent dynamics, disturbance bounds and source-linked properties.',
              'acknowledge': True}
    if parent:
        record.update(parent_source_hash=parent['source_hash'], parent_evidence_hash=parent['evidence_hash'])
    return record


@unittest.skipUnless(shutil.which('z3'), 'Local Z3 unavailable')
class AnalysisModeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='analysis_modes_')
        self.root = Path(self.directory.name)
        self.app = create_app(self.root)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.client.__exit__, None, None, None)
        compiler = patch('review_server.compile_sysml', return_value={'status': 'passed', 'diagnostics': []})
        compiler.start()
        self.addCleanup(compiler.stop)
        actual_which = shutil.which
        installed = patch('review_server.shutil.which', side_effect=lambda name: '/controlled/codex' if name == 'codex' else actual_which(name))
        installed.start()
        self.addCleanup(installed.stop)
        self.proposal_requested = []
        self.addCleanup(self.app.state.executor.shutdown, wait=True, cancel_futures=True)

    def completed(self, **extra):
        payload = {'name': 'Explicit analysis fixture', 'text': 'battery.voltage <= 28 V', 'engine': 'local'}
        payload.update(extra)
        response = self.client.post('/api/runs', json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()['id']
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            run = self.client.get('/api/runs/' + run_id).json()
            if run['status'] in ('completed', 'failed'):
                self.assertEqual(run['status'], 'completed', run['errors'])
                return run
            time.sleep(.02)
        self.fail('Controlled run did not finish in twenty seconds')

    def generated(self, directory, name, requirements, progress, propose_behavior=False, *, unsolicited=False):
        self.proposal_requested.append(propose_behavior)
        tlr = interpret(requirements)
        model = generate_sysml(name, requirements, tlr)
        model['path'] = str(directory / 'Generated.sysml')
        Path(model['path']).write_text(model['text'])
        proposal = None
        if propose_behavior or unsolicited:
            proposal = {'status': 'proposed', 'origin': 'llm', 'summary': 'Synthetic additive disturbance candidate pending engineering review.',
                        'candidate': validate_behavior(voltage_behavior(), [r['id'] for r in requirements])}
            (directory / 'llm_behavior_proposal.json').write_text(json.dumps(proposal, indent=2) + '\n')
        return {'tlr': {'schema_version': 'review-pipeline-1', 'raw': tlr,
                        'requirements': [{**r, 'status': 'unsupported'} for r in requirements]},
                'analysis': {'status': 'partial', 'assumptions': []}, 'model': model,
                'behavior_proposal': proposal, 'errors': [], 'log': ''}

    def test_default_requirements_preserve_bound_without_design_check(self):
        with patch('review_behavior.analyze_behavior') as checker:
            run = self.completed()
        checker.assert_not_called()
        self.assertEqual(run['analysis_mode'], 'requirements')
        self.assertEqual(run['analysis']['status'], 'sat')
        self.assertIsNone(run['behavior'])
        self.assertIsNone(run['behavior_origin'])
        self.assertIsNone(run['design_review'])
        self.assertEqual(run['behavioral_analysis']['status'], 'not_run')
        self.assertEqual(run['behavioral_analysis']['checks'], [])
        self.assertIsNone(run['contracts']['behavior'])
        self.assertIn('<= 28', run['model']['text'])
        self.assertNotIn('FiniteTrace', run['model']['text'])
        stage = next(s for s in run['stages'] if s['id'] == 'behavior')
        self.assertEqual(stage['status'], 'not_run')
        self.assertIn('requirements', stage['detail'].lower())
        self.assertFalse((self.root / run['id'] / 'behavior.json').exists())

    def test_default_pipeline_cannot_install_or_check_unsolicited_proposal(self):
        def unexpected(*args, **kwargs):
            return self.generated(*args, **kwargs, unsolicited=True)
        with patch('review_pipeline_adapter.run_existing_pipeline', side_effect=unexpected), patch('review_behavior.analyze_behavior') as checker:
            run = self.completed(engine='pipeline')
        self.assertEqual(self.proposal_requested, [False])
        checker.assert_not_called()
        self.assertIsNone(run['behavior'])
        self.assertIsNone(run['behavior_origin'])
        self.assertEqual(run['behavioral_analysis']['status'], 'not_run')
        self.assertEqual(run['behavioral_analysis']['checks'], [])
        self.assertIsNone(run['contracts']['behavior'])
        self.assertNotIn('FiniteTrace', run['model']['text'])
        self.assertNotIn('VoltageRobustness', run['model']['text'])
        self.assertFalse((self.root / run['id'] / 'behavior.json').exists())

    def test_invalid_modes_and_missing_review_never_allocate_a_run(self):
        candidate = voltage_behavior()
        cases = [
            {'behavior': candidate},
            {'design_review': reviewed()},
            {'analysis_mode': 'automatic'},
            {'analysis_mode': 'propose_design', 'engine': 'local'},
            {'analysis_mode': 'propose_design', 'engine': 'pipeline', 'behavior': candidate},
            {'analysis_mode': 'propose_design', 'engine': 'pipeline', 'design_review': reviewed()},
            {'analysis_mode': 'check_design'},
            {'analysis_mode': 'check_design', 'behavior': candidate},
            {'analysis_mode': 'check_design', 'behavior': candidate, 'design_review': {**reviewed(), 'acknowledge': False}},
            {'analysis_mode': 'check_design', 'behavior': candidate, 'design_review': {**reviewed(), 'reviewer': '   '}},
            {'analysis_mode': 'check_design', 'behavior': candidate, 'design_review': {**reviewed(), 'rationale': '   '}},
            {'analysis_mode': 'check_design', 'behavior': {'schema': 'unknown'}, 'design_review': reviewed()},
        ]
        with patch('review_server.run_job') as job:
            for index, extra in enumerate(cases):
                with self.subTest(case=index):
                    response = self.client.post('/api/runs', json={'text': 'battery.voltage <= 28 V', **extra})
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(self.client.get('/api/runs').json()['runs'], [])
            job.assert_not_called()
        self.assertEqual(list(self.root.glob('run-*')), [])

    def test_proposal_waits_for_review_and_new_checked_candidate(self):
        with patch('review_pipeline_adapter.run_existing_pipeline', side_effect=self.generated), patch('review_behavior.analyze_behavior') as checker:
            parent = self.completed(engine='pipeline', analysis_mode='propose_design')
        self.assertEqual(self.proposal_requested, [True])
        checker.assert_not_called()
        self.assertIsNone(parent['behavior'])
        self.assertIsNone(parent['behavior_origin'])
        self.assertIsNone(parent['contracts']['behavior'])
        self.assertEqual(parent['behavioral_analysis']['status'], 'pending_review')
        self.assertEqual(parent['behavioral_analysis']['checks'], [])
        self.assertEqual(parent['behavior_proposal']['status'], 'proposed')
        self.assertNotIn('VoltageRobustness', parent['model']['text'])
        self.assertNotIn('FiniteTrace', parent['model']['text'])
        self.assertFalse((self.root / parent['id'] / 'behavior.json').exists())
        snapshot = {a['name']: (self.root / parent['id'] / a['name']).read_bytes() for a in parent['artifacts']}
        candidate = deepcopy(parent['behavior_proposal']['candidate'])
        next(v for v in candidate['variables'] if v['name'] == 'nominal')['value'] = '27'
        child = self.completed(analysis_mode='check_design', behavior=candidate, design_review=reviewed(parent),
                               parent_run_id=parent['id'], revision_rationale='Reviewed and replaced the speculative nominal value with a synthetic design value.')
        self.assertEqual(child['behavior_origin'], 'llm_proposed_reviewed')
        self.assertEqual(child['behavioral_analysis']['status'], 'bounded_pass')
        self.assertEqual(child['design_review']['behavior_sha256'], child['contracts']['behavior_sha256'])
        self.assertEqual(child['design_review']['source_hash'], child['source_hash'])
        self.assertEqual(child['design_review']['parent_evidence_hash'], parent['evidence_hash'])
        self.assertEqual(child['design_review']['proposal_run_id'], parent['id'])
        self.assertTrue(child['design_review']['proposal_sha256'])
        self.assertIn('FiniteTrace', child['model']['text'])
        self.assertIn('VoltageRobustness', child['model']['text'])
        for name, content in snapshot.items():
            self.assertEqual((self.root / parent['id'] / name).read_bytes(), content, name)
        unchanged = self.client.get('/api/runs/' + parent['id']).json()
        self.assertEqual(unchanged['behavioral_analysis']['status'], 'pending_review')
        self.assertIsNone(unchanged['behavior'])

    def test_reviewed_counterexample_still_reports_independent_design_violation(self):
        run = self.completed(analysis_mode='check_design', behavior=voltage_behavior(), design_review=reviewed())
        self.assertEqual(run['analysis']['status'], 'sat')
        self.assertEqual(run['behavioral_analysis']['status'], 'counterexample')
        self.assertTrue(run['behavioral_analysis']['checks'][0]['trace'])
        self.assertEqual(run['behavior_origin'], 'engineer_reviewed')
        self.assertEqual(run['design_review']['behavior_sha256'], run['contracts']['behavior_sha256'])
        self.assertEqual(run['design_review']['source_hash'], digest('battery.voltage <= 28 V'))

    def test_checked_revision_rejects_stale_or_missing_parent_snapshot(self):
        parent = self.completed()
        payload = {'text': 'battery.voltage <= 28 V', 'analysis_mode': 'check_design', 'behavior': voltage_behavior('27'),
                   'parent_run_id': parent['id'], 'revision_rationale': 'Inspect an independent design for the source constraint.'}
        for field in ('parent_source_hash', 'parent_evidence_hash'):
            for value in (None, 'stale'):
                with self.subTest(field=field, value=value):
                    review = reviewed(parent)
                    review[field] = value
                    response = self.client.post('/api/runs', json={**payload, 'design_review': review})
                    self.assertIn(response.status_code, (409, 422), response.text)
        self.assertEqual(len(self.client.get('/api/runs').json()['runs']), 1)
        self.assertEqual(len(list(self.root.glob('run-*'))), 1)
        artifact = self.root / parent['id'] / 'model.sysml'
        artifact.write_text(artifact.read_text() + '\n// changed since review\n')
        response = self.client.post('/api/runs', json={**payload, 'design_review': reviewed(parent)})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(len(list(self.root.glob('run-*'))), 1)
        self.assertEqual(self.client.get('/api/runs/' + parent['id']).json()['superseded_by'], [])


if __name__ == '__main__':
    unittest.main()
