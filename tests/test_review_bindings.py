"""Exact architecture associations, compatible values, and stale/revoked reviews."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from review_behavior import validate_behavior
from review_bindings import (binding_choices, binding_targets, binding_status, binding_blockers,
                             binding_review_hash, validate_binding)
from review_contracts import build_contract_bundle
from review_model_index import build_model_inspection


MODEL = '''package Domain {
    part battery {
        attribute voltage : ISQ::ElectricPotentialValue;
        attribute unknownUnit : ScalarValues::Real;
        attribute dimensionless : ScalarValues::Real { doc /* unit: 1; */ }
        attribute current : ISQ::ElectricCurrentValue;
        attribute active : ScalarValues::Boolean;
        attribute wrongType : ScalarValues::Integer { doc /* unit: V; */ }
        attribute scaled : ScalarValues::Real { doc /* unit: mV; */ }
        attribute conflicting : ISQ::ElectricPotentialValue { doc /* unit: kg; */ }
        attribute unknownType : Vendor::Voltage { doc /* unit: V; */ }
        port outlet { attribute measured : ISQ::ElectricPotentialValue; }
    }
    part other { attribute voltage : ISQ::ElectricPotentialValue; }
    part def BatteryType { attribute voltage : ISQ::ElectricPotentialValue; }
    part batteryUsage : BatteryType;
    attribute def ValueType;
}
package ContractReview_123 {
    part FiniteTrace { attribute voltage_0 : Real; }
}
'''


def fixture():
    requirements = [{'id': 'R1', 'text': 'The battery voltage shall be at most 28 V.'}]
    behavior = validate_behavior({
        'schema': 'review_behavior/1', 'horizon': 2, 'step': {'value': '1', 'unit': 's'},
        'variables': [{'name': 'voltage', 'type': 'Real', 'role': 'state', 'unit': 'V'},
                      {'name': 'limit', 'type': 'Real', 'role': 'parameter', 'unit': 'V', 'value': '28'}],
        'initial': [], 'transitions': [],
        'properties': [{'id': 'P1', 'kind': 'always', 'requirement_ids': ['R1'],
                        'predicate': {'op': '<=', 'args': [{'var': 'voltage'}, {'var': 'limit'}]}}],
    }, ['R1'])
    return {'source_hash': 'source-a', 'evidence_hash': 'evidence-a', 'behavior': behavior,
            'requirements': requirements, 'model': {'text': MODEL},
            'contracts': build_contract_bundle(requirements, 'source-a', behavior),
            'architecture_binding_reviews': []}


def choice(run, name):
    return next(e for e in binding_choices(run) if e['qualified_name'] == name)


def payload(run, name='Domain::battery::voltage', **changes):
    return {'source_hash': run['source_hash'], 'evidence_hash': run['evidence_hash'],
            'target_kind': 'variable', 'target_id': 'voltage', 'element_id': choice(run, name)['id'],
            'decision': 'accept', 'reviewer': 'Engineer', 'rationale': 'Voltage of this battery is the behavior variable.', **changes}


class BindingTests(unittest.TestCase):
    def test_real_declarations_and_ports_are_indexed_and_projection_excluded(self):
        run = fixture()
        choices = binding_choices(run)
        self.assertTrue(any(c['kind'] == 'port' for c in choices))
        self.assertFalse(any('ContractReview_' in c['qualified_name'] for c in choices))
        self.assertFalse(any(c['qualified_name'].endswith('ValueType') for c in choices))
        self.assertEqual(choice(run, 'Domain::battery::voltage')['type'], 'Real')
        self.assertEqual(choice(run, 'Domain::battery::voltage')['unit'], 'V')
        self.assertEqual(choice(run, 'Domain::battery::unknownUnit')['metadata_status'], 'unresolved')

    def test_accept_binds_exact_fingerprints_without_mutating_model(self):
        run = fixture()
        before = deepcopy(run)
        record = validate_binding(run, payload(run))
        self.assertEqual(run, before)
        self.assertEqual(record['compatibility']['status'], 'recognized_compatible')
        self.assertEqual(record['model_sha256'], binding_status(run)['model_sha256'])
        self.assertIn('not proof', record['scope'])
        self.assertTrue(record['behavior_sha256'])
        self.assertTrue(record['target_sha256'])

    def test_known_mismatched_type_or_unit_cannot_be_acknowledged_away(self):
        run = fixture()
        for attr in ['wrongType', 'current', 'active', 'dimensionless', 'scaled', 'conflicting']:
            with self.subTest(attr=attr), self.assertRaisesRegex(ValueError, 'Incompatible'):
                validate_binding(run, payload(run, 'Domain::battery::' + attr,
                                             acknowledge_unresolved_compatibility=True))

    def test_unknown_metadata_requires_explicit_acknowledgment_and_stays_unresolved(self):
        run = fixture()
        for attr in ['unknownUnit', 'unknownType']:
            with self.subTest(attr=attr):
                data = payload(run, 'Domain::battery::' + attr)
                with self.assertRaisesRegex(ValueError, 'Compatibility is unresolved'):
                    validate_binding(run, data)
                data['acknowledge_unresolved_compatibility'] = True
                record = validate_binding(run, data)
                self.assertEqual(record['compatibility']['status'], 'unresolved')
                self.assertTrue(record['compatibility']['issues'])
        with self.assertRaisesRegex(ValueError, 'Boolean'):
            validate_binding(run, payload(run, acknowledge_unresolved_compatibility='yes'))

    def test_boolean_binding_rejects_known_physical_units_even_if_type_is_unknown(self):
        run = fixture()
        run['behavior']['variables'][0] = {'name': 'voltage', 'type': 'Bool', 'role': 'state'}
        with self.assertRaisesRegex(ValueError, 'Boolean variable'):
            validate_binding(run, payload(run, 'Domain::battery::unknownType', acknowledge_unresolved_compatibility=True))
        record = validate_binding(run, payload(run, 'Domain::battery::active'))
        self.assertEqual(record['compatibility']['status'], 'recognized_compatible')

    def test_known_string_attribute_is_incompatible_even_with_unknown_acknowledgment(self):
        run = fixture()
        run['model']['text'] = run['model']['text'].replace(
            'attribute unknownType : Vendor::Voltage', 'attribute unknownType : ScalarValues::String')
        self.assertEqual(choice(run, 'Domain::battery::unknownType')['type'], 'String')
        for variable_type in ['Real', 'Int', 'Bool']:
            with self.subTest(variable_type=variable_type):
                run['behavior']['variables'][0]['type'] = variable_type
                with self.assertRaisesRegex(ValueError, 'differs from SysML type String'):
                    validate_binding(run, payload(run, 'Domain::battery::unknownType',
                                                 acknowledge_unresolved_compatibility=True))

    def test_container_requires_a_real_contained_value(self):
        run = fixture()
        data = payload(run, 'Domain::battery')
        with self.assertRaisesRegex(ValueError, 'contained value attribute'):
            validate_binding(run, data)
        data['value_element_id'] = choice(run, 'Domain::battery::voltage')['id']
        record = validate_binding(run, data)
        self.assertEqual(record['qualified_name'], 'Domain::battery')
        self.assertEqual(record['value_qualified_name'], 'Domain::battery::voltage')
        data['value_element_id'] = choice(run, 'Domain::other::voltage')['id']
        with self.assertRaisesRegex(ValueError, 'contained value attribute'):
            validate_binding(run, data)
        data['element_id'] = choice(run, 'Domain::battery::outlet')['id']
        data['value_element_id'] = choice(run, 'Domain::battery::outlet::measured')['id']
        self.assertEqual(validate_binding(run, data)['element_kind'], 'port')

    def test_inherited_usage_path_is_not_invented(self):
        run = fixture()
        data = payload(run, 'Domain::batteryUsage', value_element_id=choice(run, 'Domain::BatteryType::voltage')['id'])
        with self.assertRaisesRegex(ValueError, 'inherited paths are not resolved'):
            validate_binding(run, data)

    def test_duplicate_qualified_names_and_descendants_are_not_selectable(self):
        run = fixture()
        original_id = choice(run, 'Domain::battery::voltage')['id']
        run['model']['text'] = run['model']['text'].replace('    part other', '    part battery { attribute extra : Real; }\n    part other')
        self.assertFalse(any(c['qualified_name'].startswith('Domain::battery::') for c in binding_choices(run)))
        with self.assertRaisesRegex(ValueError, 'unambiguous domain'):
            validate_binding(run, {**payload(run, 'Domain::other::voltage'), 'element_id': original_id})

    def test_saved_inspection_cannot_invent_a_bindable_element(self):
        run = fixture()
        run['model']['inspection'] = {'elements': [{'id': 'fake', 'kind': 'attribute', 'qualified_name': 'P::fake'}]}
        with self.assertRaisesRegex(ValueError, 'unambiguous domain'):
            validate_binding(run, {**payload(run), 'element_id': 'fake'})
        projection = next(e for e in build_model_inspection(MODEL, [], {})['elements'] if e['name'] == 'voltage_0')
        with self.assertRaisesRegex(ValueError, 'unambiguous domain'):
            validate_binding(run, {**payload(run), 'element_id': projection['id']})

    def test_model_and_behavior_changes_make_old_acceptances_stale(self):
        for change in ['model', 'behavior', 'contract', 'source', 'evidence']:
            with self.subTest(change=change):
                run = fixture()
                data = payload(run)
                if change == 'contract':
                    data.update(target_kind='contract', target_id='CONTRACT-P1')
                record = validate_binding(run, data)
                run['architecture_binding_reviews'].append(record)
                if change == 'model':
                    run['model']['text'] += '\n// new exact model revision\n'
                elif change == 'behavior':
                    run['behavior']['horizon'] = 3
                elif change == 'contract':
                    run['contracts']['contracts'][0]['property']['predicate']['op'] = '<'
                else:
                    run[change + '_hash'] += '-changed'
                target = next(t for t in binding_status(run)['targets'] if t['target_kind'] == data['target_kind'] and t['target_id'] == data['target_id'])
                self.assertEqual(target['review_status'], 'stale')
                self.assertTrue(target['issue'])

    def test_later_rejection_or_deferral_revokes_accepted_mapping(self):
        for decision in ['reject', 'defer']:
            with self.subTest(decision=decision):
                run = fixture()
                run['architecture_binding_reviews'].append(validate_binding(run, payload(run)))
                target = next(t for t in binding_status(run)['targets'] if t['target_id'] == 'voltage')
                self.assertEqual(target['review_status'], 'accept')
                previous_hash = binding_review_hash(run['architecture_binding_reviews'])
                run['architecture_binding_reviews'].append(validate_binding(run, payload(run, decision=decision)))
                target = next(t for t in binding_status(run)['targets'] if t['target_id'] == 'voltage')
                self.assertEqual(target['review_status'], decision)
                self.assertNotEqual(previous_hash, binding_review_hash(run['architecture_binding_reviews']))
                self.assertTrue(any('voltage' in blocker for blocker in binding_blockers(run)))

    def test_required_targets_and_contract_ownership(self):
        run = fixture()
        self.assertEqual([t['target_id'] for t in binding_targets(run) if t['required']], ['voltage', 'CONTRACT-P1'])
        run['architecture_binding_reviews'].append(validate_binding(run, payload(run)))
        contract = validate_binding(run, payload(run, 'Domain::battery::outlet', target_kind='contract', target_id='CONTRACT-P1'))
        self.assertEqual(contract['compatibility']['status'], 'association_only')
        run['architecture_binding_reviews'].append(contract)
        self.assertEqual(binding_blockers(run), [])

    def test_missing_fingerprints_or_stale_request_cannot_be_accepted(self):
        run = fixture()
        for changes in [{'source_hash': 'old'}, {'evidence_hash': 'old'}, {'model_sha256': 'old'},
                        {'behavior_sha256': 'old'}, {'target_sha256': 'old'}, {'target_id': 'absent'}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_binding(run, payload(run, **changes))
        record = validate_binding(run, payload(run))
        del record['target_sha256']
        run['architecture_binding_reviews'].append(record)
        self.assertEqual(next(t for t in binding_status(run)['targets'] if t['target_id'] == 'voltage')['review_status'], 'stale')

    def test_scalar_contract_attribute_binding_checks_known_units(self):
        run = fixture()
        row = run['contracts']['contracts'][0]
        row.update(property=None, scalar={'type': 'Real', 'unit': 'V', 'symbol': 'voltage', 'relation': 'le', 'value': '28'})
        data = payload(run, 'Domain::battery::current', target_kind='contract', target_id='CONTRACT-P1')
        with self.assertRaisesRegex(ValueError, 'Incompatible'):
            validate_binding(run, data)
        data['element_id'] = choice(run, 'Domain::battery::voltage')['id']
        self.assertEqual(validate_binding(run, data)['compatibility']['status'], 'recognized_compatible')

    def test_requirement_parameters_and_trace_evidence_are_excluded(self):
        run = fixture()
        run['model']['text'] += '''package Domain_trace { part traceOwner { attribute number : Real; } }
package More { requirement Source { attribute limit : Real; }
part def SolverEvidence { attribute result : Boolean; }
part proof : SolverEvidence;
package ContractReview_nested { part trace; }
}'''
        choices = binding_choices(run)
        self.assertFalse(any(c['qualified_name'].startswith(('Domain_trace', 'More')) for c in choices))

    def test_reject_can_record_an_incompatible_proposal_without_accepting_it(self):
        run = fixture()
        record = validate_binding(run, payload(run, 'Domain::battery::wrongType', decision='reject'))
        self.assertEqual(record['compatibility']['status'], 'incompatible')
        self.assertEqual(record['decision'], 'reject')


if __name__ == '__main__':
    unittest.main()
