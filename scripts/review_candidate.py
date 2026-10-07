"""Review-only candidate equations, explicit provenance, and syntactic diagnostics.

This module neither invents dynamics nor runs a solver. Its parser accepts only
review_behavior's constrained expression language; validate_behavior remains the
final type/unit/scope authority. Diagnostics are review prompts, never proofs of
model completeness or grounds to change a requirement automatically.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any

from review_behavior import VARIABLE_IDENT, validate_behavior

SCHEMA = 'review_candidate/1'
ORIGINS = {'unspecified', 'source', 'design', 'environment', 'llm'}
_KEYWORDS = {'true', 'false', 'and', 'or', 'not', 'implies', 'next', 'ite', 'var'}
_TOKEN = re.compile(r'\s+|\d+(?:\.\d+)?|[A-Za-z][A-Za-z0-9_]*|\[[^\[\]\r\n]{1,40}\]|<=|>=|!=|[=<>+*(),-]')
_PRECEDENCE = {'implies': 1, 'or': 2, 'and': 3, '=': 4, '!=': 4, '<': 4, '<=': 4, '>': 4, '>=': 4, '+': 5, '-': 5, '*': 6}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def _source_ids(requirements: list) -> list[str]:
    if not isinstance(requirements, list):
        raise ValueError('Candidate sources must be a list.')
    ids = [source if isinstance(source, str) else source.get('id') if isinstance(source, dict) else None for source in requirements]
    if any(not isinstance(rid, str) or not rid for rid in ids) or len(set(ids)) != len(ids):
        raise ValueError('Candidate sources require unique, nonempty requirement IDs.')
    return ids


def render_expression(ast: Any) -> str:
    """Render canonical AST as a round-trippable, non-executable expression."""
    if isinstance(ast, bool):
        return 'true' if ast else 'false'
    if 'var' in ast:
        name = ast['var']
        if ast.get('at') == 'next':
            return f'next({name})'
        return f'var({name})' if name in _KEYWORDS else name
    if 'value' in ast:
        unit = ast.get('unit', '1')
        return str(ast['value']) + (f'[{unit}]' if unit != '1' else '')
    args = [render_expression(arg) for arg in ast['args']]
    op = ast['op']
    if op == 'ite':
        return 'ite(' + ', '.join(args) + ')'
    if op == 'not':
        return '(not ' + args[0] + ')'
    if op == '-' and len(args) == 1:
        return '(- (' + args[0] + '))'
    return '(' + f' {op} '.join(args) + ')'


def parse_expression(text: str) -> Any:
    """Parse a bounded infix expression into AST without eval or raw SMT input.

    Parsing alone makes no type/unit/scope claim. Call validate_behavior after
    installing the result, as edit_candidate does.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        raise ValueError('An expression must contain 1 to 16000 characters.')
    tokens, offset = [], 0
    while offset < len(text):
        match = _TOKEN.match(text, offset)
        if not match:
            raise ValueError(f'Unsupported expression syntax at character {offset + 1}.')
        token = match.group()
        if not token.isspace():
            tokens.append(token)
        offset = match.end()
        if len(tokens) > 4800:
            raise ValueError('Expression has too many tokens.')
    position = 0
    nodes = 0

    def peek():
        return tokens[position] if position < len(tokens) else None

    def take(expected=None):
        nonlocal position
        token = peek()
        if token is None or (expected is not None and token != expected):
            raise ValueError(f'Expected {expected or "an expression"}; found {token!r}.')
        position += 1
        return token

    def expr(minimum=0, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 32 or nodes > 1600:
            raise ValueError('Expression exceeds the size/depth limit.')
        token = take()
        if token == '(':
            left = expr(0, depth + 1)
            take(')')
        elif token in {'true', 'false'}:
            left = token == 'true'
        elif token in {'not', '-'}:
            # Negative numeric literals stay literals, preserving linear factors.
            if token == '-' and peek() is not None and re.fullmatch(r'\d+(?:\.\d+)?', peek()):
                value = '-' + take()
                unit = take()[1:-1] if peek() is not None and peek().startswith('[') else '1'
                left = {'value': value, 'unit': unit}
            else:
                left = {'op': token, 'args': [expr(4 if token == 'not' else 7, depth + 1)]}
        elif re.fullmatch(r'\d+(?:\.\d+)?', token):
            unit = take()[1:-1] if peek() is not None and peek().startswith('[') else '1'
            left = {'value': token, 'unit': unit}
        elif token in {'next', 'var'} and peek() == '(':
            take('(')
            name = take()
            if not VARIABLE_IDENT.fullmatch(name):
                raise ValueError('Variable references require a declared identifier.')
            take(')')
            left = {'var': name, 'at': 'next' if token == 'next' else 'current'}
        elif token == 'ite' and peek() == '(':
            take('(')
            args = [expr(0, depth + 1)]
            for _ in range(2):
                take(',')
                args.append(expr(0, depth + 1))
            take(')')
            left = {'op': 'ite', 'args': args}
        elif VARIABLE_IDENT.fullmatch(token) and token not in _KEYWORDS:
            left = {'var': token, 'at': 'current'}
        else:
            raise ValueError(f'Expected a literal or variable; found {token!r}.')
        joined_op = None
        while peek() in _PRECEDENCE and _PRECEDENCE[peek()] >= minimum:
            op = take()
            prec = _PRECEDENCE[op]
            right = expr(prec if op == 'implies' else prec + 1, depth + 1)
            # Preserve the native n-ary form for canonical rendered conjunctions.
            if op in {'and', 'or'} and joined_op == op:
                left = {'op': op, 'args': [*left['args'], right]}
            else:
                left = {'op': op, 'args': [left, right]}
            joined_op = op
        return left

    result = expr()
    if position != len(tokens):
        raise ValueError(f'Unexpected trailing token: {peek()!r}.')
    return result


def _refs(ast: Any, at: str | None = None) -> set[str]:
    if not isinstance(ast, dict):
        return set()
    if 'var' in ast:
        return {ast['var']} if at is None or ast.get('at', 'current') == at else set()
    result = set()
    for arg in ast.get('args', []):
        result.update(_refs(arg, at))
    return result


def _normal(ast: Any) -> Any:
    if not isinstance(ast, dict) or 'op' not in ast:
        return ast
    op, args = ast['op'], [_normal(arg) for arg in ast['args']]
    if op in {'>', '>='}:
        op, args = {'>': '<', '>=': '<='}[op], list(reversed(args))
    if op == 'not' and isinstance(args[0], dict) and args[0].get('op') in {'<', '<=', '=', '!='}:
        child = args[0]
        reverse = {'<': '>=', '<=': '>', '=': '!=', '!=': '='}[child['op']]
        return _normal({'op': reverse, 'args': child['args']})
    if op in {'and', 'or'}:
        args = [nested for arg in args for nested in (arg['args'] if isinstance(arg, dict) and arg.get('op') == op else [arg])]
    if op in {'and', 'or', '=', '!='}:
        args = sorted(args, key=lambda arg: json.dumps(arg, sort_keys=True))
    return {'op': op, 'args': args}


def _conjuncts(ast: Any) -> list:
    if isinstance(ast, dict) and ast.get('op') == 'and':
        return [part for arg in ast['args'] for part in _conjuncts(arg)]
    return [ast]


def _entries(behavior: dict) -> list[dict]:
    entries = []
    for i, variable in enumerate(behavior['variables']):
        name, unit = variable['name'], variable.get('unit', '1')
        pointer = f'/variables/{i}'
        details = f"{name}: {variable['type']}" + (f' [{unit}]' if unit != '1' else '') + f" ({variable['role']})"
        entries.append({'pointer': pointer, 'category': 'variable', 'label': name, 'expression': details,
                        'ast': variable, 'editable': False, 'variable': name, 'value_type': variable['type'], 'unit': unit, 'role': variable['role']})
        for side, value in variable.get('bounds', {}).items():
            literal = {'value': value, 'unit': unit}
            entries.append({'pointer': pointer + f'/bounds/{side}', 'category': 'bound', 'label': f'{name} {side} bound',
                            'ast': literal, 'editable': False, 'variable': name})
        if 'value' in variable:
            literal = variable['value'] if variable['type'] == 'Bool' else {'value': variable['value'], 'unit': unit}
            entries.append({'pointer': pointer + '/value', 'category': 'parameter', 'label': f'{name} fixed value',
                            'ast': literal, 'editable': False, 'variable': name})
    for category in ('initial', 'transitions'):
        for i, ast in enumerate(behavior[category]):
            entries.append({'pointer': f'/{category}/{i}', 'category': category, 'label': f'{category.capitalize()} {i + 1}', 'ast': ast, 'editable': True})
    for i, assumption in enumerate(behavior['assumptions']):
        entries.append({'pointer': f'/assumptions/{i}/predicate', 'category': 'assumption', 'label': assumption['id'],
                        'ast': assumption['predicate'], 'editable': True, 'scope': assumption['scope'], 'text': assumption['text']})
    for i, prop in enumerate(behavior['properties']):
        for field in ('predicate', 'trigger', 'response', 'margin'):
            if field in prop:
                entries.append({'pointer': f'/properties/{i}/{field}', 'category': 'property', 'label': f"{prop['id']} {field}",
                                'ast': prop[field], 'editable': True, 'property_id': prop['id'], 'requirement_ids': prop['requirement_ids']})
    return entries


def _diagnostics(behavior: dict) -> list[dict]:
    diagnostics = []
    dynamics = behavior['transitions'] + [a['predicate'] for a in behavior['assumptions'] if a['scope'] == 'transition']
    updated = set().union(*(_refs(ast, 'next') for ast in dynamics)) if dynamics else set()
    initial = behavior['initial'] + [a['predicate'] for a in behavior['assumptions'] if a['scope'] in {'initial', 'always'}]
    initialized = set().union(*(_refs(ast) for ast in initial)) if initial else set()
    for i, variable in enumerate(behavior['variables']):
        if variable['role'] not in {'state', 'output'}:
            continue
        name = variable['name']
        if name not in updated:
            diagnostics.append({'code': 'MISSING_STATE_UPDATE', 'severity': 'warning', 'pointers': [f'/variables/{i}'], 'variable': name,
                                'message': f'{name} has no next-state reference in transitions or transition assumptions. Its next value has no explicit dynamic relation; declarations and bounds do not make it persist.'})
        if name not in initialized:
            diagnostics.append({'code': 'MISSING_INITIAL_CONSTRAINT', 'severity': 'info', 'pointers': [f'/variables/{i}'], 'variable': name,
                                'message': f'{name} is not referenced by an initial or always predicate. Its initial value is free within declared domains and other model constraints.'})
    premises = []
    for i, assumption in enumerate(behavior['assumptions']):
        if assumption['scope'] == 'always':
            premises.extend((part, f'/assumptions/{i}/predicate', None) for part in _conjuncts(assumption['predicate']))
    for i, variable in enumerate(behavior['variables']):
        for side, value in variable.get('bounds', {}).items():
            premises.append(({'op': '>=' if side == 'lower' else '<=', 'args': [
                {'var': variable['name'], 'at': 'current'}, {'value': value, 'unit': variable.get('unit', '1')}]},
                f'/variables/{i}/bounds/{side}', variable['name']))
    for i, prop in enumerate(behavior['properties']):
        if prop['kind'] not in {'always', 'robustness'}:
            continue
        target = f'/properties/{i}/predicate'
        seen = set()
        for guarantee in _conjuncts(prop['predicate']):
            key = _hash(_normal(guarantee))
            for premise, pointer, name in premises:
                if pointer in seen or _hash(_normal(premise)) != key:
                    continue
                seen.add(pointer)
                diagnostics.append({'code': 'GUARANTEE_IN_ASSUMPTION', 'severity': 'warning', 'pointers': [target, pointer],
                                    'property_id': prop['id'], **({'variable': name} if name else {}),
                                    'message': f"A guarantee or conjunct of {prop['id']} is repeated in an always assumption or domain bound. A passing check would rely on this premise; review its independent justification. This is a syntactic match, not a general circularity proof."})
    return diagnostics


def inspect_candidate(behavior: dict, requirements: list, provenance: dict | None = None, origin: str = 'engineer_supplied') -> dict:
    """Normalize a candidate and expose review rows without altering its semantics.

    Provenance is a pointer-keyed map of {expression_sha256, origin,
    requirement_ids, rationale}. Stale entries are dropped with a diagnostic;
    source provenance always requires explicit valid source identifiers.
    """
    ids = _source_ids(requirements)
    normalized = validate_behavior(behavior, ids)
    if provenance is None:
        provenance = {}
    if not isinstance(provenance, dict):
        raise ValueError('Candidate provenance must be a pointer-keyed object.')
    entries = _entries(normalized)
    known_pointers = {entry['pointer'] for entry in entries}
    if set(provenance) - known_pointers:
        raise ValueError('Candidate provenance references an unknown expression pointer.')
    diagnostics = _diagnostics(normalized)
    rows, canonical = [], {}
    variable_links = {}
    for prop in normalized['properties']:
        names = set().union(*(_refs(prop[field]) for field in ('predicate', 'trigger', 'response', 'margin') if field in prop))
        for name in names:
            variable_links.setdefault(name, set()).update(prop['requirement_ids'])
    for entry in entries:
        ast = entry.pop('ast')
        pointer, digest = entry['pointer'], _hash(ast)
        details = {'origin': 'llm' if isinstance(origin, str) and origin.startswith('llm') else 'unspecified', 'requirement_ids': [], 'rationale': ''}
        supplied = provenance.get(pointer)
        if supplied is not None:
            if not isinstance(supplied, dict) or set(supplied) != {'expression_sha256', 'origin', 'requirement_ids', 'rationale'}:
                raise ValueError('Each provenance entry requires only expression_sha256, origin, requirement_ids and rationale.')
            links = supplied['requirement_ids']
            if not isinstance(supplied['origin'], str) or supplied['origin'] not in ORIGINS:
                raise ValueError('Unsupported provenance origin.')
            if not isinstance(links, list) or any(not isinstance(rid, str) or rid not in ids for rid in links) or len(set(links)) != len(links):
                raise ValueError('Provenance links must reference unique existing requirement IDs.')
            if supplied['origin'] == 'source' and not links:
                raise ValueError('Source provenance requires at least one existing source requirement ID.')
            if not isinstance(supplied['rationale'], str) or len(supplied['rationale']) > 4000:
                raise ValueError('Provenance rationale must be text of at most 4000 characters.')
            if not isinstance(supplied['expression_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', supplied['expression_sha256']):
                raise ValueError('Provenance requires an expression SHA-256 digest.')
            if supplied['expression_sha256'] != digest:
                details = {'origin': 'unspecified', 'requirement_ids': [], 'rationale': ''}
                diagnostics.append({'code': 'STALE_PROVENANCE', 'severity': 'warning', 'pointers': [pointer],
                                    'message': 'The expression changed; its previous provenance was invalidated and must be reviewed again.'})
            else:
                details = {key: deepcopy(supplied[key]) for key in ('origin', 'requirement_ids', 'rationale')}
        links = entry.get('requirement_ids')
        if links is None:
            names = {entry['variable']} if 'variable' in entry else _refs(ast)
            links = sorted(set().union(*(variable_links.get(name, set()) for name in names))) if names else []
        canonical[pointer] = {'expression_sha256': digest, **details}
        rows.append({**entry, 'expression': entry.get('expression', render_expression(ast) if entry['category'] != 'variable' else ''),
                     'expression_sha256': digest, 'provenance': details, 'requirement_ids': links})
    return {'schema': SCHEMA, 'behavior_sha256': _hash(normalized), 'rows': rows, 'diagnostics': diagnostics, 'provenance': canonical,
            'limitations': ['Diagnostics identify missing references and syntactic duplicates only; they do not prove complete dynamics or detect every circular assumption.',
                            'Linked requirement IDs identify related properties. They do not establish an equation\'s source or stakeholder approval.']}


def edit_candidate(behavior: dict, requirements: list, edits: list, provenance: dict | None = None, origin: str = 'engineer_supplied') -> dict:
    """Edit existing equation rows atomically and revalidate the full candidate."""
    ids = _source_ids(requirements)
    normalized = validate_behavior(behavior, ids)
    previous = inspect_candidate(normalized, requirements, None, origin)['provenance']
    supplied_provenance = {**previous, **(provenance or {})} if isinstance(provenance, dict) or provenance is None else provenance
    allowed = {entry['pointer'] for entry in _entries(normalized) if entry['editable']}
    if not isinstance(edits, list) or len(edits) > len(allowed):
        raise ValueError('Candidate edits must be a list of existing equation fields.')
    seen = set()
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'pointer', 'expression'}:
            raise ValueError('Each equation edit requires pointer and expression only.')
        pointer = edit['pointer']
        if not isinstance(pointer, str) or pointer not in allowed or pointer in seen:
            raise ValueError('Equation edits must name unique existing editable pointers.')
        seen.add(pointer)
        ast = parse_expression(edit['expression'])
        parts = pointer.strip('/').split('/')
        target = normalized
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        if isinstance(target, list):
            target[int(parts[-1])] = ast
        else:
            target[parts[-1]] = ast
    normalized = validate_behavior(normalized, ids)
    return {'behavior': normalized, 'inspection': inspect_candidate(normalized, requirements, supplied_provenance, origin)}
