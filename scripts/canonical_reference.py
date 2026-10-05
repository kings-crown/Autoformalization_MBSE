"""Small reference evaluator for the normalized static TLR expression language.

This evaluator deliberately does not call the SMT or SysML emitters. It accepts
canonical-unit, structurally validated expressions and concrete valuations, and
uses exact rational arithmetic. It is a differential-testing oracle, not a
source-fidelity judge, temporal verifier, or independent SysML reader.
"""
from fractions import Fraction


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, str, Fraction)):
        raise ValueError("Reference numbers must be exact integers, strings or Fractions")
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("Invalid exact reference number") from exc


def evaluate_normalized_formula(formula, valuation, variable_types):
    """Evaluate a canonical current-state Boolean formula on one valuation.

    Values are expressed in the normalized variables' canonical units. Validation
    and unit normalization are prerequisites; this API never infers missing
    values or temporal history. Failures remain errors, not false verdicts.
    """
    if not isinstance(valuation, dict) or not isinstance(variable_types, dict):
        raise ValueError("Concrete valuation and declared variable types are required")

    def boolean(value):
        if type(value) is not bool:
            raise ValueError("Expected Boolean operand")
        return value

    def numeric(value):
        if not isinstance(value, Fraction):
            raise ValueError("Expected numeric operand")
        return value

    def evaluate(node, depth=0):
        if depth > 16:
            raise ValueError("Reference expression exceeds the supported depth")
        if type(node) is bool:
            return node
        if not isinstance(node, dict):
            raise ValueError("Expected normalized expression object")
        if 'var' in node:
            if set(node) - {'var', 'at'} or node.get('at', 'current') != 'current':
                raise ValueError("Only current-state variable references are supported")
            name = node['var']
            if name not in variable_types or name not in valuation:
                raise ValueError(f"Missing declared type or concrete value for {name}")
            kind, value = variable_types[name], valuation[name]
            if kind == 'Bool':
                return boolean(value)
            if kind not in {'Int', 'Real'}:
                raise ValueError("Unsupported variable type")
            value = _number(value)
            if kind == 'Int' and value.denominator != 1:
                raise ValueError("Integer variable has non-integral value")
            return value
        if 'value' in node:
            if set(node) - {'value', 'unit'}:
                raise ValueError("Invalid normalized numeric literal")
            return _number(node['value'])
        if set(node) != {'op', 'args'} or not isinstance(node['args'], list):
            raise ValueError("Invalid operator expression")
        op = node['op']
        args = [evaluate(arg, depth + 1) for arg in node['args']]
        arity = {'not': (1,), 'implies': (2,), '=': (2,), '!=': (2,),
                 '<': (2,), '<=': (2,), '>': (2,), '>=': (2,),
                 '+': (2,), '-': (1, 2), '*': (2,), 'ite': (3,),
                 'and': tuple(range(2, 17)), 'or': tuple(range(2, 17))}
        if op not in arity or len(args) not in arity[op]:
            raise ValueError("Unsupported operator or arity")
        if op in {'and', 'or'}:
            values = [boolean(a) for a in args]
            return all(values) if op == 'and' else any(values)
        if op == 'not':
            return not boolean(args[0])
        if op == 'implies':
            left, right = map(boolean, args)
            return (not left) or right
        if op == 'ite':
            condition = boolean(args[0])
            if type(args[1]) is not type(args[2]):
                raise ValueError("Conditional branches have incompatible types")
            return args[1] if condition else args[2]
        if op in {'=', '!='}:
            if type(args[0]) is not type(args[1]):
                raise ValueError("Equality operands have incompatible types")
            same = args[0] == args[1]
            return same if op == '=' else not same
        values = [numeric(a) for a in args]
        if op == '+': return values[0] + values[1]
        if op == '-': return -values[0] if len(values) == 1 else values[0] - values[1]
        if op == '*': return values[0] * values[1]
        if op == '<': return values[0] < values[1]
        if op == '<=': return values[0] <= values[1]
        if op == '>': return values[0] > values[1]
        if op == '>=': return values[0] >= values[1]
        raise ValueError("Unsupported operator")

    return boolean(evaluate(formula))
