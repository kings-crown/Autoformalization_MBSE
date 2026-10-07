"""Scope and soundness regressions for explicit finite behavior proposals."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import review_behavior as behavior

ROOT = Path(__file__).resolve().parents[1]


def v(name, next=False):
    return {'var': name, **({'at': 'next'} if next else {})}


def c(value, unit='1'):
    return {'value': str(value), 'unit': unit}


def op(name, *args):
    return {'op': name, 'args': list(args)}


def sample(name):
    return json.loads((ROOT / 'examples' / 'synthetic' / f'behavior_{name}.json').read_text())['behavior']


def minimal(predicate=True):
    return {'schema': 'review_behavior/1', 'horizon': 2, 'step': {'value': '1', 'unit': 's'},
            'variables': [{'name': 'x', 'type': 'Real', 'role': 'state', 'unit': 'V'}],
            'initial': [], 'transitions': [], 'assumptions': [],
            'properties': [{'id': 'P1', 'kind': 'always', 'requirement_ids': ['R1'], 'predicate': predicate}]}


def schedule(trigger_step=0, response_steps=(1,), window=(1, 2), horizon=3):
    model = {'schema': 'review_behavior/1', 'horizon': horizon, 'step': {'value': '1', 'unit': 's'},
             'variables': [{'name': 'clock', 'type': 'Int', 'role': 'state', 'unit': 's'}],
             'initial': [op('=', v('clock'), c(0, 's'))],
             'transitions': [op('=', v('clock', True), op('+', v('clock'), c(1, 's')))],
             'properties': [{'id': 'P1', 'kind': 'bounded_response', 'requirement_ids': ['R1'],
                             'trigger': op('=', v('clock'), c(trigger_step, 's')),
                             'response': False if not response_steps else op('=', v('clock'), c(response_steps[0], 's')) if len(response_steps) == 1 else op('or', *[op('=', v('clock'), c(n, 's')) for n in response_steps]),
                             'window': {'min': window[0], 'max': window[1]},
                             'response_semantics': 'first_after_trigger'}]}
    return model


class ValidationTests(unittest.TestCase):
    def validate(self, model):
        return behavior.validate_behavior(model, ['R1', 'R2', 'R3'])

    def test_variable_names_allow_64_while_other_identifiers_retain_48(self):
        name = "x" * 64
        model = minimal(op('>=', v(name), c(0, 'V')))
        model['variables'][0]['name'] = name
        self.assertEqual(self.validate(model)['variables'][0]['name'], name)
        self.assertIn('at most 64 characters', behavior.BEHAVIOR_GUIDE)
        model['variables'][0]['name'] = name + 'x'
        with self.assertRaisesRegex(ValueError, 'Variable name.*64'):
            self.validate(model)
        model['variables'][0]['name'] = name
        model['assumptions'] = [{'id': 'a' * 48, 'text': 'Explicit premise', 'scope': 'initial', 'predicate': True}]
        model['properties'][0]['id'] = 'p' * 48
        self.validate(model)
        for field in ('assumptions', 'properties'):
            changed = copy.deepcopy(model)
            changed[field][0]['id'] += 'x'
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, '48'):
                self.validate(changed)

    def test_supported_examples_normalize_idempotently(self):
        for name in ('response', 'charging', 'disturbance'):
            with self.subTest(name=name):
                model = self.validate(sample(name))
                self.assertEqual(model, self.validate(model))

    def test_exact_unit_conversion_and_si_case(self):
        model = minimal(op('>=', v('x'), c('1001', 'mV')))
        model['step'] = {'value': '0.1', 'unit': 'ms'}
        result = self.validate(model)
        self.assertEqual(result['properties'][0]['predicate']['args'][1], c('1.001', 'V'))
        self.assertEqual(result['step'], {'value': '0.0001', 'unit': 's'})
        model['properties'][0]['predicate']['args'][1]['unit'] = 'MV'
        with self.assertRaises(ValueError):
            self.validate(model)

    def test_units_types_unknown_refs_and_next_refs_are_rejected(self):
        bad = [op('>=', v('x'), c(1, 's')), op('and', v('x'), True),
               op('=', v('missing'), c(1, 'V')), op('>=', v('x', True), c(0, 'V')),
               op('*', v('x'), v('x')), '(assert false)', {'value': 1.2}]
        for predicate in bad:
            with self.subTest(predicate=predicate), self.assertRaises(ValueError):
                self.validate(minimal(predicate))

    def test_malformed_enum_objects_raise_value_error(self):
        for field in ('type', 'role'):
            model = minimal()
            model['variables'][0][field] = {}
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(model)
        model = minimal(op('=', {'var': 'x', 'at': []}, c(1, 'V')))
        with self.assertRaises(ValueError):
            self.validate(model)
        model = minimal()
        model['properties'][0]['kind'] = {}
        with self.assertRaises(ValueError):
            self.validate(model)
        model = minimal()
        model['assumptions'] = [{'id': 'A', 'text': 'Assumption', 'scope': {}, 'predicate': True}]
        with self.assertRaises(ValueError):
            self.validate(model)

    def test_fixed_parameters_and_integer_grid_are_required(self):
        model = minimal()
        model['variables'][0]['role'] = 'parameter'
        with self.assertRaises(ValueError):
            self.validate(model)
        model['variables'][0].update(type='Int', unit='mV', value='1000')
        with self.assertRaises(ValueError):
            self.validate(model)

    def test_precision_and_resource_limits_fail_at_first_validation(self):
        model = minimal()
        model['step'] = {'value': '0.1234567890123456789012345678', 'unit': 'ms'}
        with self.assertRaises(ValueError):
            self.validate(model)
        for value in (0, 21, True, 2.5):
            model = minimal()
            model['horizon'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.validate(model)

    def test_unbounded_response_cannot_carry_window(self):
        model = schedule()
        model['properties'][0]['kind'] = 'eventual_response'
        with self.assertRaises(ValueError):
            self.validate(model)

    def test_source_links_and_unknown_fields_are_required(self):
        model = minimal()
        model['properties'][0]['requirement_ids'] = ['R_missing']
        with self.assertRaises(ValueError):
            self.validate(model)
        model = minimal()
        model['raw_smt'] = '(assert false)'
        with self.assertRaises(ValueError):
            self.validate(model)


@unittest.skipUnless(shutil.which('z3'), 'Real Z3 executable unavailable')
class SolverTests(unittest.TestCase):
    def analyze(self, model):
        normalized = behavior.validate_behavior(model, ['R1', 'R2', 'R3'])
        with tempfile.TemporaryDirectory(prefix='behavior_test_') as folder:
            result = behavior.analyze_behavior(normalized, Path(folder))
            for path in Path(folder).glob('behavior_*.json'):
                record = json.loads(path.read_text())
                if 'query_sha256' in record:
                    query = Path(folder) / record['artifacts']['query']
                    self.assertEqual(hashlib.sha256(query.read_bytes()).hexdigest(), record['query_sha256'])
            return result

    def test_feasible_model_and_counterexample_are_distinct(self):
        result = self.analyze(minimal(op('>=', v('x'), c(0, 'V'))))
        self.assertEqual(result['model_feasibility']['verdict'], 'sat')
        self.assertEqual(result['checks'][0]['verdict'], 'counterexample')
        self.assertTrue(any(row['values']['x'].startswith('-') for row in result['checks'][0]['trace']))

    def test_inconsistent_assumptions_never_pass(self):
        model = minimal(True)
        model['assumptions'] = [{'id': 'A_impossible', 'scope': 'always', 'text': 'Deliberately inconsistent scenario.', 'predicate': False}]
        result = self.analyze(model)
        self.assertEqual(result['status'], 'infeasible')
        self.assertEqual(result['checks'][0]['verdict'], 'unknown')
        self.assertTrue(result['model_feasibility']['core'])

    def test_bounded_response_has_reachable_trigger_and_eventual_is_unproved(self):
        result = self.analyze(sample('response'))
        response, eventual = result['checks']
        self.assertEqual(response['verdict'], 'bounded_pass')
        self.assertEqual(response['trigger_reachability']['verdict'], 'sat')
        self.assertEqual(response['overlap_check']['verdict'], 'unsat')
        self.assertEqual(eventual['verdict'], 'unproved')
        self.assertEqual(eventual['finite_completion_witness']['verdict'], 'sat')
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['approval_blocked'])

    def test_positive_shrinking_progress_does_not_prove_eventual_full(self):
        result = self.analyze(sample('charging'))
        self.assertEqual([x['verdict'] for x in result['checks']], ['bounded_pass', 'unproved'])
        self.assertEqual(result['checks'][1]['finite_completion_witness']['verdict'], 'unsat')
        self.assertEqual(result['model_feasibility']['trace'][-1]['values']['level'], '1575/16')

    def test_disturbance_is_adversarial_and_nominal_fixed(self):
        model = sample('disturbance')
        result = self.analyze(model)
        check = result['checks'][0]
        self.assertEqual(check['verdict'], 'counterexample')
        self.assertTrue(all(row['values']['nominal'] == '9' for row in check['trace']))
        model['variables'][1]['bounds'] = {'lower': '-1', 'upper': '1'}
        result = self.analyze(model)
        self.assertEqual(result['checks'][0]['verdict'], 'bounded_pass')
        self.assertIn('no worst-case optimization', result['checks'][0]['margin_scope'])

    def test_parameter_bounds_cannot_choose_favorable_value(self):
        model = minimal(True)
        model['variables'][0].update(role='parameter', value='9', bounds={'lower': '10'})
        result = self.analyze(model)
        self.assertEqual(result['model_feasibility']['verdict'], 'unsat')
        self.assertEqual(result['checks'][0]['verdict'], 'unknown')

    def test_trigger_unreachable_is_vacuous_not_pass(self):
        result = self.analyze(schedule(trigger_step=10))
        self.assertEqual(result['checks'][0]['verdict'], 'vacuous')

    def test_late_trigger_is_pending_even_without_known_violation(self):
        result = self.analyze(schedule(trigger_step=3, response_steps=(), horizon=3))
        self.assertEqual(result['checks'][0]['verdict'], 'pending')
        self.assertEqual(result['checks'][0]['scope']['complete_trigger_steps'], [0, 1])

    def test_window_longer_than_horizon_remains_pending(self):
        result = self.analyze(schedule(window=(1, 10), horizon=3))
        self.assertEqual(result['checks'][0]['verdict'], 'pending')
        self.assertEqual(result['checks'][0]['scope']['complete_trigger_steps'], [])

    def test_same_first_response_cannot_use_later_event_to_meet_lower_bound(self):
        result = self.analyze(schedule(response_steps=(1, 3), window=(2, 3), horizon=4))
        self.assertEqual(result['checks'][0]['verdict'], 'counterexample')

    def test_overlapping_commands_are_not_discharged_by_one_response(self):
        model = schedule(response_steps=(2,), window=(1, 2), horizon=3)
        model['properties'][0]['trigger'] = op('<=', v('clock'), c(1, 's'))
        self.assertEqual(self.analyze(model)['checks'][0]['verdict'], 'unsupported')

    def test_subsecond_trace_times_remain_exact(self):
        model = minimal(True)
        model['step'] = {'value': '0.0000001', 'unit': 's'}
        result = self.analyze(model)
        self.assertEqual(result['scope']['time_interval_seconds'][1], '0.0000002')
        self.assertEqual(result['model_feasibility']['trace'][1]['time_seconds'], '0.0000001')

    def test_partial_transition_relation_is_explicitly_scoped(self):
        model = minimal(op('not', v('bad')))
        model['variables'] = [{'name': 'bad', 'type': 'Bool', 'role': 'state'}]
        model['transitions'] = [op('not', v('bad')), op('=', v('bad', True), False)]
        result = self.analyze(model)
        self.assertEqual(result['checks'][0]['verdict'], 'bounded_pass')
        self.assertIn('shorter nonextendable executions are not checked', result['scope']['meaning'])
        self.assertTrue(any('deadlocks' in line for line in result['limitations']))

    def test_portfolio_disagreement_during_unsat_evidence_is_unknown(self):
        actual = behavior.solver.run_z3_fragment
        def disagree(fragment):
            result = actual(fragment)
            if '(get-unsat-core)' in fragment:
                result = copy.deepcopy(result)
                result['cross_check'] = {'agree': False}
            return result
        with patch.object(behavior.solver, 'run_z3_fragment', side_effect=disagree):
            result = self.analyze(minimal(True))
        self.assertEqual(result['checks'][0]['verdict'], 'unknown')


if __name__ == '__main__':
    unittest.main()
