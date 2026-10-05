"""Offline source/solver feedback guards and exact audit-evidence extraction."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from canonical_abstractions import POLICY_VERSION
from canonical_feedback import (FEEDBACK_INSTRUCTIONS, FEEDBACK_POLICY_VERSION,
    feedback_instructions, feedback_prompt, scenario_instructions, solver_feedback,
    validate_feedback_proposal)
from canonical_tlr import tlr_context, validate_tlr


def fixture():
    sources = [
        {'id': 'R1', 'text': 'The battery voltage shall be at most 28 V.',
         'source': {'context': {'domain': 'Measurements lie between 0 V and 100 V.'}}},
        {'id': 'R2', 'text': 'The library shall support sending unicast messages.',
         'source': {'context': {'definition': 'Unicast selects one destination.', 'context_key': 'The operation is optional to invoke.'}}},
        {'id': 'R3', 'text': 'After a stop request the controller shall remain stopped.', 'source': {'section': '3'}},
    ]
    raw = {'schema': 'mbse_tlr/1', 'abstraction_policy': POLICY_VERSION,
        'variables': [{'name': 'voltage', 'type': 'Real', 'unit': 'V',
                       'bounds': {'lower': '0', 'upper': '100'}, 'description': 'Observed battery voltage.'},
                      {'name': 'power', 'type': 'Bool', 'description': 'External power is available.'},
                      {'name': 'stopped', 'type': 'Bool', 'description': 'The controller is stopped at one observation.'}],
        'assumptions': [{'id': 'ENV', 'text': 'External power is present in this scenario.', 'predicate': {'var': 'power'}}],
        'requirements': [
            {'id': 'R1', 'status': 'supported', 'formula': {'op': '<', 'args': [{'var': 'voltage'}, {'value': '28', 'unit': 'V'}]},
             'abstraction': {'kind': 'state_constraint', 'meaning': 'Battery voltage is bounded.', 'scope': 'One observation.', 'limitations': []}},
            {'id': 'R2', 'status': 'unsupported', 'reason_code': 'profile_limit', 'reason': 'An implementation was initially assumed necessary.'},
            {'id': 'R3', 'status': 'supported', 'formula': {'var': 'stopped'},
             'abstraction': {'kind': 'state_constraint', 'meaning': 'Stopped at one observation.', 'scope': 'Current observation only.', 'limitations': []}},
        ]}
    return sources, validate_tlr(raw, sources, require_abstractions=True)


def envelope(sources, before, after=None):
    after = deepcopy(before if after is None else after)
    old = {r['id']: r for r in before['requirements']}
    source = {r['id']: r for r in sources}
    return {'schema': 'semantic_repair_proposal/1', 'tlr': after, 'reviews': [
        {'id': r['id'], 'outcome': 'retained' if r == old[r['id']] else 'changed',
         'reason': 'Preserve the complete source obligation.' if r == old[r['id']] else 'Correct the interpretation to preserve the stated source obligation.',
         'source_basis': [] if r == old[r['id']] else [{'source_id': r['id'], 'quote': source[r['id']]['text']}]}
        for r in after['requirements']]}


def corrected(before):
    after = deepcopy(before)
    after['requirements'][0]['formula']['op'] = '<='
    return after


def recover(before):
    after = deepcopy(before)
    after['variables'].append({'name': 'unicast_available', 'type': 'Bool', 'description': 'The library offers unicast sending.'})
    row = after['requirements'][1]; row.pop('reason'); row.pop('reason_code')
    row.update(status='supported', formula={'var': 'unicast_available'}, abstraction={
        'kind': 'capability', 'subject': 'library', 'operation': 'send unicast', 'symbol': 'unicast_available',
        'meaning': 'Unicast sending is available.', 'scope': 'Declared operation availability.',
        'limitations': ['No invocation or delivery is proved.']})
    return after


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def audit_fixture(directory):
    directory.mkdir(parents=True, exist_ok=True)
    query = '(declare-const voltage Real)\n(assert (> voltage 28))\n(check-sat)\n'
    witness_query = query + '(get-value (voltage))\n'
    witness = {'status': 'sat', 'stdout': 'sat\n((voltage 29.0))\n', 'stderr': '', 'exit_code': 0,
        'witness_status': 'available', 'artifacts': {'query': 'violation_witness.smt2', 'result': 'violation_witness.json'}}
    evidence = {'status': 'sat', 'stdout': 'sat\n', 'stderr': '', 'exit_code': 0,
        'purpose': 'Can the background admit a violation with the target excluded?', 'requirement_ids': ['R1'],
        'artifacts': {'query': 'violation.smt2', 'result': 'violation.json'},
        'witness': {'voltage': '29.0'}, 'witness_evidence': witness,
        'witness_kind': 'Static valuation; no supplied design.', 'judge_verdict': 'must never enter feedback'}
    (directory / 'violation.smt2').write_text(query)
    (directory / 'violation_witness.smt2').write_text(witness_query)
    put(directory / 'violation.json', evidence); put(directory / 'violation_witness.json', witness)
    analysis = {'schema': 'canonical_audits/1', 'status': 'passed', 'background_status': 'sat',
        'consistency_status': 'sat', 'requirement_ids': ['R1', 'R2', 'R3'],
        'findings': [{'code': 'redundant_requirement', 'requirement_ids': ['R1'], 'explanation': 'May be legitimate.', 'expected_formula': 'must not enter'}],
        'inconclusive_checks': [], 'requirements': [{'id': 'R1', 'checks': {'violatability': evidence}}],
        'judges': ['held-out'], 'mutation_results': {'expected': 'held-out'}}
    put(directory / 'audit.json', analysis)
    return analysis, query, witness_query


class ProposalTests(unittest.TestCase):
    def test_supported_endpoint_can_change_without_source_or_environment_changes(self):
        sources, before = fixture(); raw = envelope(sources, before, corrected(before)); snapshot = deepcopy(raw)
        with patch('mutation_core._execute', side_effect=AssertionError('No solver execution in guard')):
            result = validate_feedback_proposal(raw, sources, before)
        self.assertEqual(result['changes']['changed_ids'], ['R1'])
        self.assertEqual(result['changes']['revised_supported_ids'], ['R1'])
        self.assertEqual(result['changes']['retained_ids'], ['R2', 'R3'])
        self.assertTrue(result['changes']['progress'])
        self.assertEqual(result['tlr']['requirements'][0]['formula']['op'], '<=')
        self.assertEqual(raw, snapshot)

    def test_retained_records_and_no_progress_remain_exact(self):
        sources, before = fixture(); result = validate_feedback_proposal(envelope(sources, before), sources, before)
        self.assertFalse(result['changes']['progress'])
        self.assertEqual(result['tlr'], before)

    def test_supported_to_abstained_is_recorded_as_regression_not_recovery(self):
        sources, before = fixture()
        for status, code in [('unsupported', 'profile_limit'), ('unresolved', 'source_ambiguity')]:
            after = deepcopy(before); row = after['requirements'][2]
            row.pop('formula'); row.pop('abstraction')
            row.update(status=status, reason_code=code, reason='The static predicate does not establish persistence after the request.')
            with self.subTest(status=status):
                result = validate_feedback_proposal(envelope(sources, before, after), sources, before)
                self.assertEqual(result['changes']['regressed_ids'], ['R3'])
                self.assertEqual(result['changes']['recovered_ids'], [])
                self.assertEqual(result['changes']['status_changes'], [{'id': 'R3', 'before': 'supported', 'after': status}])

    def test_recovery_adds_only_a_described_unbounded_referenced_symbol(self):
        sources, before = fixture(); result = validate_feedback_proposal(envelope(sources, before, recover(before)), sources, before)
        self.assertEqual(result['changes']['recovered_ids'], ['R2'])
        self.assertEqual(result['changes']['added_symbols'], ['unicast_available'])

    def test_all_source_ids_need_unique_reviews_including_supported_rows(self):
        sources, before = fixture()
        for kind in ('missing', 'duplicate', 'unknown'):
            raw = envelope(sources, before, corrected(before))
            if kind == 'missing': raw['reviews'].pop(0)
            elif kind == 'duplicate': raw['reviews'].append(deepcopy(raw['reviews'][0]))
            else: raw['reviews'][0]['id'] = 'UNKNOWN'
            with self.subTest(kind=kind), self.assertRaises(ValueError): validate_feedback_proposal(raw, sources, before)

    def test_changed_target_needs_own_literal_quote_not_neighbor_or_context_only(self):
        sources, before = fixture()
        for basis in ([], [{'source_id': 'R2', 'quote': sources[1]['text']}],
                      [{'source_id': 'R1', 'quote': 'Measurements lie between 0 V and 100 V.'}],
                      [{'source_id': 'R1', 'quote': 'A fabricated requirement.'}]):
            raw = envelope(sources, before, corrected(before)); raw['reviews'][0]['source_basis'] = basis
            with self.subTest(basis=basis), self.assertRaises(ValueError): validate_feedback_proposal(raw, sources, before)

    def test_quotes_on_retained_rows_are_also_checked(self):
        sources, before = fixture(); raw = envelope(sources, before)
        raw['reviews'][1]['source_basis'] = [{'source_id': 'R2', 'quote': 'context_key'}]
        with self.assertRaisesRegex(ValueError, 'literal source/context'): validate_feedback_proposal(raw, sources, before)

    def test_retained_change_and_changed_noop_are_rejected(self):
        sources, before = fixture()
        for changed, outcome in [(True, 'retained'), (False, 'changed')]:
            raw = envelope(sources, before, corrected(before) if changed else before)
            raw['reviews'][0]['outcome'] = outcome
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'normalized requirement changes'):
                validate_feedback_proposal(raw, sources, before)

    def test_raw_source_rewrites_are_rejected_before_authoritative_rebinding(self):
        sources, before = fixture()
        for key, value in [('text', 'The voltage shall be 29 V.'), ('source', {'context': 'A newly invented premise.'})]:
            raw = envelope(sources, before, corrected(before)); raw['tlr']['requirements'][0][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'differs from prepared source'):
                validate_feedback_proposal(raw, sources, before)

    def test_omitted_source_fields_rebind_and_normalized_order_does_not_change_meaning(self):
        sources, before = fixture(); raw = envelope(sources, before)
        for row in raw['tlr']['requirements']: row.pop('text'); row.pop('source')
        raw['tlr']['requirements'][0]['formula']['args'][0].pop('at')
        raw['tlr']['requirements'].reverse(); raw['tlr']['variables'].reverse(); raw['reviews'].reverse()
        result = validate_feedback_proposal(raw, sources, before)
        self.assertFalse(result['changes']['progress'])
        self.assertEqual([r['id'] for r in result['reviews']], ['R1', 'R2', 'R3'])

    def test_existing_variables_are_frozen_including_meaning_and_bounds(self):
        sources, before = fixture()
        for key, value in [('description', 'A different voltage quantity.'), ('bounds', {'upper': '28'}), ('unit', 'A'), ('type', 'Int')]:
            raw = envelope(sources, before, corrected(before)); raw['tlr']['variables'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): validate_feedback_proposal(raw, sources, before)
        raw = envelope(sources, before); raw['tlr']['variables'].pop(1); raw['tlr']['assumptions'] = []
        with self.assertRaises(ValueError): validate_feedback_proposal(raw, sources, before)

    def test_assumptions_cannot_be_changed_removed_or_gain_a_copied_guarantee(self):
        sources, before = fixture()
        for kind in ('text', 'predicate', 'remove', 'add'):
            raw = envelope(sources, before, corrected(before))
            if kind == 'text': raw['tlr']['assumptions'][0]['text'] = 'Changed environment meaning.'
            elif kind == 'predicate': raw['tlr']['assumptions'][0]['predicate'] = deepcopy(raw['tlr']['requirements'][0]['formula'])
            elif kind == 'remove': raw['tlr']['assumptions'] = []
            else: raw['tlr']['assumptions'].append({'id': 'NEW', 'text': 'Assume the obligation.', 'predicate': deepcopy(raw['tlr']['requirements'][0]['formula'])})
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'background assumptions'):
                validate_feedback_proposal(raw, sources, before)

    def test_new_bounded_unused_or_undefined_symbols_are_rejected(self):
        sources, before = fixture()
        for symbol in [{'name': 'extra', 'type': 'Real', 'bounds': {'lower': '0'}, 'description': 'Extra magnitude.'},
                       {'name': 'extra', 'type': 'Bool', 'description': 'Unused flag.'},
                       {'name': 'extra', 'type': 'Bool'}]:
            raw = envelope(sources, before, corrected(before)); raw['tlr']['variables'].append(symbol)
            with self.subTest(symbol=symbol), self.assertRaises(ValueError): validate_feedback_proposal(raw, sources, before)

    def test_fixed_context_allows_formula_changes_but_no_new_symbols(self):
        sources, before = fixture(); context = tlr_context(before)
        result = validate_feedback_proposal(envelope(sources, before, corrected(before)), sources, before, context)
        self.assertEqual(result['changes']['changed_ids'], ['R1'])
        with self.assertRaisesRegex(ValueError, 'fixed'):
            validate_feedback_proposal(envelope(sources, before, recover(before)), sources, before, context)
        context['variables'][0]['bounds']['upper'] = '200'
        with self.assertRaisesRegex(ValueError, 'fixed context'):
            validate_feedback_proposal(envelope(sources, before), sources, before, context)

    def test_requirement_ids_policy_schema_and_wrapper_are_frozen(self):
        sources, before = fixture()
        for kind in ('remove', 'rename', 'policy', 'schema', 'extra', 'reason'):
            raw = envelope(sources, before)
            if kind == 'remove': raw['tlr']['requirements'].pop()
            elif kind == 'rename': raw['tlr']['requirements'][0]['id'] = 'RENAMED'
            elif kind == 'policy': raw['tlr']['abstraction_policy'] = 'new'
            elif kind == 'schema': raw['schema'] = 'wrong'
            elif kind == 'extra': raw['judge_scores'] = {'R1': 'pass'}
            else: raw['reviews'][0]['reason'] = 'unsupported'
            with self.subTest(kind=kind), self.assertRaises(ValueError): validate_feedback_proposal(raw, sources, before)


class GeneratedContextRepairTests(unittest.TestCase):
    def review(self, kind, identity, field, source_id='R1', quote=None):
        sources, _ = fixture()
        return {'kind': kind, 'id': identity, 'field': field,
                'reason': 'Correct the generated premise against the unchanged source and retain its independent review obligation.',
                'source_basis': [{'source_id': source_id, 'quote': quote or sources[0]['text']}]}

    def test_generated_assumption_removal_records_exact_diff_and_invalidates_all_rules(self):
        sources, before = fixture(); after = deepcopy(before); after['assumptions'] = []
        raw = envelope(sources, before, after)
        raw['context_reviews'] = [self.review('assumption', 'ENV', '$record')]
        snapshot = deepcopy(raw)
        with patch('mutation_core._execute', side_effect=AssertionError('Guard must not decide using SAT')):
            result = validate_feedback_proposal(raw, sources, before, allow_generated_context_repair=True)
        changes = result['changes']
        self.assertTrue(changes['context_changed'])
        self.assertTrue(changes['progress'])
        self.assertTrue(changes['requires_source_review'])
        self.assertEqual(changes['changed_ids'], [])
        self.assertEqual(changes['affected_requirement_ids'], ['R1', 'R2', 'R3'])
        change = changes['context_changes'][0]
        self.assertEqual(change['before'], before['assumptions'][0])
        self.assertIsNone(change['after'])
        self.assertTrue(change['before_present'])
        self.assertFalse(change['after_present'])
        self.assertEqual(change['source_basis'], raw['context_reviews'][0]['source_basis'])
        self.assertEqual(raw, snapshot)

    def test_each_changed_symbol_field_has_an_exact_normalized_diff(self):
        sources, before = fixture(); after = deepcopy(before)
        after['variables'][0]['description'] = 'Battery terminal voltage at the current observation.'
        after['variables'][0].pop('bounds')
        raw = envelope(sources, before, after)
        raw['context_reviews'] = [self.review('variable', 'voltage', name) for name in ('bounds', 'description')]
        result = validate_feedback_proposal(raw, sources, before, allow_generated_context_repair=True)
        changes = {change['field']: change for change in result['changes']['context_changes']}
        self.assertEqual(changes['description']['before'], before['variables'][0]['description'])
        self.assertEqual(changes['description']['after'], after['variables'][0]['description'])
        self.assertEqual(changes['bounds']['before'], before['variables'][0]['bounds'])
        self.assertFalse(changes['bounds']['after_present'])
        self.assertEqual(result['context_reviews'], raw['context_reviews'])

    def test_generated_context_changes_are_opt_in_and_need_complete_field_reviews(self):
        sources, before = fixture(); after = deepcopy(before)
        after['variables'][0]['description'] = 'Corrected meaning of the observed battery voltage.'
        after['variables'][0].pop('bounds')
        raw = envelope(sources, before, after)
        with self.assertRaises(ValueError): validate_feedback_proposal(raw, sources, before)
        for reviews in (None, [], [self.review('variable', 'voltage', 'description')]):
            attempt = deepcopy(raw)
            if reviews is not None: attempt['context_reviews'] = reviews
            with self.subTest(reviews=reviews), self.assertRaisesRegex(ValueError, 'context_reviews'):
                validate_feedback_proposal(attempt, sources, before, allow_generated_context_repair=True)

    def test_context_reviews_reject_duplicates_unrelated_fields_empty_or_fabricated_evidence(self):
        sources, before = fixture(); after = deepcopy(before); after['assumptions'] = []
        good = self.review('assumption', 'ENV', '$record')
        for kind in ('duplicate', 'unrelated', 'empty', 'fabricated', 'unknown_source', 'generic', 'before_after'):
            raw = envelope(sources, before, after); raw['context_reviews'] = [deepcopy(good)]
            review = raw['context_reviews'][0]
            if kind == 'duplicate': raw['context_reviews'].append(deepcopy(good))
            elif kind == 'unrelated': review['field'] = 'text'
            elif kind == 'empty': review['source_basis'] = []
            elif kind == 'fabricated': review['source_basis'][0]['quote'] = 'Imaginary source permission.'
            elif kind == 'unknown_source': review['source_basis'][0]['source_id'] = 'MISSING'
            elif kind == 'generic': review['reason'] = 'unsupported'
            else: review['before'] = {'predicate': True}
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                validate_feedback_proposal(raw, sources, before, allow_generated_context_repair=True)

    def test_context_source_excerpts_are_allowed_but_document_keys_are_not_quotes(self):
        sources, before = fixture(); after = deepcopy(before)
        after['variables'][0]['description'] = 'Battery voltage measured within the source observation domain.'
        raw = envelope(sources, before, after)
        raw['context_reviews'] = [self.review('variable', 'voltage', 'description', quote='Measurements lie between 0 V and 100 V.')]
        validate_feedback_proposal(raw, sources, before, allow_generated_context_repair=True)
        raw['context_reviews'][0]['source_basis'][0]['quote'] = 'domain'
        with self.assertRaisesRegex(ValueError, 'literal source/context'):
            validate_feedback_proposal(raw, sources, before, allow_generated_context_repair=True)

    def test_source_ids_and_words_remain_immutable_under_generated_context_policy(self):
        sources, before = fixture(); after = deepcopy(before); after['assumptions'] = []
        for key, value in [('text', 'The voltage may exceed 28 V.'), ('source', {'context': 'Invented context.'})]:
            raw = envelope(sources, before, after)
            raw['context_reviews'] = [self.review('assumption', 'ENV', '$record')]
            raw['tlr']['requirements'][0][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'differs from prepared source'):
                validate_feedback_proposal(raw, sources, before, allow_generated_context_repair=True)

    def test_fixed_context_is_frozen_even_with_complete_change_rationale(self):
        sources, before = fixture(); context = tlr_context(before)
        for mode in ('assumption', 'description', 'bounds'):
            after = deepcopy(before)
            if mode == 'assumption':
                after['assumptions'] = []
                review = self.review('assumption', 'ENV', '$record')
            else:
                if mode == 'description': after['variables'][0][mode] = 'An altered variable meaning.'
                else: after['variables'][0].pop(mode)
                review = self.review('variable', 'voltage', mode)
            raw = envelope(sources, before, after); raw['context_reviews'] = [review]
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_feedback_proposal(raw, sources, before, context, allow_generated_context_repair=True)

    def test_generated_context_policy_allows_existing_envelope_when_shared_context_unchanged(self):
        sources, before = fixture(); raw = envelope(sources, before, corrected(before))
        result = validate_feedback_proposal(raw, sources, before, allow_generated_context_repair=True)
        self.assertFalse(result['changes']['context_changed'])
        self.assertEqual(result['changes']['context_changes'], [])
        self.assertEqual(result['changes']['affected_requirement_ids'], ['R1'])
        self.assertTrue(result['changes']['requires_source_review'])
        self.assertEqual(result['context_reviews'], [])
        raw = envelope(sources, before); raw['context_reviews'] = []
        result = validate_feedback_proposal(raw, sources, before, allow_generated_context_repair=True)
        self.assertFalse(result['changes']['progress'])
        self.assertFalse(result['changes']['requires_source_review'])

    def test_new_symbol_addition_and_unused_symbol_removal_need_record_reviews(self):
        sources, before = fixture(); after = recover(before)
        raw = envelope(sources, before, after)
        with self.assertRaisesRegex(ValueError, 'context_reviews'):
            validate_feedback_proposal(raw, sources, before, allow_generated_context_repair=True)
        raw['context_reviews'] = [self.review('variable', 'unicast_available', '$record', 'R2', sources[1]['text'])]
        result = validate_feedback_proposal(raw, sources, before, allow_generated_context_repair=True)
        self.assertEqual(result['changes']['added_symbols'], ['unicast_available'])
        self.assertFalse(result['changes']['context_changes'][0]['before_present'])
        after = deepcopy(before); after['variables'].pop(1); after['assumptions'] = []
        raw = envelope(sources, before, after)
        raw['context_reviews'] = [self.review('variable', 'power', '$record'), self.review('assumption', 'ENV', '$record')]
        result = validate_feedback_proposal(raw, sources, before, allow_generated_context_repair=True)
        self.assertEqual(result['changes']['removed_symbols'], ['power'])

    def test_assumption_field_edits_and_additions_are_recorded_but_not_semantically_approved(self):
        sources, before = fixture(); after = deepcopy(before)
        after['assumptions'][0]['text'] = 'The power condition is unspecified in this prepared source.'
        after['assumptions'][0]['predicate'] = {'op': 'not', 'args': [{'var': 'power'}]}
        after['assumptions'].append({'id': 'BOUND', 'text': 'Proposed measurement domain.',
                                    'predicate': {'op': '>=', 'args': [{'var': 'voltage'}, {'value': '0', 'unit': 'V'}]}})
        raw = envelope(sources, before, after)
        raw['context_reviews'] = [self.review('assumption', 'ENV', field) for field in ('predicate', 'text')]
        raw['context_reviews'].append(self.review('assumption', 'BOUND', '$record', quote='Measurements lie between 0 V and 100 V.'))
        result = validate_feedback_proposal(raw, sources, before, allow_generated_context_repair=True)
        self.assertEqual(len(result['changes']['context_changes']), 3)
        self.assertTrue(result['changes']['requires_source_review'])
        self.assertNotIn('accepted', result)

    def test_new_unused_symbol_is_rejected_even_with_literal_rationale(self):
        sources, before = fixture(); after = deepcopy(before)
        after['variables'].append({'name': 'unused', 'type': 'Bool', 'description': 'A named but unused generated symbol.'})
        raw = envelope(sources, before, after)
        raw['context_reviews'] = [self.review('variable', 'unused', '$record')]
        with self.assertRaisesRegex(ValueError, 'must be referenced'):
            validate_feedback_proposal(raw, sources, before, allow_generated_context_repair=True)

    def test_prompt_selects_generated_policy_without_relaxing_input_or_evaluation_boundaries(self):
        sources, before = fixture()
        payload = json.loads(feedback_prompt(sources, None, before, allow_generated_context_repair=True))
        self.assertTrue(payload['generated_context_policy']['generated_context_repair_permitted'])
        payload = json.loads(feedback_prompt(sources, tlr_context(before), before, allow_generated_context_repair=True))
        self.assertFalse(payload['generated_context_policy']['generated_context_repair_permitted'])
        self.assertTrue(payload['generated_context_policy']['fixed_context_immutable'])
        self.assertEqual(feedback_instructions(), FEEDBACK_INSTRUCTIONS)
        instructions = feedback_instructions(True)
        for text in ('LLM-generated variables', 'context_reviews', 'not independent semantic approval',
                     'engineer input revision', 'final judges', 'literal quotations alone'):
            self.assertIn(text, instructions)
        self.assertNotIn("every existing variable's name/type/unit/bounds/description", instructions)
        self.assertIn('LLM-generated meanings and premises may be corrected', scenario_instructions(True))
        self.assertIn('Supplied source, fixed_context and explicit development definitions', scenario_instructions(True))

    def test_context_policy_requires_explicit_boolean_opt_in(self):
        sources, before = fixture()
        for value in ('true', 1, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError): feedback_instructions(value)
                with self.assertRaises(ValueError): scenario_instructions(value)
                with self.assertRaises(ValueError): feedback_prompt(sources, None, before, allow_generated_context_repair=value)
                with self.assertRaises(ValueError):
                    validate_feedback_proposal(envelope(sources, before), sources, before, allow_generated_context_repair=value)

    def test_development_assistance_while_source_review_pending_is_explicit_and_not_solver_evidence(self):
        from test_canonical_scenarios import fixture as scenario_fixture
        sources, before, suite = scenario_fixture()
        before['abstraction_policy'] = POLICY_VERSION
        before['requirements'][0]['abstraction'] = {
            'kind': 'state_constraint', 'meaning': 'Inclusive source bound.',
            'scope': 'One observation of the integer value.', 'limitations': []}
        with self.assertRaisesRegex(ValueError, 'requires solver feedback'):
            feedback_prompt(sources, None, before, development_scenarios=suite)
        payload = json.loads(feedback_prompt(sources, None, before, development_scenarios=suite,
            development_results={'status': 'not_run', 'reason': 'Source review pending'},
            allow_pending_source_review=True))
        self.assertIsNone(payload['solver_feedback'])
        self.assertEqual(payload['feedback_mode'], 'source_review_before_solver')
        self.assertEqual(payload['solver_availability']['status'], 'not_run')
        self.assertIn('pending source-to-rule acceptance', payload['solver_availability']['reason'])
        self.assertEqual(payload['development_results']['status'], 'not_run')
        self.assertEqual([row['id'] for row in payload['development_scenarios']['scenarios']], ['boundary', 'violation'])
        self.assertEqual([row['expected'] for row in payload['development_scenarios']['scenarios']], ['sat', 'unsat'])
        with self.assertRaisesRegex(ValueError, 'Boolean'):
            feedback_prompt(sources, None, before, allow_pending_source_review='yes')


class EvidenceAndPromptTests(unittest.TestCase):
    def test_exact_queries_results_and_sat_witness_survive_without_evaluation_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); analysis, query, witness_query = audit_fixture(root / 'audit')
            with patch('mutation_core._execute', side_effect=AssertionError('Read-only evidence adapter')):
                feedback = solver_feedback(analysis, root)
            evidence = feedback['checks'][0]
            self.assertEqual(evidence['query_smt2'], query)
            self.assertEqual(evidence['result']['stdout'], 'sat\n')
            self.assertEqual(evidence['witness']['query_smt2'], witness_query)
            self.assertEqual(evidence['witness']['result']['stdout'], 'sat\n((voltage 29.0))\n')
            self.assertEqual(evidence['witness']['values'], {'voltage': '29.0'})
            self.assertNotIn('held-out', json.dumps(feedback))
            self.assertNotIn('must not enter', json.dumps(feedback))
            self.assertNotIn('must never enter', json.dumps(feedback))
            self.assertEqual(solver_feedback(analysis, root / 'audit'), feedback)

    def test_missing_query_is_explicit_not_a_fabricated_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); analysis, _, _ = audit_fixture(root)
            (root / 'violation.smt2').unlink()
            check = solver_feedback(analysis, root)['checks'][0]
            self.assertIsNone(check['query_smt2'])
            self.assertIn('violation.smt2', check['evidence_missing'])

    def test_artifact_escape_and_mismatched_record_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); analysis, _, _ = audit_fixture(root / 'audit')
            raw = deepcopy(analysis); raw['requirements'][0]['checks']['violatability']['artifacts']['query'] = '../other.smt2'
            with self.assertRaisesRegex(ValueError, 'inside'): solver_feedback(raw, root)
            raw = deepcopy(analysis); raw['requirements'][0]['checks']['violatability']['stdout'] = 'unsat\n'
            with self.assertRaisesRegex(ValueError, 'disagrees'): solver_feedback(raw, root)

    def test_source_prompt_is_compact_full_source_and_has_no_solver_evidence(self):
        sources, before = fixture(); snapshot = deepcopy(before)
        payload = json.loads(feedback_prompt(sources, None, before))
        self.assertEqual(payload['source_packet'], sources)
        self.assertEqual(payload['review_requirement_ids'], ['R1', 'R2', 'R3'])
        self.assertEqual(payload['feedback_mode'], 'source')
        self.assertIsNone(payload['solver_feedback'])
        for original, compact in zip(before['requirements'], payload['current_tlr']['requirements']):
            self.assertEqual(compact, {k: v for k, v in original.items() if k not in {'text', 'source'}})
        self.assertEqual(before, snapshot)

    def test_solver_prompt_includes_exact_query_and_rejects_foreign_source_or_metadata(self):
        sources, before = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            analysis, query, _ = audit_fixture(Path(tmp)); feedback = solver_feedback(analysis, tmp)
            payload = json.loads(feedback_prompt(sources, None, before, feedback, 'Previous proposal changed a frozen domain.'))
            self.assertEqual(payload['feedback_mode'], 'solver')
            self.assertEqual(payload['solver_feedback']['checks'][0]['query_smt2'], query)
            self.assertEqual(payload['previous_failure'], 'Previous proposal changed a frozen domain.')
            foreign = deepcopy(feedback); foreign['requirement_ids'] = ['OTHER']
            with self.assertRaisesRegex(ValueError, 'source IDs'): feedback_prompt(sources, None, before, foreign)
            foreign = deepcopy(feedback); foreign['judges'] = {'R1': 'pass'}
            with self.assertRaises(ValueError): feedback_prompt(sources, None, before, foreign)

    def test_prompt_validates_original_source_before_removing_duplicate_metadata(self):
        sources, before = fixture(); before['requirements'][0]['text'] = 'Voltage at most 29 V.'
        with self.assertRaisesRegex(ValueError, 'fixed source'): feedback_prompt(sources, None, before)

    def test_selection_omits_repeated_checks_but_retains_the_complete_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); analysis, _, _ = audit_fixture(root)
            original = analysis['requirements'][0]['checks']['violatability']
            analysis['findings'] = []
            analysis['background'] = deepcopy(original); analysis['consistency'] = deepcopy(original)
            for name in ('redundancy_context', 'redundancy', 'in_model_trigger'):
                analysis['requirements'][0]['checks'][name] = deepcopy(original)
            feedback = solver_feedback(analysis, root)
            indexed = {c['name']: c for c in feedback['checks']}
            self.assertEqual(len(indexed), 6)
            self.assertTrue(all(indexed[name]['evidence_included'] for name in ('background', 'consistency', 'violatability')))
            for name in ('redundancy_context', 'redundancy', 'in_model_trigger'):
                self.assertFalse(indexed[name]['evidence_included'])
                self.assertIsNone(indexed[name]['query_smt2'])
                self.assertEqual(indexed[name]['query_file'], 'violation.smt2')
                self.assertEqual(indexed[name]['status'], 'sat')
                self.assertFalse(indexed[name]['witness']['evidence_included'])
            self.assertEqual(feedback['selection']['omitted_by_policy_count'], 3)
            self.assertEqual(feedback['selection']['omitted_by_cap_count'], 0)
            self.assertTrue(feedback['selection']['index_complete'])
            self.assertIn('(assert (> voltage 28))', (root / 'violation.smt2').read_text())

    def test_findings_and_inconclusives_promote_corresponding_exact_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); analysis, _, _ = audit_fixture(root)
            original = analysis['requirements'][0]['checks']['violatability']
            for name in ('trigger_reachability', 'in_model_trigger', 'redundancy_context', 'redundancy'):
                check = {'status': 'unknown' if name == 'trigger_reachability' else 'sat',
                         'purpose': name, 'requirement_ids': ['R1'],
                         'artifacts': {'query': name + '.smt2', 'result': name + '.json'}}
                check['stdout'] = check['status'] + '\n'
                (root / (name + '.smt2')).write_text('; ' + name + '\n(check-sat)\n')
                put(root / (name + '.json'), check)
                analysis['requirements'][0]['checks'][name] = check
            analysis['findings'].append({'code': 'trigger_excluded_by_specification', 'requirement_ids': ['R1'], 'explanation': 'Inspect the complete specification.'})
            analysis['inconclusive_checks'] = [{'check': 'trigger_reachability', 'status': 'unknown', 'requirement_ids': ['R1']}]
            feedback = solver_feedback(analysis, root)
            self.assertEqual(feedback['selection']['included_checks'], 5)
            reasons = {c['name']: c['selection_reason'] for c in feedback['checks']}
            self.assertEqual(reasons['trigger_reachability'], 'inconclusive:trigger_reachability')
            self.assertEqual(reasons['in_model_trigger'], 'finding:trigger_excluded_by_specification')
            self.assertEqual(reasons['redundancy'], 'finding:redundant_requirement')
            self.assertEqual(reasons['redundancy_context'], 'finding:redundant_requirement')
            self.assertEqual(reasons['violatability'], 'ordinary_violatability_diagnostic')

    def test_selection_cap_is_disclosed_stable_and_prioritizes_globals_and_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); analysis, _, _ = audit_fixture(root)
            original = analysis['requirements'][0]['checks']['violatability']
            analysis['background'] = deepcopy(original); analysis['consistency'] = deepcopy(original)
            analysis['requirement_ids'] = [f'R{i}' for i in range(70)]
            analysis['requirements'] = [{'id': rid, 'checks': {'violatability': deepcopy(original)}} for rid in analysis['requirement_ids']]
            analysis['requirements'][-1]['checks']['redundancy'] = deepcopy(original)
            analysis['requirements'][-1]['checks']['redundancy_context'] = deepcopy(original)
            analysis['findings'] = [{'code': 'redundant_requirement', 'requirement_ids': ['R69'], 'explanation': 'Inspect this legitimate relation.'}]
            first = solver_feedback(analysis, root)
            self.assertEqual(first, solver_feedback(analysis, root))
            self.assertEqual(first['selection']['included_checks'], 64)
            self.assertEqual(first['selection']['executed_checks'], 74)
            self.assertEqual(first['selection']['omitted_by_cap_count'], 10)
            self.assertEqual(first['selection']['omitted_by_policy_count'], 0)
            selected = {(c['name'], c['target_requirement_id']) for c in first['checks'] if c['evidence_included']}
            self.assertTrue({('background', None), ('consistency', None), ('redundancy', 'R69'), ('redundancy_context', 'R69')} <= selected)
            self.assertIn(('violatability', 'R59'), selected)
            self.assertNotIn(('violatability', 'R60'), selected)
            self.assertEqual(len(first['selection']['omitted_queries']), 10)

    def test_instructions_do_not_treat_expected_audit_results_as_defects(self):
        self.assertEqual(FEEDBACK_POLICY_VERSION, 'source_grounded_semantic_feedback/3')
        for phrase in ('SAT violatability query is ordinarily expected', 'UNSAT redundancy can be legitimate',
                       'real conflict in the unchanged source must remain visible',
                       'Previously supported formulas MAY change', 'loss of formal coverage',
                       'final judges', 'mutation labels/results'):
            self.assertIn(phrase, FEEDBACK_INSTRUCTIONS)


if __name__ == '__main__':
    unittest.main()
