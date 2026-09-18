"""Structured review API, immutable evidence, binding decisions and revision scope."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from review_server import create_app
from review_corrections import build_correction_summary
from review_contracts import _hash


def fixture():
    return json.loads((ROOT / 'examples/synthetic/contracts_voltage.json').read_text())


class StructuredReviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='structured_review_')
        self.root = Path(self.directory.name)
        self.compiler = patch('review_workflow.compile_sysml', return_value={'status': 'passed', 'diagnostics': []})
        self.compiler.start()
        self.app = create_app(self.root)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.compiler.stop()
        self.directory.cleanup()

    def load(self, run_id):
        response = self.client.get('/api/runs/' + run_id)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def completed(self, parent=None, limit='28', provenance=None):
        payload = fixture()
        payload['text'] = 'battery.voltage <= ' + limit + ' V'
        payload['behavior']['properties'][0]['predicate']['args'][1]['value'] = limit
        payload.update(analysis_mode='check_design', candidate_provenance=provenance or {},
                       design_review={'reviewer': 'Fixture engineer', 'rationale': 'Independent fixed voltage candidate reviewed.', 'acknowledge': True})
        if parent:
            payload.update(parent_run_id=parent['id'], revision_rationale='Review revised source limit and formal obligation.')
            payload['design_review'].update(parent_source_hash=parent['source_hash'], parent_evidence_hash=parent['evidence_hash'])
        response = self.client.post('/api/runs', json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            run = self.load(response.json()['id'])
            if run['status'] in ('completed', 'failed'):
                self.assertEqual(run['status'], 'completed', run.get('errors'))
                return run
            time.sleep(.03)
        self.fail('Run did not complete')

    def binding_payload(self, run, target, element):
        return {'source_hash': run['source_hash'], 'evidence_hash': run['evidence_hash'],
                'model_sha256': hashlib.sha256(run['model']['text'].encode()).hexdigest(),
                'architecture_binding_review_hash': run['architecture_binding_review_hash'],
                'target_kind': target['target_kind'], 'target_id': target['target_id'], 'element_id': element['id'],
                'decision': 'accept', 'reviewer': 'Fixture engineer', 'rationale': 'Reviewed exact domain declaration association.'}

    def accept_prerequisites(self, run):
        for kind, items, hash_key in [('assumptions', run['assumptions'], 'assumption_review_hash'),
                                      ('contracts', run['contracts']['contracts'], 'contract_review_hash')]:
            for item in items:
                response = self.client.post(f"/api/runs/{run['id']}/{kind}/{item['id']}/reviews", json={
                    'source_hash': run['source_hash'], 'evidence_hash': run['evidence_hash'], hash_key: run[hash_key],
                    'decision': 'accept', 'reviewer': 'Fixture engineer', 'rationale': 'Reviewed synthetic interpretation and premises.'})
                self.assertEqual(response.status_code, 201, response.text)
                run = self.load(run['id'])
        view = self.client.get(f"/api/runs/{run['id']}/bindings").json()
        attribute = next(e for e in view['choices'] if e['kind'] == 'attribute' and e.get('unit') == 'V')
        for target in view['status']['targets']:
            if target['required']:
                response = self.client.post(f"/api/runs/{run['id']}/bindings/reviews", json=self.binding_payload(run, target, attribute))
                self.assertEqual(response.status_code, 201, response.text)
                run = self.load(run['id'])
        return run

    def acceptance(self, run, **updates):
        payload = {'source_hash': run['source_hash'], 'evidence_hash': run['evidence_hash'],
                   'assumption_review_hash': run['assumption_review_hash'], 'contract_review_hash': run['contract_review_hash'],
                   'architecture_binding_review_hash': run['architecture_binding_review_hash'],
                   'correction_summary_sha256': (run.get('correction_summary') or {}).get('sha256'),
                   'decision': 'approve', 'reviewer': 'Fixture engineer', 'rationale': 'Review exact candidate and associations.',
                   'acknowledge': True, 'acknowledge_contract_changes': True}
        payload.update(updates)
        return self.client.post(f"/api/runs/{run['id']}/reviews", json=payload)

    def test_inspect_edit_and_provenance_do_not_run_tools_or_allocate_runs(self):
        f = fixture()
        payload = {'behavior': f['behavior'], 'text': f['text']}
        with patch('review_server.run_job') as job, patch('review_behavior.analyze_behavior') as solver:
            response = self.client.post('/api/behavior/inspect', json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            inspection = response.json()
            row = next(r for r in inspection['rows'] if r['pointer'] == '/transitions/0')
            provenance = {row['pointer']: {'expression_sha256': row['expression_sha256'], 'origin': 'design',
                                         'requirement_ids': [], 'rationale': 'Fixed regulator setting in this fixture.'}}
            edited = self.client.post('/api/behavior/edit', json={**payload, 'provenance': provenance,
                'edits': [{'pointer': '/transitions/0', 'expression': 'next(voltage) = 26[V]'}]})
            self.assertEqual(edited.status_code, 200, edited.text)
            self.assertEqual(edited.json()['inspection']['provenance']['/transitions/0']['origin'], 'unspecified')
            bad = self.client.post('/api/behavior/edit', json={**payload,
                'edits': [{'pointer': '/transitions/0', 'expression': "__import__('os').system('false')"}]})
            self.assertEqual(bad.status_code, 422)
            self.assertEqual(self.client.get('/api/runs').json()['runs'], [])
            job.assert_not_called()
            solver.assert_not_called()
        run = self.completed(provenance=provenance)
        self.assertEqual(run['candidate_provenance']['/transitions/0']['origin'], 'design')
        self.assertEqual(run['candidate_inspection']['behavior_sha256'], run['contracts']['behavior_sha256'])
        self.assertEqual(run['design_review']['candidate_provenance_sha256'], _hash(run['candidate_provenance']))
        self.assertIn('candidate_inspection.json', {a['name'] for a in run['artifacts']})
        self.assertEqual(run['integrity']['status'], 'passed')

    def test_binding_acceptance_stale_reviews_revoke_without_changing_evidence(self):
        run = self.completed()
        self.assertTrue(any('architecture binding' in b for b in run['baseline']['blockers']))
        self.assertEqual(self.acceptance(run).status_code, 409)
        snapshot = {a['name']: (self.root / run['id'] / a['name']).read_bytes() for a in run['artifacts']}
        run = self.accept_prerequisites(run)
        self.assertEqual(self.acceptance(run, architecture_binding_review_hash='stale').status_code, 409)
        accepted = self.acceptance(run)
        self.assertEqual(accepted.status_code, 201, accepted.text)
        self.assertTrue(accepted.json()['architecture_binding_decisions'])
        view = self.client.get(f"/api/runs/{run['id']}/bindings").json()
        target = next(t for t in view['status']['targets'] if t['target_kind'] == 'variable' and t['required'])
        element = next(e for e in view['choices'] if e['id'] == target['latest_review']['element_id'])
        payload = self.binding_payload(run, target, element)
        for key in ('source_hash', 'evidence_hash', 'model_sha256', 'architecture_binding_review_hash'):
            response = self.client.post(f"/api/runs/{run['id']}/bindings/reviews", json={**payload, key: 'stale'})
            self.assertEqual(response.status_code, 409, key)
        response = self.client.post(f"/api/runs/{run['id']}/bindings/reviews", json={**payload, 'decision': 'defer'})
        self.assertEqual(response.status_code, 201, response.text)
        deferred = self.load(run['id'])
        self.assertEqual(deferred['baseline']['status'], 'pending')
        self.assertEqual(self.acceptance(deferred).status_code, 409)
        self.assertEqual(deferred['evidence_hash'], run['evidence_hash'])
        for name, original in snapshot.items():
            self.assertEqual((self.root / run['id'] / name).read_bytes(), original)
        path = self.root / run['id'] / 'candidate_inspection.json'
        path.write_text(path.read_text() + ' ')
        self.assertEqual(self.client.post(f"/api/runs/{run['id']}/bindings/reviews", json=self.binding_payload(deferred, target, element)).status_code, 409)

    def test_weakened_revision_requires_its_exact_correction_summary(self):
        parent = self.completed()
        child = self.completed(parent, '29')
        summary = child['correction_summary']
        self.assertEqual(summary['sha256'], _hash({k: v for k, v in summary.items() if k != 'sha256'}))
        interpretation = next(c for c in summary['changes'] if c['category'] == 'interpretation')
        self.assertEqual(interpretation['status'], 'weakened')
        self.assertEqual(interpretation['evidence']['newly_permitted']['verdict'], 'sat')
        self.assertEqual({c['category'] for c in summary['changes']}, {'source', 'interpretation'})
        child = self.accept_prerequisites(child)
        self.assertEqual(self.acceptance(child, correction_summary_sha256=None).status_code, 409)
        self.assertEqual(self.acceptance(child, correction_summary_sha256='stale').status_code, 409)
        self.assertEqual(self.acceptance(child, acknowledge_contract_changes=False).status_code, 409)
        accepted = self.acceptance(child)
        self.assertEqual(accepted.status_code, 201, accepted.text)
        self.assertEqual(accepted.json()['correction_summary_snapshot'], summary)

    def test_binding_and_acceptance_reject_changed_snapshot_or_ledger(self):
        run = self.accept_prerequisites(self.completed())
        path = self.root / run['id'] / 'run.json'
        original = path.read_bytes()
        corruptions = [
            lambda r: r['model'].update(text=r['model']['text'] + '\n'),
            lambda r: r['behavior']['variables'][1].update(value='29'),
            lambda r: r['candidate_inspection'].update(behavior_sha256='stale'),
            lambda r: r['correction_summary'].update(summary='Changed after review'),
            lambda r: r['architecture_binding_reviews'].pop(),
            lambda r: r.pop('architecture_binding_review_hash'),
        ]
        for corrupt in corruptions:
            with self.subTest(corruption=corrupt):
                modified = json.loads(original)
                corrupt(modified)
                path.write_text(json.dumps(modified))
                self.assertEqual(self.client.get(f"/api/runs/{run['id']}/bindings").status_code, 409)
                self.assertEqual(self.acceptance(run).status_code, 409)
                path.write_bytes(original)

    def test_correction_categories_do_not_relabel_environment_as_design(self):
        parent = {'id': 'old', 'source_hash': 's', 'requirements': [], 'behavior': fixture()['behavior']}
        child = deepcopy(parent)
        child['behavior']['variables'][0]['bounds'] = {'upper': '28'}
        child['behavior']['transitions'] = []
        child['behavior']['horizon'] = 5
        summary = build_correction_summary(parent, child, {})
        self.assertEqual({r['category'] for r in summary['changes']}, {'design', 'environment', 'scope'})
        self.assertTrue(any('hide counterexamples' in r['explanation'] for r in summary['changes']))


if __name__ == '__main__':
    unittest.main()
