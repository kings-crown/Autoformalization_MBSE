"""Regression checks for review diagnostics and safe, provenance-bound edits."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from review_behavior import validate_behavior
from review_candidate import edit_candidate, inspect_candidate, parse_expression, render_expression
from review_contracts import _hash

SOURCES = [{'id': 'R1', 'text': 'Voltage shall remain at most 28 V.'}, {'id': 'R2', 'text': 'Respond to a command.'}]


def op(name, *args):
    return {'op': name, 'args': list(args)}


def v(name='voltage', at='current'):
    return {'var': name, 'at': at}


def n(value, unit='V'):
    return {'value': str(value), 'unit': unit}


def candidate():
    return {'schema': 'review_behavior/1', 'horizon': 2, 'step': {'value': '1', 'unit': 's'},
            'variables': [{'name': 'voltage', 'type': 'Real', 'role': 'state', 'unit': 'V'}],
            'initial': [op('=', v(), n(27))],
            'transitions': [op('=', v(at='next'), op('+', v(), n(1)))],
            'assumptions': [],
            'properties': [{'id': 'P_voltage', 'kind': 'always', 'requirement_ids': ['R1'], 'predicate': op('<=', v(), n(28))}]}


class CandidateInspectionTests(unittest.TestCase):
    def test_rows_are_source_linked_but_do_not_invent_provenance(self):
        behavior = candidate()
        snapshot = copy.deepcopy(behavior)
        result = inspect_candidate(behavior, SOURCES)
        self.assertEqual(behavior, snapshot)
        self.assertEqual(result['schema'], 'review_candidate/1')
        self.assertEqual(result['behavior_sha256'], _hash(validate_behavior(behavior, ['R1', 'R2'])))
        row = next(r for r in result['rows'] if r['pointer'] == '/transitions/0')
        self.assertEqual(row['expression'], '(next(voltage) = (voltage + 1[V]))')
        self.assertEqual(row['requirement_ids'], ['R1'])
        self.assertEqual(row['provenance'], {'origin': 'unspecified', 'requirement_ids': [], 'rationale': ''})
        self.assertEqual(result['diagnostics'], [])
        self.assertTrue(all(r['provenance']['origin'] == 'llm' for r in inspect_candidate(behavior, SOURCES, origin='llm_proposed_reviewed')['rows']))

    def test_missing_updates_initial_values_and_transition_assumptions(self):
        behavior = candidate()
        behavior['variables'].append({'name': 'response', 'type': 'Bool', 'role': 'output'})
        behavior['variables'].append({'name': 'disturbance', 'type': 'Real', 'role': 'disturbance'})
        behavior['initial'] = []
        behavior['transitions'] = []
        behavior['assumptions'] = [{'id': 'A_step', 'text': 'Voltage persists', 'scope': 'transition', 'predicate': op('=', v(at='next'), v())}]
        result = inspect_candidate(behavior, SOURCES)
        updates = [d['variable'] for d in result['diagnostics'] if d['code'] == 'MISSING_STATE_UPDATE']
        self.assertEqual(updates, ['response'])
        self.assertEqual({d['variable'] for d in result['diagnostics'] if d['code'] == 'MISSING_INITIAL_CONSTRAINT'}, {'voltage', 'response'})

    def test_guarantees_copied_into_bounds_or_conjunctive_always_assumptions(self):
        behavior = candidate()
        behavior['variables'][0]['bounds'] = {'upper': '28'}
        behavior['assumptions'] = [{'id': 'A_limit', 'text': 'Questionable environmental premise', 'scope': 'always',
                                    'predicate': op('and', op('>=', n(28000, 'mV'), v()), op('>=', v(), n(0)))}]
        result = inspect_candidate(behavior, SOURCES)
        matches = [d for d in result['diagnostics'] if d['code'] == 'GUARANTEE_IN_ASSUMPTION']
        self.assertEqual(len(matches), 2)
        self.assertEqual({d['pointers'][1] for d in matches}, {'/variables/0/bounds/upper', '/assumptions/0/predicate'})
        self.assertTrue(all(d['property_id'] == 'P_voltage' for d in matches))
        # Inspection cannot silently remove the questionable premise.
        self.assertEqual(result['behavior_sha256'], _hash(validate_behavior(behavior, ['R1', 'R2'])))

    def test_complement_normalization_and_guarantee_conjuncts(self):
        behavior = candidate()
        behavior['properties'][0]['predicate'] = op('and', op('<=', v(), n(28)), op('>=', v(), n(0)))
        behavior['assumptions'] = [{'id': 'A_limit', 'text': 'Premise', 'scope': 'always', 'predicate': op('not', op('>', v(), n(28)))}]
        result = inspect_candidate(behavior, SOURCES)
        self.assertEqual(len([d for d in result['diagnostics'] if d['code'] == 'GUARANTEE_IN_ASSUMPTION']), 1)

    def test_initial_or_different_assumption_is_not_reported_as_copied_guarantee(self):
        behavior = candidate()
        behavior['assumptions'] = [{'id': 'A_start', 'text': 'Initial voltage limit', 'scope': 'initial', 'predicate': op('<=', v(), n(28))},
                                   {'id': 'A_weak', 'text': 'Weak environmental limit', 'scope': 'always', 'predicate': op('<=', v(), n(30))}]
        result = inspect_candidate(behavior, SOURCES)
        self.assertFalse(any(d['code'] == 'GUARANTEE_IN_ASSUMPTION' for d in result['diagnostics']))

    def test_provenance_requires_source_ids_and_valid_expression_hash(self):
        behavior = candidate()
        records = inspect_candidate(behavior, SOURCES)['provenance']
        records['/properties/0/predicate'].update(origin='source', requirement_ids=['R1'], rationale='Reviewed against source paragraph 2.')
        result = inspect_candidate(behavior, SOURCES, records)
        self.assertEqual(result['provenance']['/properties/0/predicate']['origin'], 'source')
        for ids in ([], ['missing'], ['R1', 'R1']):
            bad = copy.deepcopy(records)
            bad['/properties/0/predicate']['requirement_ids'] = ids
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                inspect_candidate(behavior, SOURCES, bad)
        records['/properties/0/predicate']['expression_sha256'] = '0' * 64
        stale = inspect_candidate(behavior, SOURCES, records, origin='llm_proposed')
        self.assertEqual(stale['provenance']['/properties/0/predicate']['origin'], 'unspecified')
        self.assertTrue(any(d['code'] == 'STALE_PROVENANCE' for d in stale['diagnostics']))
        with self.assertRaises(ValueError):
            inspect_candidate(behavior, SOURCES, {'/arbitrary': {}})

    def test_declaration_bound_and_parameter_rows_are_readonly_to_expression_api(self):
        behavior = candidate()
        behavior['variables'][0]['bounds'] = {'upper': '28'}
        behavior['variables'].append({'name': 'gain', 'type': 'Real', 'role': 'parameter', 'value': '2'})
        result = inspect_candidate(behavior, SOURCES)
        pointers = {r['pointer']: r for r in result['rows']}
        self.assertEqual(pointers['/variables/0/bounds/upper']['expression'], '28[V]')
        self.assertEqual(pointers['/variables/1/value']['expression'], '2')
        self.assertTrue(all(not row['editable'] for pointer, row in pointers.items() if pointer.startswith('/variables/')))


class CandidateEditingTests(unittest.TestCase):
    def test_edit_equation_is_atomic_and_invalidates_provenance(self):
        behavior = candidate()
        snapshot = copy.deepcopy(behavior)
        records = inspect_candidate(behavior, SOURCES, origin='llm_proposed')['provenance']
        result = edit_candidate(behavior, SOURCES, [{'pointer': '/transitions/0', 'expression': 'next(voltage) = ite(voltage < 28[V], voltage + 1[V], voltage)'}], records, origin='llm_proposed')
        self.assertEqual(behavior, snapshot)
        self.assertEqual(result['behavior']['transitions'][0]['args'][1]['op'], 'ite')
        self.assertEqual(result['inspection']['provenance']['/transitions/0']['origin'], 'unspecified')
        self.assertEqual(result['inspection']['provenance']['/initial/0']['origin'], 'llm')
        # No previous explicit metadata still must not attribute a new engineer edit to the LLM.
        without_metadata = edit_candidate(behavior, SOURCES, [{'pointer': '/transitions/0', 'expression': 'next(voltage) = voltage'}], origin='llm_proposed')
        self.assertEqual(without_metadata['inspection']['provenance']['/transitions/0']['origin'], 'unspecified')

    def test_new_matching_provenance_can_accompany_reviewed_edit(self):
        behavior = candidate()
        edits = [{'pointer': '/transitions/0', 'expression': 'next(voltage) = voltage'}]
        first = edit_candidate(behavior, SOURCES, edits)
        provenance = first['inspection']['provenance']
        provenance['/transitions/0'].update(origin='design', rationale='Reviewed voltage regulation design.')
        second = edit_candidate(behavior, SOURCES, edits, provenance)
        self.assertEqual(second['inspection']['provenance']['/transitions/0']['origin'], 'design')

    def test_edits_cannot_write_arbitrary_fields_or_change_units_scope_and_types(self):
        behavior = candidate()
        snapshot = copy.deepcopy(behavior)
        invalid = [('/variables/0/name', 'anything'), ('/properties/0/requirement_ids', 'true'), ('/initial/2', 'true'),
                   ('/initial/0', 'next(voltage) = 27[V]'), ('/transitions/0', 'voltage = 1[s]'),
                   ('/properties/0/predicate', 'voltage + 1[V]'), ('/transitions/0', 'next(voltage) = voltage * voltage')]
        for pointer, expression in invalid:
            with self.subTest(pointer=pointer, expression=expression), self.assertRaises(ValueError):
                edit_candidate(behavior, SOURCES, [{'pointer': pointer, 'expression': expression}])
            self.assertEqual(behavior, snapshot)
        with self.assertRaises(ValueError):
            edit_candidate(behavior, SOURCES, [{'pointer': '/initial/0', 'expression': 'true'}] * 2)

    def test_parser_variable_name_boundary_in_bare_and_explicit_references(self):
        name = 'v' * 64
        for template, at in (('{}', 'current'), ('var({})', 'current'), ('next({})', 'next')):
            with self.subTest(template=template):
                self.assertEqual(parse_expression(template.format(name)), {'var': name, 'at': at})
                with self.assertRaises(ValueError):
                    parse_expression(template.format(name + 'x'))
        model = candidate()
        # Round-trip a full candidate using the longest valid name.
        model = json.loads(json.dumps(model).replace('voltage', name))
        model['properties'][0]['id'] = 'P_voltage'
        normalized = validate_behavior(model, ['R1', 'R2'])
        inspection = inspect_candidate(normalized, SOURCES)
        edits = [{'pointer': row['pointer'], 'expression': row['expression']}
                 for row in inspection['rows'] if row['editable']]
        self.assertEqual(edit_candidate(normalized, SOURCES, edits)['behavior'], normalized)

    def test_parser_rejects_raw_code_smt_and_resource_exhaustion(self):
        bad = ['__import__("os").system("echo bad")', '(assert false)', '(<= voltage 28)', 'voltage / 2', 'voltage ** 2',
               'voltage; false', 'voltage[0]', '1e3', 'NaN()', 'next(voltage,voltage)', 'ite(true, 1, 2, 3)',
               '(' * 100 + 'true' + ')' * 100, 'x' * 16001]
        for text in bad:
            with self.subTest(text=text[:60]), self.assertRaises(ValueError):
                parse_expression(text)

    def test_canonical_supported_ast_roundtrips_without_hash_changes(self):
        behavior = candidate()
        behavior['variables'].extend([{'name': 'flag', 'type': 'Bool', 'role': 'input'}, {'name': 'true', 'type': 'Bool', 'role': 'input'}])
        expressions = [op('=', v(at='next'), op('*', n('-1', '1'), v())),
                       op('=', v(at='next'), op('-', n(1))),
                       op('implies', v('flag'), op('not', v('true'))),
                       op('and', v('flag'), v('true'), op('<=', v(), n(28))),
                       op('and', op('and', v('flag'), v('true')), op('<=', v(), n(28))),
                       op('=', v(at='next'), op('ite', v('flag'), op('-', v(), n(1)), op('+', v(), n(1))))]
        for ast in expressions:
            behavior['transitions'] = [ast]
            canonical = validate_behavior(behavior, ['R1', 'R2'])
            text = render_expression(canonical['transitions'][0])
            with self.subTest(text=text):
                result = edit_candidate(canonical, SOURCES, [{'pointer': '/transitions/0', 'expression': text}])
                self.assertEqual(result['behavior'], canonical)

    def test_all_shipped_behavior_equations_roundtrip(self):
        folder = Path(__file__).resolve().parents[1] / 'examples' / 'synthetic'
        for path in folder.glob('behavior_*.json'):
            data = json.loads(path.read_text())
            if 'behavior' not in data:
                continue
            sources = data.get('requirements') or sorted({rid for p in data['behavior']['properties'] for rid in p['requirement_ids']})
            normalized = validate_behavior(data['behavior'], [source['id'] if isinstance(source, dict) else source for source in sources])
            inspection = inspect_candidate(normalized, sources)
            edits = [{'pointer': row['pointer'], 'expression': row['expression']} for row in inspection['rows'] if row['editable']]
            with self.subTest(path=path.name):
                result = edit_candidate(normalized, sources, edits)
                self.assertEqual(result['behavior'], normalized)


if __name__ == '__main__':
    unittest.main()
