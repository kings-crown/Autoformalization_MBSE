"""Historical gated replay stays bound; current draft extraction needs no review calls."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import mutation_sources as mutation
from canonical_source_review import POLICY_VERSION, REPORT_SCHEMA
from canonical_tlr import validate_tlr
from source_review_support import pass_source_review, review_response
from test_mutation_stress import manifest


def fixture(directory, with_inventory=False):
    campaign = mutation.validate_manifest(manifest())
    packet = mutation.source_packet(campaign, None)
    tlr = validate_tlr({'schema': 'mbse_tlr/1', 'abstraction_policy': 'mbse_abstraction/1',
        'variables': [{**deepcopy(campaign['context']['variables'][0]), 'description': 'Number of supported languages.'}],
        'assumptions': [], 'requirements': [{'id': 'R1', 'status': 'supported',
            'formula': deepcopy(campaign['requirements'][0]['formula']),
            'abstraction': {'kind': 'state_constraint', 'meaning': packet[0]['text'],
                            'scope': 'Supported language count.', 'limitations': []}}]}, packet)
    inventory, preparation = None, None
    if with_inventory:
        from canonical_obligations import (POLICY_VERSION as INVENTORY_POLICY,
            REPORT_SCHEMA as INVENTORY_REPORT_SCHEMA, validate_inventory)
        inventory = {'schema': 'mbse_obligation_inventory/1', 'requirements': [
            {'id': 'R1', 'context': [], 'obligations': [{
                'id': 'R1.O1', 'meaning': packet[0]['text'],
                'source_basis': [{'source_id': 'R1', 'quote': packet[0]['text']}],
                'kind': 'state_constraint', 'slots': {'subject': 'Controller', 'scope': 'Language support',
                    'quantity': 'Supported language count', 'unit': '1', 'operator': '>=', 'bound': '10'},
                'selection_reason': 'Explicit minimum count.', 'limitations': []}]}]}
        tlr['requirements'][0]['coverage'] = [{'obligation_id': 'R1.O1', 'status': 'represented',
            'formula_path': '/formula', 'slots': {'quantity': ['/formula/args/0'], 'operator': ['/formula'],
                'bound': ['/formula/args/1'], 'unit': ['/formula/args/1']}}]
        inventory = validate_inventory(inventory, packet)
        preparation = {'schema': INVENTORY_REPORT_SCHEMA, 'policy': INVENTORY_POLICY,
            'inventory': deepcopy(inventory), 'binding': {'policy': INVENTORY_POLICY,
                'source_packet': deepcopy(packet), 'inventory': deepcopy(inventory), 'model': 'review-fixture'},
            'response': json.loads(pass_source_review('', json.dumps({'source_packet': packet,
                'obligation_inventory': inventory}), 'review-fixture', None, 'fixture'))}
    # Construct a retained historical report directly: this replay fixture makes
    # no generation, inventory-preparation, or review transport calls.
    tlr = validate_tlr(tlr, packet)
    payload = {'source_packet': packet, 'candidate_tlr': tlr, 'obligation_inventory': inventory}
    report = {'schema': REPORT_SCHEMA, 'policy': POLICY_VERSION,
        'binding': {'policy': POLICY_VERSION, 'candidate_tlr': deepcopy(tlr),
            'source_packet': deepcopy(packet), 'fixed_context': deepcopy(campaign['context']),
            'reviewer_model': 'review-fixture', 'obligation_inventory': deepcopy(inventory)},
        'response': review_response(payload)}
    run = {'tlr': tlr, 'sources': packet, 'source_review': report,
           'configuration': {'source_review_policy': POLICY_VERSION, 'source_review_model': 'review-fixture'}}
    if with_inventory:
        run.update(obligation_inventory=inventory, obligation_preparation=preparation)
        run['configuration']['obligation_inventory_required'] = True
    return campaign, packet, run


class MutationSourceReviewTests(unittest.TestCase):
    def test_inventory_bound_component_reviews_revalidate_for_mutation_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            campaign, packet, run = fixture(tmp, with_inventory=True)
            formulas, reasons = mutation.extract_canonical_formulas(campaign, packet, run)
        self.assertEqual(formulas['R1'], run['tlr']['requirements'][0]['formula'])
        self.assertEqual(reasons, {})

    def test_missing_changed_or_rejected_inventory_preparation_cannot_authorize_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            campaign, packet, baseline = fixture(tmp, with_inventory=True)
            for defect in ('missing_inventory', 'missing_preparation', 'changed_source', 'changed_inventory', 'rejected_review'):
                run = deepcopy(baseline)
                if defect == 'missing_inventory':
                    run['source_review']['binding'].pop('obligation_inventory')
                elif defect == 'missing_preparation':
                    run.pop('obligation_preparation')
                elif defect == 'changed_source':
                    run['obligation_preparation']['binding']['source_packet'][0]['text'] += ' Modified.'
                elif defect == 'changed_inventory':
                    run['obligation_inventory']['requirements'][0]['obligations'][0]['slots']['bound'] = '9'
                else:
                    run['obligation_preparation']['response']['requirements'][0]['disposition'] = 'revise'
                    run['obligation_preparation'].update(status='passed', complete=True)
                with self.subTest(defect=defect):
                    formulas, reasons = mutation.extract_canonical_formulas(campaign, packet, run)
                    self.assertIsNone(formulas['R1'])
                    self.assertIn('obligation preparation', reasons['R1'])

    def test_exact_reviewed_formula_is_available_to_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            campaign, packet, run = fixture(tmp)
            formulas, reasons = mutation.extract_canonical_formulas(campaign, packet, run)
        self.assertEqual(formulas['R1'], run['tlr']['requirements'][0]['formula'])
        self.assertEqual(reasons, {})

    def test_old_source_candidate_background_or_reviewer_binding_never_authorizes_a_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            campaign, packet, baseline = fixture(tmp)
            for mismatch in ('source', 'formula', 'background', 'model', 'missing', 'failure', 'policy'):
                run = deepcopy(baseline)
                if mismatch == 'source': run['source_review']['binding']['source_packet'][0]['text'] += ' Altered source.'
                elif mismatch == 'formula': run['source_review']['binding']['candidate_tlr']['requirements'][0]['formula']['op'] = '>'
                elif mismatch == 'background': run['source_review']['binding']['fixed_context']['variables'][0]['bounds']['lower'] = '1'
                elif mismatch == 'model': run['source_review']['binding']['reviewer_model'] = 'another-model'
                elif mismatch == 'missing': run.pop('source_review')
                elif mismatch == 'failure': run['source_review']['failure'] = {'kind': 'transport_error'}
                else: run['source_review']['policy'] = 'future-or-invalid-policy'
                with self.subTest(mismatch=mismatch):
                    formulas, reasons = mutation.extract_canonical_formulas(campaign, packet, run)
                    self.assertIsNone(formulas['R1'])
                    self.assertIn('Source-to-rule', reasons['R1'])

    def test_forged_eligible_summary_does_not_override_rejected_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            campaign, packet, run = fixture(tmp)
            record = run['source_review']['response']['requirements'][0]
            record['disposition'] = 'revise'
            record['reason'] = 'The source review rejects this interpretation; do not use its formula.'
            run['source_review'].update(eligible_ids=['R1'], complete=True, status='passed')
            formulas, reasons = mutation.extract_canonical_formulas(campaign, packet, run)
        self.assertIsNone(formulas['R1'])
        self.assertIn('withheld', reasons['R1'])

    def test_missing_or_malformed_response_is_not_replaced_by_pass_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            campaign, packet, baseline = fixture(tmp)
            for response in (None, {}, {'schema': 'source_rule_review/1', 'requirements': []}):
                run = deepcopy(baseline); run['source_review']['response'] = response
                with self.subTest(response=response):
                    formulas, reasons = mutation.extract_canonical_formulas(campaign, packet, run)
                    self.assertIsNone(formulas['R1'])
                    self.assertIn('withheld', reasons['R1'])

    def test_historical_ungated_replay_remains_explicitly_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            campaign, packet, run = fixture(tmp)
            run.pop('source_review'); run['configuration'] = {}
            formulas, reasons = mutation.extract_canonical_formulas(campaign, packet, run)
        self.assertIsNotNone(formulas['R1'])
        self.assertEqual(reasons, {})

    def test_current_not_run_review_marker_needs_no_historical_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            campaign, packet, run = fixture(tmp)
            run['source_review'] = {'status': 'not_run'}
            run['configuration'] = {'source_review_required': False, 'source_review_mode': 'embedded'}
            formulas, reasons = mutation.extract_canonical_formulas(campaign, packet, run)
        self.assertEqual(formulas['R1'], run['tlr']['requirements'][0]['formula'])
        self.assertEqual(reasons, {})

    def test_declared_bounds_include_only_generation_and_single_feedback_loop(self):
        for budget in ({}, {'abstention_repairs': 2}, {'feedback_repairs': 2}, {'engine': 'local'}):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(mutation, '_generate_sample', return_value={}), \
                    patch.object(mutation, '_evaluate_samples', side_effect=lambda m, c, o, meta, t, z: meta):
                result = mutation.run_source(manifest(), Path(tmp) / 'campaign', **budget)
                config = result['configuration']
                expected = 0 if budget.get('engine') == 'local' else 1 + budget.get('abstention_repairs', 0) + budget.get('feedback_repairs', 0)
                self.assertEqual(config['max_source_review_calls_per_workflow'], 0)
                self.assertEqual(config['max_obligation_preparation_calls_per_workflow'], 0)
                self.assertEqual(config['max_model_transport_invocations_per_workflow'], expected)
                self.assertEqual(config['max_model_transport_invocations'], 2 * expected)


if __name__ == '__main__':
    unittest.main()
