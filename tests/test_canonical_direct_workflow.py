"""End-to-end boundaries of direct TLR conversion; provider calls are mocked."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import canonical_cli as cli
from canonical_tlr import validate_tlr
from test_canonical_feedback_execution import fixture, proposal, corrected


class DirectWorkflowTests(unittest.TestCase):
    def test_context_reaches_generation_without_an_inventory_or_review_veto(self):
        sources, tlr = fixture()
        sources[0]['source'] = {'context': {'project_note': 'Demonstrate the feature by phase II.'}}
        generator = Mock(return_value=json.dumps(tlr))
        with tempfile.TemporaryDirectory() as tmp, \
                patch('canonical_obligations.prepare_inventory', side_effect=AssertionError('No inventory calls')), \
                patch('canonical_source_review.review_candidate', side_effect=AssertionError('No review gate')):
            directory = Path(tmp)/'B'
            result = cli.run_candidate(sources, directory, 'B', generator=generator, compile_model=False)
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(generator.call_count, 1)
            self.assertEqual(json.loads(generator.call_args.args[1])['requirements'], sources)
            self.assertEqual(result['representation']['emitted_requirement_ids'], ['R1','R2'])
            self.assertEqual((directory/'model.sysml').read_text().count('require constraint obligation'), 2)
            self.assertFalse((directory/'obligation_inventory').exists())
            self.assertFalse((directory/'source_review').exists())
            self.assertEqual(result['assurance']['source_to_sysml']['status'], 'not_assessed')

    def test_source_change_is_rejected_and_last_valid_model_retained(self):
        sources, tlr = fixture()
        raw = proposal(sources, tlr, corrected(tlr))
        raw['tlr']['requirements'][0]['text'] = 'A replacement stakeholder requirement.'
        with tempfile.TemporaryDirectory() as tmp:
            result = cli.run_candidate(sources, Path(tmp)/'B', 'B', tlr=tlr,
                generator=Mock(return_value=json.dumps(raw)), feedback_repairs=1, compile_model=False)
            self.assertEqual(result['configuration']['semantic_repairs'], 0)
            self.assertEqual(result['tlr'], validate_tlr(tlr, sources))
            self.assertEqual(result['feedback_repair']['steps'][0]['status'], 'rejected')

    @unittest.skipUnless(shutil.which('z3'), 'Requires real Z3')
    def test_genuine_source_conflict_remains_visible_after_review(self):
        sources, tlr = fixture()
        sources[1]['text'] = 'Battery voltage shall be at least 30 V.'
        generator = Mock(return_value=json.dumps(proposal(sources, tlr, tlr)))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'C'
            result = cli.run_candidate(sources, path, 'C', tlr=tlr,
                generator=generator, feedback_repairs=1, compile_model=False)
            self.assertEqual(result['analysis']['consistency_status'], 'unsat')
            self.assertEqual(result['admission'], 'withheld')
            self.assertTrue((path/'model.sysml').exists())
            self.assertEqual(result['configuration']['semantic_repairs'], 0)
            prompt = json.loads(generator.call_args.args[1])
            self.assertEqual(prompt['solver_feedback']['consistency_status'], 'unsat')
            self.assertEqual(prompt['source_packet'], sources)
            self.assertNotIn('obligation_inventory', prompt)
            self.assertNotIn('judge_verdicts', prompt)

    def test_generated_abstraction_is_revisable_without_changing_source(self):
        sources, tlr = fixture()
        updated = deepcopy(tlr)
        updated['requirements'][0]['abstraction']['meaning'] = 'Battery terminal voltage may equal 28 V and cannot exceed it.'
        generator = Mock(return_value=json.dumps(proposal(sources, tlr, updated)))
        with tempfile.TemporaryDirectory() as tmp:
            result = cli.run_candidate(sources, Path(tmp)/'B', 'B', tlr=tlr,
                generator=generator, feedback_repairs=1, compile_model=False)
            self.assertEqual(result['configuration']['semantic_repairs'], 1)
            self.assertEqual(result['tlr'], validate_tlr(updated, sources))
            self.assertEqual(result['assurance']['source_to_rule']['status'], 'embedded_review_recorded')
            self.assertEqual(result['source_fidelity'], 'not_assessed')

    def test_unusable_feedback_is_not_reported_as_a_recorded_semantic_review(self):
        sources, tlr = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            result = cli.run_candidate(sources, Path(tmp)/'B', 'B', tlr=tlr,
                generator=Mock(return_value='malformed'), feedback_repairs=1, compile_model=False)
            self.assertEqual(result['configuration']['semantic_repairs'], 0)
            self.assertEqual(result['assurance']['source_to_rule']['status'], 'unavailable')
            self.assertEqual(result['assurance']['source_to_sysml']['status'], 'not_assessed')

    def test_old_gate_requests_fail_explicitly_instead_of_silently_disabling_them(self):
        sources, tlr = fixture()
        for option in ({'require_obligation_inventory':True}, {'review_repairs':1}, {'reviewer':Mock()}):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(option=list(option)):
                path = Path(tmp)/'B'
                with self.assertRaisesRegex(ValueError, 'retired inventory/review gate'):
                    cli.run_candidate(sources, path, 'B', tlr=tlr, **option)
                self.assertFalse(path.exists())

    def test_study_format_recovery_is_shared_and_counted_once(self):
        sources, tlr = fixture()
        calls = []
        def generate(system,prompt,model,directory,call_id):
            calls.append(call_id)
            if call_id == 'generation': return '{}'
            if call_id == 'format_correction': return json.dumps(tlr)
            current = json.loads(prompt)['current_tlr']
            return json.dumps(proposal(sources,current,current))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'study'
            with patch('canonical_audits.audit_tlr', return_value={'schema':'canonical_audits/1',
                    'status':'partial','admitted':False,'background_status':'not_run',
                    'consistency_status':'not_run','requirements':[],'findings':[],'inconclusive_checks':[]}):
                result = cli.run_study(sources,path,repetitions=1,a_sysml='package Fixture {}',
                    generator=generate,format_repairs=1,feedback_repairs=1,compile_model=False)
            config = cli.read_json(path/'study_configuration.json')
            self.assertEqual(calls.count('generation'),1)
            self.assertEqual(calls.count('format_correction'),1)
            self.assertEqual(config['format_repair_calls'],1)
            self.assertEqual(config['actual_model_transport_invocations'],len(calls))
            self.assertEqual(result['rows'][0]['B']['tlr'],result['rows'][0]['C']['tlr'])


if __name__ == '__main__': unittest.main()
