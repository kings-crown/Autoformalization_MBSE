"""Read-only candidate presentation must preserve historical evidence and approval gates."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from review_server import create_app, interpretation_presentation, baseline_blockers


class InterpretationViewTests(unittest.TestCase):
    def fixture(self, root):
        run_id = 'run-1234567890abcdef'
        directory = Path(root) / run_id
        directory.mkdir()
        fragment = '''(declare-const voice_service Bool)
(define-fun requirement_holds () Bool
  voice_service)
(declare-const en_R1 Bool)
(assert (! (=> en_R1 requirement_holds) :named req_R1))
(assert en_R1)
(check-sat)
'''
        (directory / 'pipeline_model_sat.smt2').write_text(fragment)
        req = {'id': 'R1', 'text': 'The network shall provide voice services.'}
        typed = {**req, 'symbols': [{'name': 'requirement_holds', 'type': 'Bool'}], 'ranges': []}
        run = {'id': run_id, 'engine': 'pipeline', 'name': 'Historical fixture', 'status': 'completed',
               'created_at': '2026-09-15T00:00:00+00:00', 'requirements': [req],
               'tlr': {'raw': {'requirements': [typed]}, 'requirements': [{**typed, 'raw': typed, 'status': 'unsupported'}]},
               'analysis': {'status': 'partial', 'solver_status': 'sat', 'unsupported_ids': ['R1'], 'checked_ids': ['R1']},
               'artifacts': [{'name': 'pipeline_model_sat.smt2', 'sha256': hashlib.sha256(fragment.encode()).hexdigest()}],
               'source_hash': 'source-fixture', 'evidence_hash': 'evidence-fixture', 'assumption_review_hash': 'review-fixture',
               'assumptions': [], 'assumption_reviews': [], 'reviews': [], 'errors': [],
               'model': {'text': 'package Fixture {}'}, 'compilation': {'status': 'passed'}}
        (directory / 'run.json').write_text(json.dumps(run))
        return run, directory

    def test_get_presents_candidate_without_rewriting_historical_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_app(Path(root))
            with TestClient(app) as client:
                run, directory = self.fixture(root)
                original = (directory / 'run.json').read_bytes()
                response = client.get('/api/runs/' + run['id'])
                self.assertEqual(response.status_code, 200)
                displayed = response.json()
                row = displayed['interpretation_view']['requirements'][0]
                self.assertEqual(row['status'], 'pending_review')
                self.assertIn('requirement_holds', row['candidate_assertion']['expression'])
                self.assertTrue(any('voice_service' in definition['command'] for definition in row['referenced_definitions']))
                self.assertEqual(displayed['tlr']['requirements'][0]['status'], 'unsupported')
                self.assertEqual(displayed['analysis'], run['analysis'])
                for field in ['evidence_hash', 'source_hash', 'assumption_review_hash', 'reviews']:
                    self.assertEqual(displayed[field], run[field])
                self.assertEqual((directory / 'run.json').read_bytes(), original)

    def test_changed_smt_cannot_be_presented_as_recorded_candidate(self):
        with tempfile.TemporaryDirectory() as root:
            run, directory = self.fixture(root)
            (directory / 'pipeline_model_sat.smt2').write_text('(assert false)\n(check-sat)')
            view = interpretation_presentation(run, directory)
            self.assertTrue(view['diagnostics'])
            self.assertEqual(view['pending_review_ids'], [])
            self.assertEqual(view['requirements'][0]['status'], 'needs_interpretation')
            self.assertIsNone(view['requirements'][0]['candidate_assertion'])

    def test_status_correction_does_not_approve_pipeline_or_relabel_local_unsupported(self):
        with tempfile.TemporaryDirectory() as root:
            run, directory = self.fixture(root)
            view = interpretation_presentation(run, directory)
            run['tlr']['requirements'] = view['requirements']
            run['analysis']['status'] = 'sat'
            blockers = baseline_blockers(run)
            self.assertTrue(any('await engineering review' in blocker for blocker in blockers))
            self.assertTrue(any('semantic alignment' in blocker for blocker in blockers))
            run['engine'] = 'local'
            run['tlr']['requirements'][0]['status'] = 'unsupported'
            self.assertIsNone(interpretation_presentation(run, directory))
            self.assertEqual(run['tlr']['requirements'][0]['status'], 'unsupported')
