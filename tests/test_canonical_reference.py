"""Concrete semantic oracles, metamorphic controls, and real-Z3 differentials."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import shutil
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from canonical_reference import evaluate_normalized_formula as reference
from mutation_core import validate_context, validate_formula, emit_formula, _base, _execute


def var(name): return {'var': name}
def num(value, unit='1'): return {'value': str(value), 'unit': unit}
def op(name, *args): return {'op': name, 'args': list(args)}


class ReferenceTests(unittest.TestCase):
    def test_guard_and_exact_boundary_have_independent_expected_answers(self):
        formula = op('implies', var('active'), op('<', var('delay'), num('1', 's')))
        for active, delay, expected in [(False, 2, True), (True, '0.999', True),
                                        (True, 1, False), (True, 2, False)]:
            with self.subTest(active=active, delay=delay):
                self.assertIs(reference(formula, {'active': active, 'delay': delay},
                                        {'active': 'Bool', 'delay': 'Real'}), expected)

    def test_arithmetic_boolean_and_conditional_nodes(self):
        types, values = {'x': 'Int', 'y': 'Real', 'b': 'Bool'}, {'x': 2, 'y': '1.5', 'b': True}
        cases = [op('=', op('+', var('x'), var('y')), num('3.5')),
                 op('=', op('-', var('x')), num(-2)),
                 op('=', op('-', var('x'), var('y')), num('.5')),
                 op('=', op('*', num(3), var('y')), num('4.5')),
                 op('=', op('ite', var('b'), var('y'), num(0)), num('1.5')),
                 op('and', var('b'), op('not', False)), op('or', False, var('b')),
                 op('!=', var('x'), var('y')), op('>=', var('x'), num(2)),
                 op('>', var('x'), var('y')), op('<=', var('y'), var('x'))]
        for formula in cases:
            with self.subTest(formula=formula): self.assertTrue(reference(formula, values, types))

    def test_incomplete_invalid_or_temporal_inputs_are_errors(self):
        cases = [(var('x'), {}, {'x': 'Bool'}),
                 (var('x'), {'x': 1}, {'x': 'Bool'}),
                 (op('=', var('x'), num(1)), {'x': True}, {'x': 'Int'}),
                 (op('=', var('x'), num(1)), {'x': '.5'}, {'x': 'Int'}),
                 (op('=', var('x'), num(1)), {'x': 1.0}, {'x': 'Real'}),
                 ({'var': 'x', 'at': 'next'}, {'x': True}, {'x': 'Bool'}),
                 (op('=', True, num(1)), {}, {}),
                 (op('eventually', True), {}, {})]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError): reference(*args)


@unittest.skipUnless(shutil.which('z3'), 'Requires real Z3 executable')
class DifferentialTests(unittest.TestCase):
    def check(self, formula, variables, values, expected):
        context = validate_context(variables, [])
        normalized = validate_formula(formula, context)
        actual = reference(normalized, values, {v['name']: v['type'] for v in context['variables']})
        self.assertIs(actual, expected)
        base, _ = _base(context)
        bindings = []
        for name, value in values.items():
            if type(value) is bool: literal = 'true' if value else 'false'
            else:
                value = Fraction(value)
                sign = '-' if value < 0 else ''
                magnitude = abs(value)
                literal = str(magnitude.numerator) if magnitude.denominator == 1 else f'(/ {magnitude.numerator} {magnitude.denominator})'
                if sign: literal = f'(- {literal})'
            bindings.append(f'(assert (= v_{name}_0 {literal}))')
        query = base + '\n'.join(bindings) + '\n(assert ' + emit_formula(normalized, context) + ')\n(check-sat)\n'
        result = _execute(query, 'z3', 5, {})
        self.assertEqual(result['status'], 'sat' if expected else 'unsat', result)

    def test_unit_equivalence_strictness_and_missing_guard_controls(self):
        variables = [{'name': 'active', 'type': 'Bool'}, {'name': 'delay', 'type': 'Real', 'unit': 's'}]
        original = op('implies', var('active'), op('<', var('delay'), num(1, 's')))
        equivalent = op('implies', var('active'), op('<', var('delay'), num(1000, 'ms')))
        changed_boundary = op('implies', var('active'), op('<=', var('delay'), num(1, 's')))
        removed_guard = op('<', var('delay'), num(1, 's'))
        for active, delay in [(False, '2'), (True, '.999'), (True, '1'), (True, '1.001')]:
            values = {'active': active, 'delay': delay}
            expected = not active or Fraction(delay) < 1
            for formula in (original, equivalent): self.check(formula, variables, values, expected)
        self.check(changed_boundary, variables, {'active': True, 'delay': '1'}, True)
        self.check(removed_guard, variables, {'active': False, 'delay': '2'}, False)

    def test_alpha_renaming_regrouping_and_wrong_recipient_control(self):
        variables = [{'name': 'received', 'type': 'Bool'}] + [
            {'name': name, 'type': 'Int', 'unit': '1'} for name in ('actual', 'intended', 'sender')]
        formula = op('implies', var('received'), op('=', var('actual'), var('intended')))
        values = {'received': True, 'actual': 2, 'intended': 2, 'sender': 3}
        self.check(formula, variables, values, True)
        self.check(op('implies', var('received'), op('=', var('actual'), var('sender'))), variables, values, False)
        renamed = deepcopy(formula)
        renamed['args'][1]['args'][1]['var'] = 'destination'
        renamed_variables = [dict(v, name='destination') if v['name'] == 'intended' else v for v in variables]
        renamed_values = {'destination' if k == 'intended' else k: v for k, v in values.items()}
        self.check(renamed, renamed_variables, renamed_values, True)
        for grouped in (op('and', formula, True), op('and', True, formula)):
            self.check(grouped, variables, values, True)


if __name__ == '__main__': unittest.main()
