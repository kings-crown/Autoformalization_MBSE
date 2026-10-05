"""Pattern recovery must not bypass source review, immutable inputs or budgets."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from canonical_patterns import compile_capability, evaluate_capability, expand_proposal
from canonical_feedback import validate_feedback_proposal
from test_canonical_feedback import fixture, envelope
from test_canonical_feedback_execution import pass_source_review


def proposal():
    sources, before = fixture()
    raw = envelope(sources, before)
    basis = [{'source_id': 'R2', 'quote': sources[1]['text']}]
    diagnosis = {'id': 'R2', 'decision': 'capability_candidate',
        'reason': 'Availability is required; implementation scheduling is not specified or required.',
        'source_basis': basis, 'realization_details': ['Message scheduling algorithm.'],
        'missing_slots': [], 'unsupported_operators': [], 'alternatives': []}
    binding = {'id': 'R2', 'subject': 'library', 'operation': 'send unicast messages',
        'scope': 'Availability of the named operation, without occurrence or delivery guarantees.',
        'symbol': 'unicast_available', 'meaning': 'The library offers unicast sending.',
        'limitations': ['Does not establish invocation or successful delivery.'],
        'whole_obligation': True, 'residual_obligations': [], 'source_basis': basis}
    raw.update(abstention_diagnostics=[diagnosis], capability_bindings=[binding])
    raw['reviews'][1].update(outcome='changed', source_basis=basis,
        reason='Preserve the complete availability requirement without inventing an implementation.')
    return sources, before, raw


class PatternTests(unittest.TestCase):
    def test_capability_identifier_matches_existing_tlr_length_boundary(self):
        from canonical_patterns import INSTRUCTIONS
        sources, before, raw = proposal()
        binding = raw["capability_bindings"][0]
        binding["symbol"] = "c" * 48
        variable, row = compile_capability(binding, before["requirements"][1])
        self.assertEqual(len(variable["name"]), 48)
        self.assertEqual(row["formula"], {"var": "c" * 48})
        binding["symbol"] = "c" * 49
        with self.assertRaisesRegex(ValueError, "48"):
            compile_capability(binding, before["requirements"][1])
        self.assertIn("at most 48 characters", INSTRUCTIONS)


    def test_expand_and_review_are_separate_with_complete_source_and_context_diff(self):
        sources, before, raw = proposal(); snapshot = deepcopy(raw)
        result = validate_feedback_proposal(raw, sources, before,
            allow_generated_context_repair=True, require_pattern_diagnostics=True)
        self.assertEqual(raw, snapshot)
        self.assertEqual(result['changes']['recovered_ids'], ['R2'])
        self.assertEqual(result['changes']['added_symbols'], ['unicast_available'])
        self.assertEqual(result['tlr']['requirements'][1]['formula'], {'var': 'unicast_available', 'at': 'current'})
        self.assertEqual(result['pattern_review']['status'], 'structured')
        self.assertEqual(len(result['context_reviews']), 1)
        self.assertEqual(result['changes']['context_changes'][0]['field'], '$record')
        self.assertNotIn('accepted', result)

    def test_expansion_preserves_proposed_component_bindings_for_downstream_gate(self):
        from canonical_coverage import validate_coverage
        sources, before, raw = proposal()
        coverage = [{'obligation_id': 'R2.O1', 'status': 'represented',
            'formula_path': '/formula', 'slots': {'operation': ['/formula']}}]
        raw['tlr']['requirements'][1]['coverage'] = deepcopy(coverage)
        result = validate_feedback_proposal(raw, sources, before,
            allow_generated_context_repair=True, require_pattern_diagnostics=True)
        self.assertEqual(result['tlr']['requirements'][1]['coverage'], coverage)
        inventory = {'schema': 'mbse_obligation_inventory/1', 'requirements': [
            {'id': 'R2', 'obligations': [{'id': 'R2.O1', 'kind': 'capability',
             'slots': {'subject': 'library', 'scope': 'API', 'operation': 'send unicast messages'}}]}]}
        self.assertIn('R2', validate_coverage(result['tlr'], inventory)['eligible_ids'])
        # A stale/wrong mapping is retained as a reviewable defect, never fixed
        # automatically by the capability macro or interpreted as acceptance.
        raw['tlr']['requirements'][1]['coverage'][0]['formula_path'] = '/formula/args/0'
        result = validate_feedback_proposal(raw, sources, before,
            allow_generated_context_repair=True, require_pattern_diagnostics=True)
        report = validate_coverage(result['tlr'], inventory)
        self.assertNotIn('R2', report['eligible_ids'])
        self.assertIn('NONASSERTED_COMPONENT_PATH', {f['code'] for f in report['findings']})

    def test_expansion_does_not_invent_mapping_from_previous_withheld_metadata(self):
        sources, before, raw = proposal()
        before['requirements'][1]['coverage'] = [{'obligation_id': 'R2.O1', 'status': 'unsupported', 'reason': 'Old candidate abstained.'}]
        result = validate_feedback_proposal(raw, sources, before,
            allow_generated_context_repair=True, require_pattern_diagnostics=True)
        self.assertNotIn('coverage', result['tlr']['requirements'][1])

    def test_missing_diagnosis_and_applicable_without_attempt_cannot_end_recovery(self):
        sources, before, raw = proposal()
        with self.assertRaisesRegex(ValueError, 'structured'):
            validate_feedback_proposal(envelope(sources, before), sources, before, require_pattern_diagnostics=True)
        raw['capability_bindings'] = []
        with self.assertRaisesRegex(ValueError, 'actual binding attempt'):
            validate_feedback_proposal(raw, sources, before, require_pattern_diagnostics=True)

    def test_concrete_blocker_is_recorded_but_realization_detail_alone_rejected(self):
        sources, before, raw = proposal()
        raw['capability_bindings'] = []
        raw['reviews'][1].update(outcome='retained', source_basis=[])
        d = raw['abstention_diagnostics'][0]; d['decision'] = 'blocked'
        with self.assertRaisesRegex(ValueError, 'Realization detail alone'):
            validate_feedback_proposal(raw, sources, before, require_pattern_diagnostics=True)
        d['unsupported_operators'] = ['A required historical membership relation, if established by source review.']
        result = validate_feedback_proposal(raw, sources, before, require_pattern_diagnostics=True)
        self.assertFalse(result['changes']['progress'])
        self.assertEqual(result['pattern_review']['diagnostics'][0]['unsupported_operators'], d['unsupported_operators'])
        # The guard checks structure, not whether this LLM diagnosis is truthful.

    def test_partial_compound_capability_and_hidden_alternative_rejected(self):
        sources, before, original = proposal()
        for field, value in [('whole_obligation', False), ('residual_obligations', ['Automatic execution remains unencoded.'])]:
            raw = deepcopy(original); raw['capability_bindings'][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'compound'):
                validate_feedback_proposal(raw, sources, before, require_pattern_diagnostics=True)
        raw = deepcopy(original); raw['abstention_diagnostics'][0]['missing_slots'] = ['Required recipient definition.']
        with self.assertRaisesRegex(ValueError, 'conceal'):
            validate_feedback_proposal(raw, sources, before, require_pattern_diagnostics=True)

    def test_binding_cannot_overwrite_symbols_or_hide_source_rewrite(self):
        sources, before, original = proposal()
        raw = deepcopy(original); raw['capability_bindings'][0]['symbol'] = 'power'
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            validate_feedback_proposal(raw, sources, before, require_pattern_diagnostics=True)
        raw = deepcopy(original); raw['tlr']['requirements'][1]['text'] = 'A different source.'
        with self.assertRaisesRegex(ValueError, 'differs from prepared source'):
            validate_feedback_proposal(raw, sources, before, require_pattern_diagnostics=True)

    def test_binding_quotes_exact_source_and_fixed_context_is_still_enforced(self):
        from canonical_tlr import tlr_context
        sources, before, raw = proposal()
        raw['capability_bindings'][0]['source_basis'][0]['quote'] = 'Invented source.'
        with self.assertRaisesRegex(ValueError, 'literal'):
            validate_feedback_proposal(raw, sources, before, require_pattern_diagnostics=True)
        sources, before, raw = proposal()
        with self.assertRaisesRegex(ValueError, 'fixed'):
            validate_feedback_proposal(raw, sources, before, fixed_context=tlr_context(before), require_pattern_diagnostics=True)

    def test_legacy_explicitly_marked_and_unknown_envelope_fields_still_fail(self):
        sources, before = fixture(); raw = envelope(sources, before)
        result = validate_feedback_proposal(raw, sources, before)
        self.assertEqual(result['pattern_review']['status'], 'legacy_unstructured')
        raw['certificate'] = 'approved'
        with self.assertRaises(ValueError):
            validate_feedback_proposal(raw, sources, before)

    def test_alpha_renaming_and_replay_preserve_capability_semantics(self):
        from mutation_core import emit_formula
        import shutil, subprocess
        sources, before, raw = proposal(); binding = raw['capability_bindings'][0]
        for name in ('unicast_available', 'renamed_operation'):
            binding = {**binding, 'symbol': name}
            variable, row = compile_capability(binding, before['requirements'][1])
            context = {'variables': [{'name': name, 'type': 'Bool'}], 'background': []}
            expr = emit_formula(row['formula'], context)
            for value in (True, False):
                expected = evaluate_capability(binding, {name: value})
                self.assertEqual(expected, value)
                if shutil.which('z3'):
                    query = f'(declare-const v_{name}_0 Bool)\n(assert (= v_{name}_0 {str(value).lower()}))\n(assert {expr})\n(check-sat)\n'
                    result = subprocess.run(['z3', '-in'], input=query, text=True, capture_output=True, check=True, timeout=10)
                    self.assertEqual(result.stdout.strip(), 'sat' if expected else 'unsat')
        with self.assertRaises(ValueError): evaluate_capability(binding, {})





if __name__ == '__main__': unittest.main()
