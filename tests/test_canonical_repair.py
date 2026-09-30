"""Source grounding and preservation guards for abstention recovery; no inference."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from canonical_abstractions import POLICY_TEXT, POLICY_VERSION
from canonical_repair import (DIAGNOSIS_INSTRUCTIONS, REPAIR_INSTRUCTIONS, RECOVERY_POLICY_VERSION,
                              diagnosis_prompt, repair_prompt, validate_diagnosis, validate_proposal, validate_repair)
from canonical_tlr import tlr_context, validate_tlr


def fixture():
    sources = [
        {'id': 'R0', 'text': 'Battery voltage shall be at most 28 V.',
         'source': {'context': {'environment': 'Voltage observations lie between minus 100 V and 100 V.'}}},
        {'id': 'R1', 'text': 'The library shall support sending unicast messages.',
         'source': {'context': {'definition': 'Unicast has one sender and one destination.',
                                'source_only_key': 'Names identify the operation, not its execution.'}}},
        {'id': 'R2', 'text': 'The alarm shall eventually stop and remain stopped.', 'source': {'section': '3.2'}},
        {'id': 'R3', 'text': 'The library shall be compatible with the external API.',
         'source': {'context': {'dependency': 'The API contract has not been supplied.'}}},
    ]
    payload = {'schema': 'mbse_tlr/1', 'abstraction_policy': POLICY_VERSION,
        'variables': [
            {'name': 'voltage', 'type': 'Real', 'unit': 'V', 'bounds': {'lower': '-100', 'upper': '100'},
             'description': 'Observed battery voltage.'},
            {'name': 'power_available', 'type': 'Bool', 'description': 'External power availability.'}],
        'assumptions': [
            {'id': 'ENV1', 'text': 'The scenario includes available external power.', 'predicate': {'var': 'power_available'}},
            {'id': 'ENV2', 'text': 'The observed voltage is nonnegative in this scenario.',
             'predicate': {'op': '>=', 'args': [{'var': 'voltage'}, {'value': '0', 'unit': 'V'}]}}],
        'requirements': [
            {'id': 'R0', 'status': 'supported', 'formula': {'op': '<=', 'args': [{'var': 'voltage'}, {'value': '28', 'unit': 'V'}]},
             'abstraction': {'kind': 'state_constraint', 'meaning': 'Battery voltage is bounded above.',
                             'scope': 'One voltage observation.', 'limitations': []}},
            {'id': 'R1', 'status': 'unsupported', 'reason_code': 'profile_limit',
             'reason': 'Initial proposal incorrectly required an implementation before representing availability.'},
            {'id': 'R2', 'status': 'unsupported', 'reason_code': 'profile_limit',
             'reason': 'Eventuality and persistence are outside the static profile.'},
            {'id': 'R3', 'status': 'unresolved', 'reason_code': 'missing_context',
             'reason': 'The external API contract is absent.'},
        ]}
    before = validate_tlr(payload, sources, require_abstractions=True)
    diagnosis = {'schema': 'abstention_diagnosis/1', 'requirements': [
        {'id': 'R1', 'decision': 'repair', 'reason': 'The source asks for a named capability, not implemented delivery.',
         'rule': 'capability', 'source_basis': [{'source_id': 'R1', 'quote': sources[1]['text']},
             {'source_id': 'R1', 'quote': 'Unicast has one sender and one destination.'}],
         'repair_instruction': 'Require availability of the unicast sending operation and disclose execution limits.'},
        {'id': 'R2', 'decision': 'retain_unsupported', 'reason': 'Temporal history is outside the profile.',
         'rule': None, 'source_basis': [{'source_id': 'R2', 'quote': sources[2]['text']}], 'repair_instruction': None},
        {'id': 'R3', 'decision': 'needs_clarification', 'reason': 'Request the missing API contract.',
         'rule': None, 'source_basis': [], 'repair_instruction': None},
    ]}
    return sources, before, diagnosis


def repaired(before):
    after = deepcopy(before)
    after['variables'].append({'name': 'unicast_send_available', 'type': 'Bool',
                              'description': 'The library offers the operation to send a unicast message.'})
    row = after['requirements'][1]
    row.pop('reason'); row.pop('reason_code')
    row.update(status='supported', formula={'var': 'unicast_send_available'},
        abstraction={'kind': 'capability', 'subject': 'library', 'operation': 'send a unicast message',
                     'symbol': 'unicast_send_available', 'meaning': 'The sending operation is available.',
                     'scope': 'Declared library capability.', 'limitations': ['No invocation, delivery or implementation is proved.']})
    return validate_tlr(after, require_abstractions=True)


def proposal(sources, before, after=None):
    candidate = repaired(before) if after is None else deepcopy(after)
    rows = {r['id']: r for r in candidate['requirements']}
    sources_by_id = {r['id']: r for r in sources}
    reviews = []
    for target in before['requirements']:
        if target['status'] == 'supported':
            continue
        rid = target['id']
        supported = rows[rid]['status'] == 'supported'
        reviews.append({'id': rid, 'outcome': 'proposed' if supported else 'retained',
            'considered_rules': {
                'state_constraint': 'The clause is about operation availability or semantics extending beyond a single scalar observation.',
                'capability': 'Named operation availability is sufficient for R1; it would project away the temporal or external-contract obligations in the other clauses.',
                'event_relation': 'A single symbolic occurrence would not supply the missing history or external compatibility contract.'},
            'reason': 'Named unicast sending availability can be stated without proving delivery.' if supported else target['reason'],
            'source_basis': [{'source_id': rid, 'quote': sources_by_id[rid]['text']}],
            'blocking_detail': None if supported else ('Temporal eventuality and persistence need relations across states.' if rid == 'R2' else 'An external API contract is needed to define compatibility.')})
    return {'schema': 'abstention_proposal/1', 'tlr': candidate, 'reviews': reviews}


class ProposalTests(unittest.TestCase):
    def test_complete_review_is_copied_and_has_no_diagnosis_veto(self):
        sources, before, _ = fixture(); raw = proposal(sources, before)
        snapshot = deepcopy(raw)
        checked = validate_proposal(raw, sources, before)
        self.assertEqual(checked, raw)
        self.assertEqual(raw, snapshot)
        self.assertEqual(validate_repair(before, checked['tlr'])['recovered_ids'], ['R1'])
        checked['reviews'][0]['reason'] = 'Changed returned copy'
        self.assertNotEqual(checked, raw)

    def test_complete_retention_requires_full_reviews_and_keeps_records(self):
        sources, before, _ = fixture(); raw = proposal(sources, before, before)
        checked = validate_proposal(raw, sources, before)
        self.assertEqual([r['outcome'] for r in checked['reviews']], ['retained'] * 3)
        self.assertFalse(validate_repair(before, checked['tlr'])['progress'])

    def test_review_order_is_canonicalized_to_current_abstentions(self):
        sources, before, _ = fixture(); raw = proposal(sources, before)
        raw['reviews'].reverse()
        self.assertEqual([r['id'] for r in validate_proposal(raw, sources, before)['reviews']], ['R1', 'R2', 'R3'])

    def test_review_coverage_excludes_supported_unknown_duplicate_and_missing_ids(self):
        sources, before, _ = fixture()
        for kind in ('supported', 'unknown', 'duplicate', 'missing'):
            raw = proposal(sources, before)
            if kind == 'missing': raw['reviews'].pop()
            elif kind == 'duplicate': raw['reviews'].append(deepcopy(raw['reviews'][0]))
            else: raw['reviews'][0]['id'] = 'R0' if kind == 'supported' else 'UNKNOWN'
            with self.subTest(kind=kind), self.assertRaises(ValueError): validate_proposal(raw, sources, before)

    def test_wrapper_and_complete_tlr_schema_are_strict(self):
        sources, before, _ = fixture()
        variants=[]
        raw=proposal(sources,before);raw.pop('reviews');variants.append(raw)
        raw=proposal(sources,before);raw['schema']='wrong';variants.append(raw)
        raw=proposal(sources,before);raw['solver_feedback']={'status':'sat'};variants.append(raw)
        raw=proposal(sources,before);raw['tlr'].pop('variables');variants.append(raw)
        raw=proposal(sources,before);raw['tlr']['schema']='wrong';variants.append(raw)
        raw=proposal(sources,before);raw['tlr']['abstraction_policy']='mbse_abstraction/2';variants.append(raw)
        raw=proposal(sources,before);raw['tlr']['assumptions']={};variants.append(raw)
        for raw in variants:
            with self.subTest(raw=raw), self.assertRaises(ValueError): validate_proposal(raw,sources,before)

    def test_proposed_tlr_requirement_ids_and_statuses_are_checked(self):
        sources, before, _ = fixture()
        for kind in ('missing','duplicate','rename','status'):
            raw=proposal(sources,before)
            if kind=='missing':raw['tlr']['requirements'].pop()
            elif kind=='duplicate':raw['tlr']['requirements'].append(deepcopy(raw['tlr']['requirements'][0]))
            elif kind=='rename':raw['tlr']['requirements'][0]['id']='NEW'
            else:raw['tlr']['requirements'][0]['status']='approved'
            with self.subTest(kind=kind), self.assertRaises(ValueError):validate_proposal(raw,sources,before)

    def test_each_supported_rule_needs_an_explanation(self):
        sources, before, _ = fixture()
        for kind in ('missing','extra','empty','shortcut','nontext'):
            raw=proposal(sources,before);rules=raw['reviews'][0]['considered_rules']
            if kind=='missing':rules.pop('capability')
            elif kind=='extra':rules['temporal']='Not in the declared profile.'
            else:rules['capability']={'empty':'','shortcut':'not applicable','nontext':True}[kind]
            with self.subTest(kind=kind),self.assertRaises(ValueError):validate_proposal(raw,sources,before)

    def test_retention_needs_a_specific_blocker_and_cannot_use_reason_shortcuts(self):
        sources, before, _=fixture()
        for field,value in [('blocking_detail',None),('blocking_detail','unsupported'),('blocking_detail','outside profile'),('reason','see above')]:
            raw=proposal(sources,before);raw['reviews'][1][field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):validate_proposal(raw,sources,before)
        raw=proposal(sources,before);raw['reviews'][0]['blocking_detail']='A blocking problem remains.'
        with self.assertRaisesRegex(ValueError,'blocking_detail=null'):validate_proposal(raw,sources,before)

    def test_proposed_and_retained_reviews_both_require_own_literal_source(self):
        sources, before, _=fixture()
        for index in (0,1):
            for basis in ([],[{'source_id':'R0','quote':sources[0]['text']}],[{'source_id':'R1','quote':'Unicast has one sender and one destination.'}]):
                raw=proposal(sources,before);raw['reviews'][index]['source_basis']=basis
                with self.subTest(index=index,basis=basis),self.assertRaises(ValueError):validate_proposal(raw,sources,before)

    def test_fabricated_unknown_or_mapping_key_quotes_are_rejected(self):
        sources,before,_=fixture()
        for basis in ({'source_id':'R1','quote':'Unicast always arrives.'},{'source_id':'UNKNOWN','quote':'anything'}, {'source_id':'R1','quote':'source_only_key'}):
            raw=proposal(sources,before);raw['reviews'][0]['source_basis'].append(basis)
            with self.subTest(basis=basis),self.assertRaises(ValueError):validate_proposal(raw,sources,before)

    def test_real_supplementary_context_values_are_allowed(self):
        sources,before,_=fixture();raw=proposal(sources,before)
        raw['reviews'][0]['source_basis'].append({'source_id':'R1','quote':'Unicast has one sender and one destination.'})
        self.assertEqual(validate_proposal(raw,sources,before),raw)

    def test_review_outcome_must_match_raw_candidate_status(self):
        sources,before,_=fixture()
        for index,outcome in ((0,'retained'),(1,'proposed'),(0,'approved')):
            raw=proposal(sources,before);raw['reviews'][index]['outcome']=outcome
            with self.subTest(index=index,outcome=outcome),self.assertRaises(ValueError):validate_proposal(raw,sources,before)

    def test_type_and_formula_validation_remain_the_controller_responsibility(self):
        sources,before,_=fixture();raw=proposal(sources,before)
        raw['tlr']['requirements'][1]['formula']={'var':'UNDECLARED'}
        checked=validate_proposal(raw,sources,before)
        with self.assertRaises(ValueError):validate_tlr(checked['tlr'],require_abstractions=True)

    def test_attempted_source_rewrites_survive_wrapper_for_guard_rejection(self):
        sources,before,_=fixture();raw=proposal(sources,before)
        raw['tlr']['requirements'][1]['source']={'invented':'A new premise.'}
        checked=validate_proposal(raw,sources,before)
        self.assertEqual(checked['tlr']['requirements'][1]['source'],{'invented':'A new premise.'})
        with self.assertRaisesRegex(ValueError,'source text or context'):validate_repair(before,checked['tlr'])

    def test_source_free_output_tlr_rebinds_before_the_full_preservation_guard(self):
        sources, before, _ = fixture(); raw = proposal(sources, before)
        full_candidate = deepcopy(raw['tlr'])
        for row in raw['tlr']['requirements']:
            row.pop('text', None); row.pop('source', None)
        checked = validate_proposal(raw, sources, before)
        self.assertTrue(all('text' not in row and 'source' not in row for row in checked['tlr']['requirements']))
        rebound = validate_tlr(checked['tlr'], sources, require_abstractions=True)
        self.assertEqual(rebound, full_candidate)
        self.assertEqual(validate_repair(before, rebound)['recovered_ids'], ['R1'])

    def test_all_supported_input_requires_an_empty_review_array(self):
        sources,before,_=fixture();before['requirements']=before['requirements'][:1]
        raw={'schema':'abstention_proposal/1','tlr':before,'reviews':[]}
        self.assertEqual(validate_proposal(raw,sources[:1],before)['reviews'],[])

    def test_recovery_instructions_distinguish_preparation_notes_and_implementation(self):
        self.assertEqual(RECOVERY_POLICY_VERSION,'source_grounded_abstention_recovery/2')
        for phrase in ('advisory, not a veto','nonbinding questions','concrete identifier-to-network-address mapping',
                       'architecture option','No new bounds or assumptions'):
            self.assertIn(phrase,REPAIR_INSTRUCTIONS)
        self.assertIn('grounded proposal reviews',__import__('canonical_repair').__doc__)


class DiagnosisTests(unittest.TestCase):
    def test_capability_recovery_and_genuine_limits_remain_separate(self):
        sources, before, diagnosis = fixture()
        snapshot = deepcopy((sources, before, diagnosis))
        actual = validate_diagnosis(diagnosis, sources, before)
        self.assertEqual(actual, diagnosis)
        self.assertEqual((sources, before, diagnosis), snapshot)
        actual['requirements'][0]['reason'] = 'Changed returned copy'
        self.assertNotEqual(actual, diagnosis)

    def test_abstained_ids_are_complete_unique_and_exclude_supported_ids(self):
        sources, before, diagnosis = fixture()
        variants = []
        missing = deepcopy(diagnosis); missing['requirements'].pop(); variants.append(missing)
        duplicate = deepcopy(diagnosis); duplicate['requirements'].append(deepcopy(duplicate['requirements'][0])); variants.append(duplicate)
        for rid in ('R0', 'UNKNOWN'):
            changed = deepcopy(diagnosis); changed['requirements'][0]['id'] = rid; variants.append(changed)
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                validate_diagnosis(variant, sources, before)

    def test_diagnosis_reordering_is_canonicalized_to_accepted_target_order(self):
        sources, before, diagnosis = fixture()
        diagnosis['requirements'].reverse()
        self.assertEqual([r['id'] for r in validate_diagnosis(diagnosis, sources, before)['requirements']], ['R1', 'R2', 'R3'])

    def test_fabricated_unknown_and_mapping_key_quotes_are_rejected(self):
        sources, before, diagnosis = fixture()
        for basis in ({'source_id': 'R1', 'quote': 'Unicast always arrives.'},
                      {'source_id': 'UNKNOWN', 'quote': sources[1]['text']},
                      {'source_id': 'R1', 'quote': 'source_only_key'}):
            changed = deepcopy(diagnosis); changed['requirements'][0]['source_basis'].append(basis)
            with self.subTest(basis=basis), self.assertRaises(ValueError):
                validate_diagnosis(changed, sources, before)

    def test_repair_requires_own_target_text_not_context_or_neighbors_only(self):
        sources, before, diagnosis = fixture()
        for basis in ([], [{'source_id': 'R1', 'quote': 'Unicast has one sender and one destination.'}],
                      [{'source_id': 'R0', 'quote': sources[0]['text']}]):
            changed = deepcopy(diagnosis); changed['requirements'][0]['source_basis'] = basis
            with self.subTest(basis=basis), self.assertRaisesRegex(ValueError, 'own source text'):
                validate_diagnosis(changed, sources, before)

    def test_supplementary_neighbor_context_quote_is_allowed(self):
        sources, before, diagnosis = fixture()
        diagnosis['requirements'][0]['source_basis'].append({'source_id': 'R3', 'quote': 'The API contract has not been supplied.'})
        self.assertEqual(validate_diagnosis(diagnosis, sources, before), diagnosis)

    def test_rule_instruction_decision_and_schema_are_strict(self):
        sources, before, diagnosis = fixture()
        edits = [('decision', 'approved'), ('rule', None), ('rule', 'temporal'), ('rule', []),
                 ('repair_instruction', None), ('reason', '')]
        for key, value in edits:
            changed = deepcopy(diagnosis); changed['requirements'][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_diagnosis(changed, sources, before)
        for changed in ({'schema': 'wrong', 'requirements': diagnosis['requirements']},
                        {**diagnosis, 'semantic_fidelity': 'proved'}):
            with self.assertRaises(ValueError): validate_diagnosis(changed, sources, before)

    def test_retained_decisions_cannot_smuggle_repair_instructions(self):
        sources, before, diagnosis = fixture()
        for field, value in [('rule', 'capability'), ('repair_instruction', 'Replace history with a support flag.')]:
            changed = deepcopy(diagnosis); changed['requirements'][1][field] = value
            with self.assertRaisesRegex(ValueError, 'null'):
                validate_diagnosis(changed, sources, before)

    def test_fixed_source_text_context_and_ids_cannot_drift(self):
        sources, before, diagnosis = fixture()
        for field, value in [('text', 'A changed source'), ('source', {'context': {'new': 'A new premise'}}), ('id', 'RENAMED')]:
            changed = deepcopy(before); changed['requirements'][1][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_diagnosis(diagnosis, sources, changed)

    def test_prompts_contain_only_permitted_inputs_and_relevant_freezes(self):
        sources, before, diagnosis = fixture()
        prompt = json.loads(diagnosis_prompt(sources, None, before, 'Previous proposal omitted a declared type.'))
        self.assertEqual(prompt['source_packet'], sources)
        self.assertEqual(prompt['abstained_requirement_ids'], ['R1', 'R2', 'R3'])
        self.assertEqual(prompt['previous_failure'], 'Previous proposal omitted a declared type.')
        self.assertFalse({'judges', 'reference_formulas', 'mutations', 'audit', 'solver_result'} & set(prompt))
        proposal = json.loads(repair_prompt(sources, None, before, diagnosis))
        self.assertEqual(proposal['eligible_requirement_ids'], ['R1', 'R2', 'R3'])
        expected = deepcopy(before)
        for row in expected['requirements']:
            row.pop('text', None); row.pop('source', None)
        self.assertEqual(proposal['accepted_tlr'], expected)
        self.assertIn('entire existing abstention record unchanged', ' '.join(proposal['freeze_rules']))
        self.assertIn(POLICY_TEXT, DIAGNOSIS_INSTRUCTIONS)
        self.assertIn(POLICY_TEXT, REPAIR_INSTRUCTIONS)
        self.assertEqual(proposal['recovery_policy'], RECOVERY_POLICY_VERSION)
        self.assertIn('advisory', proposal['diagnosis_role'].lower())
        self.assertIn('nonbinding', ' '.join(proposal['review_rules']))
        self.assertIn('Literal', __import__('canonical_repair').__doc__)

    def test_both_prompts_remove_only_rebound_tlr_source_fields(self):
        sources, before, diagnosis = fixture()
        snapshot = deepcopy((sources, before, diagnosis))
        expected = deepcopy(before)
        for row in expected['requirements']:
            row.pop('text', None); row.pop('source', None)
        for name, build in (('diagnosis', lambda: diagnosis_prompt(sources, None, before)),
                            ('repair', lambda: repair_prompt(sources, None, before, diagnosis))):
            with self.subTest(prompt=name):
                payload = json.loads(build())
                self.assertEqual(payload['source_packet'], sources)
                self.assertEqual(payload['accepted_tlr'], expected)
                self.assertEqual(payload['accepted_tlr']['assumptions'], before['assumptions'])
                self.assertIn('before preservation checks', payload['source_metadata_policy'])
                self.assertIn('omit text and source', payload['source_metadata_policy'])
        self.assertEqual((sources, before, diagnosis), snapshot)
        self.assertIn('omit text and source', DIAGNOSIS_INSTRUCTIONS)
        self.assertIn('omit text and source', REPAIR_INSTRUCTIONS)

    def test_full_source_validation_precedes_model_tlr_projection(self):
        sources, before, diagnosis = fixture()
        for field, value in (('text', 'Invented source wording.'), ('source', {'context': 'Invented assumption.'})):
            changed = deepcopy(before); changed['requirements'][1][field] = value
            for build in (lambda: diagnosis_prompt(sources, None, changed),
                          lambda: repair_prompt(sources, None, changed, diagnosis)):
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'fixed source'):
                    build()

    def test_legacy_profile_is_not_silently_upgraded(self):
        sources, before, diagnosis = fixture()
        before.pop('abstraction_policy')
        with self.assertRaisesRegex(ValueError, 'current abstraction_policy'):
            diagnosis_prompt(sources, None, before)

    def test_all_supported_input_needs_an_empty_diagnosis(self):
        sources, before, _ = fixture()
        one = deepcopy(before); one['requirements'] = one['requirements'][:1]
        result = validate_diagnosis({'schema': 'abstention_diagnosis/1', 'requirements': []}, sources[:1], one)
        self.assertEqual(result['requirements'], [])


class RepairGuardTests(unittest.TestCase):
    def test_recovers_only_source_grounded_target_and_preserves_all_inputs(self):
        sources, before, diagnosis = fixture(); after = repaired(before)
        diagnosis = validate_diagnosis(diagnosis, sources, before)
        snapshot = deepcopy((before, after, diagnosis))
        with patch('mutation_core._execute', side_effect=AssertionError('No solver call belongs in this guard')):
            result = validate_repair(before, after, diagnosis)
        self.assertEqual(result, {'recovered_ids': ['R1'], 'retained_ids': ['R2', 'R3'],
                                  'added_symbols': ['unicast_send_available'], 'progress': True})
        self.assertEqual((before, after, diagnosis), snapshot)

    def test_all_abstained_empty_vocabulary_can_recover_without_dummy_symbols(self):
        sources, before, diagnosis = fixture()
        after = repaired(before)
        before['requirements'] = before['requirements'][1:]
        before['variables'] = []
        before['assumptions'] = []
        after['requirements'] = after['requirements'][1:]
        after['variables'] = [v for v in after['variables'] if v['name'] == 'unicast_send_available']
        after['assumptions'] = []
        diagnosed = validate_diagnosis(diagnosis, sources[1:], before)
        result = validate_repair(before, after, diagnosed)
        self.assertEqual(result['recovered_ids'], ['R1'])
        self.assertEqual(result['added_symbols'], ['unicast_send_available'])

    def test_guarded_occurrence_relation_can_be_recovered_with_defined_unbounded_symbols(self):
        sources, before, diagnosis = fixture()
        sources[1]['text'] = 'Received messages shall have their specified recipient address.'
        before['requirements'][1]['text'] = sources[1]['text']
        diagnosis['requirements'][0].update(rule='event_relation',
            repair_instruction='For one reception occurrence, require actual and specified address equality.',
            source_basis=[{'source_id': 'R1', 'quote': sources[1]['text']}])
        after = deepcopy(before)
        after['variables'].extend([
            {'name': 'received', 'type': 'Bool', 'description': 'Receipt of the selected message occurrence.'},
            {'name': 'actual_address', 'type': 'Int', 'description': 'Opaque actual recipient identity for that occurrence.'},
            {'name': 'specified_address', 'type': 'Int', 'description': 'Opaque intended recipient identity for that occurrence.'}])
        row = after['requirements'][1]; row.pop('reason'); row.pop('reason_code')
        row.update(status='supported',
            formula={'op': 'implies', 'args': [{'var': 'received'},
                {'op': '=', 'args': [{'var': 'actual_address'}, {'var': 'specified_address'}]}]},
            abstraction={'kind': 'event_relation', 'meaning': 'Receipt requires matching recipient identities.',
                'scope': 'One symbolic occurrence.', 'limitations': ['No delivery, history or timing guarantee.']})
        result = validate_repair(before, after, validate_diagnosis(diagnosis, sources, before))
        self.assertTrue(result['progress'])
        self.assertEqual(result['added_symbols'], ['received', 'actual_address', 'specified_address'])

    def test_genuine_abstentions_and_no_progress_are_retained_exactly(self):
        _, before, diagnosis = fixture()
        result = validate_repair(before, deepcopy(before), diagnosis)
        self.assertEqual(result, {'recovered_ids': [], 'retained_ids': ['R1', 'R2', 'R3'], 'added_symbols': [], 'progress': False})

    def test_omitted_current_reference_and_harmless_list_order_do_not_count_as_changes(self):
        _, before, diagnosis = fixture(); after = repaired(before)
        after['requirements'][0]['formula']['args'][0].pop('at')
        after['assumptions'][0]['predicate'].pop('at')
        after['variables'].reverse(); after['assumptions'].reverse(); after['requirements'].reverse()
        self.assertEqual(validate_repair(before, after, diagnosis)['recovered_ids'], ['R1'])

    def test_supported_formula_or_metadata_change_is_rejected(self):
        _, before, diagnosis = fixture()
        for kind in ('formula', 'metadata'):
            after = repaired(before)
            if kind == 'formula': after['requirements'][0]['formula']['args'][1]['value'] = '29'
            else: after['requirements'][0]['abstraction']['meaning'] = 'A different interpretation.'
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'preserve this requirement exactly'):
                validate_repair(before, after, diagnosis)

    def test_retained_abstention_records_cannot_be_rewritten(self):
        _, before, diagnosis = fixture()
        after = repaired(before); after['requirements'][2]['reason'] = 'History is now ignored.'
        with self.assertRaisesRegex(ValueError, 'entire abstention record'):
            validate_repair(before, after, diagnosis)

    def test_retention_or_clarification_diagnosis_does_not_veto_recovery(self):
        sources, before, diagnosis = fixture()
        for decision in ('retain_unsupported', 'needs_clarification'):
            changed = deepcopy(diagnosis)
            changed['requirements'][0].update(decision=decision, rule=None, repair_instruction=None)
            checked = validate_diagnosis(changed, sources, before)
            with self.subTest(decision=decision):
                self.assertEqual(validate_repair(before, repaired(before), checked)['recovered_ids'], ['R1'])

    def test_missing_diagnosis_does_not_veto_a_guarded_proposal(self):
        sources, before, _ = fixture()
        prompt = json.loads(repair_prompt(sources, None, before))
        self.assertIsNone(prompt['diagnosis'])
        self.assertEqual(prompt['eligible_requirement_ids'], ['R1', 'R2', 'R3'])
        self.assertEqual(validate_repair(before, repaired(before))['recovered_ids'], ['R1'])

    def test_eligible_but_unrecovered_record_cannot_be_rewritten(self):
        _, before, diagnosis = fixture(); after = deepcopy(before)
        after['requirements'][1]['reason'] = 'An improved explanation without recovery.'
        with self.assertRaisesRegex(ValueError, 'entire abstention record'):
            validate_repair(before, after, diagnosis)

    def test_recovered_target_source_text_context_and_presence_are_frozen(self):
        _, before, diagnosis = fixture()
        for field, value in [('text', 'Changed requirement'), ('source', {'section': 'invented'})]:
            after = repaired(before); after['requirements'][1][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'source text or context'):
                validate_repair(before, after, diagnosis)
        after = repaired(before); after['requirements'][1].pop('source')
        with self.assertRaisesRegex(ValueError, 'source text or context'):
            validate_repair(before, after, diagnosis)

    def test_existing_symbols_preserve_type_units_bounds_description_and_existence(self):
        _, before, diagnosis = fixture()
        for key, value in [('type', 'Int'), ('unit', 'A'), ('bounds', {'lower': '0', 'upper': '100'}),
                           ('description', 'Output voltage after required correction.')]:
            after = repaired(before); after['variables'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_repair(before, after, diagnosis)
        after = repaired(before); after['variables'].pop(0)
        with self.assertRaises(ValueError): validate_repair(before, after, diagnosis)

    def test_guarantees_cannot_be_copied_into_background_or_existing_assumptions(self):
        _, before, diagnosis = fixture()
        after = repaired(before)
        after['assumptions'].append({'id': 'NEW', 'text': 'Assume the capability exists.', 'predicate': {'var': 'unicast_send_available'}})
        with self.assertRaisesRegex(ValueError, 'background assumptions'):
            validate_repair(before, after, diagnosis)
        after = repaired(before)
        after['assumptions'][1]['predicate'] = deepcopy(before['requirements'][0]['formula'])
        with self.assertRaisesRegex(ValueError, 'background assumptions'):
            validate_repair(before, after, diagnosis)

    def test_assumption_text_identity_and_removal_are_frozen(self):
        _, before, diagnosis = fixture()
        for action in ('text', 'id', 'remove'):
            after = repaired(before)
            if action == 'remove': after['assumptions'].pop()
            else: after['assumptions'][0][action] = 'CHANGED'
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'background assumptions'):
                validate_repair(before, after, diagnosis)

    def test_fixed_context_forbids_new_variables(self):
        _, before, diagnosis = fixture()
        with self.assertRaisesRegex(ValueError, 'fixed context'):
            validate_repair(before, repaired(before), diagnosis, tlr_context(before))

    def test_fixed_context_recovery_can_use_a_predeclared_symbol(self):
        _, before, diagnosis = fixture(); after = repaired(before)
        before['variables'].append(deepcopy(after['variables'][-1]))
        context = tlr_context(before)
        context['symbol_meanings'] = {v['name']: v['description'] for v in before['variables']}
        actual = validate_repair(before, after, diagnosis, context)
        self.assertTrue(actual['progress']); self.assertEqual(actual['added_symbols'], [])
        bad = deepcopy(context); bad['symbol_meanings']['unicast_send_available'] = 'Every message already arrived.'
        with self.assertRaisesRegex(ValueError, 'fixed symbol_meanings'):
            validate_repair(before, after, diagnosis, bad)

    def test_fixed_context_itself_cannot_supply_different_background_or_domains(self):
        _, before, diagnosis = fixture()
        for field in ('background', 'variables'):
            context = tlr_context(before)
            if field == 'background': context[field].pop()
            else: context[field][0]['bounds']['upper'] = '200'
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'fixed context'):
                validate_repair(before, before, diagnosis, context)

    def test_new_unused_symbols_are_rejected(self):
        _, before, diagnosis = fixture(); after = repaired(before)
        after['variables'].append({'name': 'unused', 'type': 'Bool', 'description': 'An unused proposed condition.'})
        with self.assertRaisesRegex(ValueError, 'newly supported target'):
            validate_repair(before, after, diagnosis)
        after = deepcopy(before); after['variables'].append({'name': 'unused', 'type': 'Bool', 'description': 'No target recovered.'})
        with self.assertRaisesRegex(ValueError, 'newly supported target'):
            validate_repair(before, after, diagnosis)

    def test_new_numeric_symbol_unbounded_is_allowed_but_domain_injection_is_not(self):
        sources, before, diagnosis = fixture()
        sources[1]['text'] = 'Auxiliary voltage shall be at most 10 V.'
        before['requirements'][1]['text'] = sources[1]['text']
        diagnosis['requirements'][0].update(rule='state_constraint', repair_instruction='Constrain the auxiliary voltage to at most 10 V.',
            source_basis=[{'source_id': 'R1', 'quote': sources[1]['text']}])
        after = deepcopy(before)
        after['variables'].append({'name': 'aux_voltage', 'type': 'Real', 'unit': 'V', 'description': 'Observed auxiliary voltage.'})
        row = after['requirements'][1]; row.pop('reason'); row.pop('reason_code')
        row.update(status='supported', formula={'op': '<=', 'args': [{'var': 'aux_voltage'}, {'value': '10', 'unit': 'V'}]},
            abstraction={'kind': 'state_constraint', 'meaning': 'Auxiliary voltage bound.', 'scope': 'One observation.', 'limitations': []})
        self.assertEqual(validate_repair(before, after, diagnosis)['added_symbols'], ['aux_voltage'])
        after['variables'][-1]['bounds'] = {'upper': '10'}
        with self.assertRaisesRegex(ValueError, 'must be unbounded'):
            validate_repair(before, after, diagnosis)

    def test_diagnosed_rule_is_advisory_not_a_structural_veto(self):
        _, before, diagnosis = fixture(); after = repaired(before)
        diagnosis['requirements'][0]['rule'] = 'event_relation'
        self.assertEqual(validate_repair(before, after, diagnosis)['recovered_ids'], ['R1'])

    def test_requirement_addition_removal_and_rename_are_rejected(self):
        _, before, diagnosis = fixture()
        for action in ('add', 'remove', 'rename'):
            after = repaired(before)
            if action == 'add':
                extra = deepcopy(after['requirements'][-1]); extra['id'] = 'EXTRA'; after['requirements'].append(extra)
            elif action == 'remove': after['requirements'].pop()
            else: after['requirements'][-1]['id'] = 'RENAMED'
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'add, remove or rename'):
                validate_repair(before, after, diagnosis)

    def test_changed_or_omitted_policy_and_legacy_candidate_are_rejected(self):
        _, before, diagnosis = fixture()
        for policy in (None, 'mbse_abstraction/2'):
            after = repaired(before)
            if policy is None: after.pop('abstraction_policy')
            else: after['abstraction_policy'] = policy
            with self.assertRaisesRegex(ValueError, 'current abstraction_policy'):
                validate_repair(before, after, diagnosis)

    def test_guard_rechecks_grounding_when_accepted_tlr_contains_sources(self):
        _, before, diagnosis = fixture()
        diagnosis['requirements'][0]['source_basis'][0]['quote'] = 'Unsupported invented source wording.'
        with self.assertRaisesRegex(ValueError, 'literal source/context'):
            validate_repair(before, repaired(before), diagnosis)

    def test_temporal_next_state_or_truth_constant_cannot_be_forced_into_recovery(self):
        _, before, diagnosis = fixture()
        for formula in (True, {'var': 'unicast_send_available', 'at': 'next'}):
            after = repaired(before); after['requirements'][1]['formula'] = formula
            with self.subTest(formula=formula), self.assertRaises(ValueError):
                validate_repair(before, after, diagnosis)


if __name__ == '__main__':
    unittest.main()
