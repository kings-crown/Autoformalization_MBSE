"""Source-grounded feedback repairs endpoint meaning using declared scenarios."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import canonical_cli as cli
from test_canonical_scenarios import fixture


class SourceFeedbackScenarioTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('z3'), 'Local Z3 required')
    def test_source_endpoint_repair_uses_draft_audit_and_declared_scenarios(self):
        sources, candidate, suite = fixture()
        candidate['abstraction_policy'] = 'mbse_abstraction/1'
        candidate['requirements'][0]['formula']['op'] = '<'
        candidate['requirements'][0]['abstraction'] = {
            'kind': 'state_constraint', 'meaning': 'Battery source upper limit.',
            'scope': 'One observation.', 'limitations': []}
        calls = []
        def generate(system, prompt, model, directory, call_id):
            data = json.loads(prompt); calls.append(data)
            self.assertIsNotNone(data['solver_feedback'])
            self.assertNotIn('source_review', data)
            self.assertEqual(data['development_results']['status'], 'failed')
            current = data['current_tlr']
            revised = json.loads(json.dumps(current))
            revised['requirements'][0]['formula']['op'] = '<='
            return json.dumps({'schema': 'semantic_repair_proposal/1', 'tlr': revised,
                'reviews': [{'id': 'R1', 'outcome': 'changed',
                    'reason': 'The source permits the exact inclusive boundary.',
                    'source_basis': [{'source_id': 'R1', 'quote': sources[0]['text']}]}]})
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_cli._compile', return_value={'status': 'passed'}):
            result = cli.run_candidate(sources, Path(tmp) / 'run', 'C', model='fixture', tlr=candidate,
                generator=generate, feedback_repairs=1, development_scenarios=suite)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('source_review', result)
        self.assertEqual(result['representation']['constraints_emitted'], 1)
        self.assertEqual(result['configuration']['source_review_calls'], 0)
        self.assertEqual(result['feedback_repair']['accepted_repairs'], 1)
        self.assertEqual(result['analysis']['consistency_status'], 'sat')
        self.assertEqual(result['feedback_repair']['final_development_results']['status'], 'passed')


if __name__ == '__main__':
    unittest.main()
