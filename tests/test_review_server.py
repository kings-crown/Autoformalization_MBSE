"""Real API/solver/compiler lifecycle and review-boundary integration tests."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from review_server import create_app
from review_sysml import compiler_capability
from review_profile import SAMPLE


def design_review(parent=None):
    record = {'reviewer': 'Fixture engineer', 'acknowledge': True,
              'rationale': 'Inspected the independent synthetic dynamics, bounds and checked properties.'}
    if parent:
        record.update(parent_source_hash=parent['source_hash'], parent_evidence_hash=parent['evidence_hash'])
    return record


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        if not compiler_capability()['available']:
            self.skipTest('SysML pilot compiler unavailable')
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.directory.name))
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        if hasattr(self, 'client'):
            self.client.__exit__(None, None, None)
            self.directory.cleanup()

    def completed(self, payload):
        response = self.client.post('/api/runs', json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()['id']
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            run = self.client.get('/api/runs/' + run_id).json()
            if run['status'] in ('completed', 'failed'):
                return run
            time.sleep(.05)
        self.fail('Run did not complete in time')

    def review(self, run, **overrides):
        payload = {'reviewer': 'Integration test reviewer', 'decision': 'approve',
                   'rationale': 'Test acceptance of this fixture only.', 'scope': 'Prototype fixture interpretation',
                   'source_hash': run['source_hash'], 'evidence_hash': run['evidence_hash'], 'acknowledge': True,
                   'assumption_review_hash': run.get('assumption_review_hash'),
                   'contract_review_hash': run.get('contract_review_hash'),
                   'architecture_binding_review_hash': run.get('architecture_binding_review_hash'),
                   'correction_summary_sha256': (run.get('correction_summary') or {}).get('sha256'),
                   'acknowledge_contract_changes': bool(run.get('parent_run_id'))}
        payload.update(overrides)
        return self.client.post(f"/api/runs/{run['id']}/reviews", json=payload)

    def decide_assumption(self, run, assumption_id, decision='accept', **overrides):
        payload = {'source_hash': run['source_hash'], 'evidence_hash': run['evidence_hash'],
                   'assumption_review_hash': run['assumption_review_hash'], 'reviewer': 'Fixture engineer',
                   'decision': decision, 'rationale': 'Explicit test premise review; synthetic fixture only.'}
        payload.update(overrides)
        return self.client.post(f"/api/runs/{run['id']}/assumptions/{assumption_id}/reviews", json=payload)

    def accept_assumptions(self, run):
        for assumption in run['assumptions']:
            response = self.decide_assumption(run, assumption['id'])
            self.assertEqual(response.status_code, 201, response.text)
            run = self.client.get('/api/runs/' + run['id']).json()
        return run

    def accept_contracts(self, run):
        for contract in run['contracts']['contracts']:
            response = self.client.post(f"/api/runs/{run['id']}/contracts/{contract['id']}/reviews", json={
                'source_hash': run['source_hash'], 'evidence_hash': run['evidence_hash'],
                'contract_review_hash': run['contract_review_hash'], 'reviewer': 'Fixture engineer',
                'decision': 'accept', 'rationale': 'Synthetic source, contract and proposed change inspected.'})
            self.assertEqual(response.status_code, 201, response.text)
            run = self.client.get('/api/runs/' + run['id']).json()
        return run

    def test_conflict_revision_acceptance_and_exact_export(self):
        parent = self.completed({**SAMPLE, 'engine': 'local'})
        self.assertEqual(parent['status'], 'completed', parent['errors'])
        self.assertEqual(parent['analysis']['status'], 'unsat')
        self.assertEqual(set(parent['analysis']['unsat_core']), {'REQ-042', 'REQ-087'})
        self.assertEqual(parent['compilation']['status'], 'passed')
        self.assertEqual(self.review(parent).status_code, 409)
        self.assertEqual(self.review(parent, decision='defer').status_code, 201)
        requirements = parent['requirements']
        requirements[0]['text'] = requirements[0]['text'].replace('5 seconds', '10 seconds')
        child = self.completed({'name': 'Revised fixture', 'engine': 'local', 'format': 'json',
                                'text': json.dumps(requirements), 'parent_run_id': parent['id'],
                                'revision_rationale': 'Test a ten-second response window; fixture change only.'})
        self.assertEqual(child['analysis']['status'], 'sat')
        self.assertEqual(child['compilation']['status'], 'passed')
        self.assertEqual(child['reviews'], [])
        self.assertNotEqual(parent['source_hash'], child['source_hash'])
        self.assertNotEqual(parent['evidence_hash'], child['evidence_hash'])
        self.assertEqual(child['requirements'][0]['owner'], parent['requirements'][0]['owner'])
        self.assertEqual(self.review(child, source_hash=parent['source_hash']).status_code, 409)
        self.assertEqual(self.review(child, evidence_hash=parent['evidence_hash']).status_code, 409)
        self.assertEqual(self.review(child, acknowledge=False).status_code, 409)
        self.assertEqual(self.review(child).status_code, 409)
        child = self.accept_contracts(self.accept_assumptions(child))
        record = self.review(child)
        self.assertEqual(record.status_code, 201, record.text)
        self.assertFalse(record.json()['source_change_authorized'])
        updated = self.client.get('/api/runs/' + child['id']).json()
        self.assertEqual(updated['baseline']['status'], 'approved')
        parent_after = self.client.get('/api/runs/' + parent['id']).json()
        self.assertEqual(parent_after['baseline']['status'], 'superseded')
        self.assertIn('5 seconds', parent_after['requirements'][0]['text'])
        packet = self.client.get(f"/api/runs/{child['id']}/packet")
        self.assertEqual(packet.status_code, 200)
        data = packet.json()
        self.assertIn('model.sysml', data['files'])
        self.assertIn('constraints.smt2', data['files'])
        self.assertEqual(data['files']['model.sysml']['text'], child['model']['text'])
        download = self.client.get(f"/api/runs/{child['id']}/artifacts/model.sysml")
        self.assertEqual(download.text, child['model']['text'])
        self.assertEqual(len(updated['reviews']), 1)

    def test_unsupported_clause_stays_visible_and_blocks_acceptance(self):
        run = self.completed({'name': 'Unsupported fixture', 'format': 'text', 'engine': 'local',
                              'text': 'battery.voltage >= 10.5 V\nThe controller shall respond safely.'})
        self.assertEqual(run['status'], 'completed', run['errors'])
        self.assertEqual(run['analysis']['status'], 'partial')
        self.assertIn('respond safely', run['model']['text'])
        self.assertEqual(run['compilation']['status'], 'passed')
        self.assertEqual(self.review(run).status_code, 409)
        self.assertEqual(self.review(run, decision='recommend').status_code, 201)

    def test_changed_artifact_blocks_download_export_and_review(self):
        run = self.completed({'name': 'Integrity fixture', 'format': 'text', 'text': 'battery.voltage >= 10.5 V'})
        model = Path(self.directory.name) / run['id'] / 'model.sysml'
        model.write_text(model.read_text() + '\n// external edit\n')
        self.assertEqual(self.review(run).status_code, 409)
        self.assertEqual(self.client.get(f"/api/runs/{run['id']}/artifacts/model.sysml").status_code, 409)
        self.assertEqual(self.client.get(f"/api/runs/{run['id']}/packet").status_code, 409)
        self.assertEqual(self.client.get(f"/api/runs/{run['id']}/artifacts/not-a-file.txt").status_code, 404)

    def test_assumption_decisions_are_scoped_stale_checked_and_reopen_acceptance(self):
        run = self.completed({'name': 'Assumption fixture', 'text': 'battery.voltage >= 10.5 V'})
        initial_hash = run['assumption_review_hash']
        assumption = run['assumptions'][0]
        self.assertEqual(self.decide_assumption(run, assumption['id'], evidence_hash='stale').status_code, 409)
        self.assertEqual(self.decide_assumption(run, 'not-present').status_code, 404)
        self.assertEqual(self.decide_assumption(run, assumption['id'], assumption_review_hash=None).status_code, 422)
        self.assertEqual(self.review(run).status_code, 409)
        accepted = self.accept_contracts(self.accept_assumptions(run))
        self.assertEqual(self.review(accepted, assumption_review_hash=initial_hash).status_code, 409)
        self.assertEqual(self.review(accepted).status_code, 201)
        self.assertEqual(self.decide_assumption(accepted, assumption['id'], 'reject').status_code, 201)
        changed = self.client.get('/api/runs/' + run['id']).json()
        self.assertEqual(changed['baseline']['status'], 'pending')
        self.assertEqual(changed['assumptions'], run['assumptions'])
        self.assertEqual(changed['evidence_hash'], run['evidence_hash'])
        self.assertEqual(len(changed['reviews']), 1)
        self.assertEqual(self.review(changed).status_code, 409)
        packet = self.client.get(f"/api/runs/{run['id']}/packet").json()
        self.assertEqual(packet['run']['assumption_reviews'][-1]['decision'], 'reject')
        self.assertIn('assumptions.json', packet['files'])
        self.assertEqual(packet['run']['reviews'][0]['assumption_decisions'][-1]['decision'], 'accept')

    def test_behavior_schema_is_validated_before_allocating_a_run(self):
        response = self.client.post('/api/runs', json={'text': 'battery.voltage >= 1 V', 'behavior': {'schema': 'unknown'},
                                                      'analysis_mode': 'check_design', 'design_review': design_review()})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.get('/api/runs').json()['runs'], [])

    def test_behavior_counterexample_model_only_revision_and_inspection_links(self):
        from test_review_behavior_proposal import voltage_behavior
        parent = self.completed({'name': 'Robustness fixture', 'text': 'battery.voltage <= 28 V',
                                 'behavior': voltage_behavior(), 'analysis_mode': 'check_design',
                                 'design_review': design_review()})
        self.assertEqual(parent['status'], 'completed', parent['errors'])
        self.assertEqual(parent['analysis']['status'], 'sat')
        self.assertEqual(parent['behavioral_analysis']['status'], 'counterexample')
        self.assertEqual(parent['behavioral_analysis']['checks'][0]['verdict'], 'counterexample')
        self.assertTrue(parent['behavioral_analysis']['checks'][0]['trace'])
        self.assertEqual(parent['compilation']['status'], 'passed')
        req_element = next(e for e in parent['model']['inspection']['elements'] if e.get('source_requirement_ids') == ['REQ-001'])
        self.assertIn('VoltageRobustness', req_element['property_ids'])
        self.assertTrue(req_element['assumption_ids'])
        child = self.completed({'name': 'Revised fixed design', 'text': 'battery.voltage <= 28 V',
                                'behavior': voltage_behavior('27'), 'parent_run_id': parent['id'],
                                'analysis_mode': 'check_design', 'design_review': design_review(parent),
                                'revision_rationale': 'Synthetic design parameter revision; source unchanged.'})
        self.assertEqual(child['source_hash'], parent['source_hash'])
        self.assertNotEqual(child['evidence_hash'], parent['evidence_hash'])
        self.assertEqual(child['behavioral_analysis']['status'], 'bounded_pass')
        self.assertEqual(child['assumption_reviews'], [])
        self.assertEqual(self.review(child).status_code, 409)
        self.assertEqual(child['integrity']['status'], 'passed', child['integrity'])
        packet = self.client.get(f"/api/runs/{child['id']}/packet").json()
        self.assertIn('behavior.json', packet['files'])
        self.assertIn('behavioral_analysis.json', packet['files'])
        self.assertIn('model_inspection.json', packet['files'])

    def test_validation_and_local_origin_guard(self):
        self.assertEqual(self.client.post('/api/runs', json={'text': 'id,text\nR1,a\nR1,b', 'format': 'csv'}).status_code, 422)
        self.assertEqual(self.client.post('/api/runs', json={'text': 'battery.voltage >= 1 V'},
                                         headers={'Origin': 'https://unrelated.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/runs/not-a-run').status_code, 404)


if __name__ == '__main__':
    unittest.main()
