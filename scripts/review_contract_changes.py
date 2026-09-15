"""Scoped semantic comparisons of immutable contract candidates using Z3.

Comparison context contains typed domains, parameters and explicit environment
assumptions, never the candidate's implementation transitions or other guarantees.
Witnesses are contract valuations, not necessarily candidate executions.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import requirements_pipeline as solver
from review_behavior import _emit, _number, _and, _or


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _context(behavior):
    table = {v['name']: v for v in behavior['variables']}
    h = behavior['horizon']
    lines = ['(set-logic QF_LIRA)', '(set-option :produce-models true)']
    names = []
    for v in table.values():
        if v['role'] != 'parameter':
            for i in range(h + 1):
                name = _emit({'var': v['name'], 'at': 'current'}, i, table)
                names.append(name)
                lines.append(f"(declare-const {name} {v['type']})")
        for i in range(1 if v['role'] == 'parameter' else h + 1):
            name = _emit({'var': v['name'], 'at': 'current'}, i, table)
            for k, b in v.get('bounds', {}).items():
                op = '>=' if k == 'lower' else '<='
                lines.append(f'(assert ({op} {name} {_number(b)}))')
    for a in behavior.get('assumptions', []):
        indices = [0] if a['scope'] == 'initial' else range(h) if a['scope'] == 'transition' else range(h + 1)
        for i in indices:
            lines.append(f"(assert {_emit(a['predicate'], i, table)})")
    return '\n'.join(lines) + '\n', names, table


def _formula(prop, horizon, table, indices=None):
    if prop['kind'] in ('always', 'robustness'):
        return _and([_emit(prop['predicate'], i, table) for i in range(horizon + 1)])
    if prop['kind'] == 'bounded_response':
        lo, hi = prop['window']['min'], prop['window']['max']
        terms = []
        for i in indices:
            before = [f"(not {_emit(prop['response'], j, table)})" for j in range(i, i + lo)]
            within = _or([_emit(prop['response'], j, table) for j in range(i + lo, i + hi + 1)])
            terms.append(f"(=> {_emit(prop['trigger'], i, table)} {_and(before + [within])})")
        return _and(terms)
    raise ValueError('Unbounded eventual response has no finite equivalence claim.')


def _query(base, goal, names, directory, stem):
    text = base + (f'(assert {goal})\n' if goal else '') + '(check-sat)\n'
    result = solver.run_z3_fragment(text)
    verdict = solver._solver_verdict(result) or 'unknown'
    if result.get('cross_check') and not result['cross_check'].get('agree'):
        verdict = 'unknown'
    if verdict == 'sat' and names:
        text += '(get-value (' + ' '.join(names) + '))\n'
        result = solver.run_z3_fragment(text)
        if solver._solver_verdict(result) != 'sat' or (result.get('cross_check') and not result['cross_check'].get('agree')):
            verdict = 'unknown'
    query_file, result_file = stem + '.smt2', stem + '.json'
    (directory / query_file).write_text(text, encoding='utf-8')
    result_text = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    (directory / result_file).write_text(result_text, encoding='utf-8')
    witness = {}
    if verdict == 'sat':
        try:
            forms = solver._parse_sexpr('\n'.join(result.get('result', '').splitlines()[1:]))
            def fmt(x):
                return '(' + ' '.join(fmt(y) for y in x) + ')' if isinstance(x, list) else str(x)
            witness = {item[0]: fmt(item[1]) for item in forms[0] if isinstance(item, list) and len(item) == 2}
        except (ValueError, TypeError, IndexError):
            pass
    return {'verdict': verdict, 'artifacts': {'query': query_file, 'result': result_file},
            'query_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'result_sha256': hashlib.sha256(result_text.encode()).hexdigest(),
            'witness': witness, 'witness_kind': 'contract valuation; candidate transitions are not asserted'}


def compare_contract_bundles(before, after, run_dir, parent_run_id=None):
    """Compare guarantees in both directions; preserve all scope boundaries."""
    from review_contracts import behavior_from_contracts
    directory = Path(run_dir)
    directory.mkdir(parents=True, exist_ok=True)
    out = {'schema': 'review_contract_changes/1', 'parent_run_id': parent_run_id,
           'status': 'initial' if before is None and parent_run_id is None else 'compared', 'changes': [],
           'scope': 'Typed domains, fixed parameters and explicit environmental assumptions. Candidate initial/transition rules and other guarantees are excluded. Witnesses show changes in permitted contract valuations, not implementation executions.',
           'limitations': ['Every comparison is scoped to recorded types, units, assumptions and finite observation indices.',
                           'Equivalence in this context does not establish source fidelity or authorize a requirement change.',
                           'The behavior context must be unchanged; changes to assumptions, state, dynamics, parameters or horizon require separate review.']}
    if before is None:
        if parent_run_id:
            out.update(status='unavailable', summary='The parent has no shared-contract snapshot; semantic comparison is unavailable.')
        return out
    old_behavior, new_behavior = behavior_from_contracts(before), behavior_from_contracts(after)
    old = {c['id']: c for c in before['contracts']}
    new = {c['id']: c for c in after['contracts']}
    context_changed = before.get('context_sha256') != after.get('context_sha256')
    if context_changed:
        out.update(status='context_changed', before_context_sha256=before.get('context_sha256'), after_context_sha256=after.get('context_sha256'))
    for cid in sorted(old.keys() | new.keys()):
        a, b = old.get(cid), new.get(cid)
        left = deepcopy((a or {}).get('property') or (a or {}).get('scalar'))
        right = deepcopy((b or {}).get('property') or (b or {}).get('scalar'))
        change = {'contract_id': cid, 'before': left, 'after': right,
                  'source_changed': (a or {}).get('source_refs') != (b or {}).get('source_refs'),
                  'before_source_refs': (a or {}).get('source_refs', []), 'after_source_refs': (b or {}).get('source_refs', [])}
        out['changes'].append(change)
        if a is None or b is None:
            change.update(status='added' if a is None else 'removed', summary='Contract added; interpretation requires review.' if a is None else 'Contract removed; no source-change authority is implied.')
            continue
        if left is None or right is None:
            change.update(status='not_comparable', summary='An executable contract is missing on at least one side.')
            continue
        is_scalar = a.get('scalar') is not None and b.get('scalar') is not None
        if not is_scalar and (context_changed or old_behavior is None or new_behavior is None):
            change.update(status='not_comparable', summary='Behavior context changed or is missing. Assumptions/dynamics must be reviewed before comparing guarantees.')
            continue
        try:
            stem = 'contract_change_' + hashlib.sha256(cid.encode()).hexdigest()[:12]
            if is_scalar:
                keys = ('symbol', 'type', 'unit', 'kind', 'subject', 'quantity', 'trigger', 'response', 'context', 'minimum')
                if any(left.get(k) != right.get(k) for k in keys):
                    raise ValueError('Scalar binding, dimensions or operating context changed.')
                name = right['symbol']
                # Scalar symbols are emitted by the strict local grammar, never free-form source text.
                import re
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                    raise ValueError('Invalid scalar symbol.')
                base = '(set-logic QF_LRA)\n(set-option :produce-models true)\n' + f'(declare-const {name} Real)\n'
                if right.get('minimum') is not None:
                    from decimal import Decimal
                    minimum = format(Decimal(right['minimum']), 'f')
                    if not Decimal(minimum).is_finite():
                        raise ValueError('Non-finite scalar domain.')
                    base += f'(assert (>= {name} {_number(minimum)}))\n'
                ops = {'le': '<=', 'lt': '<', 'ge': '>=', 'gt': '>', 'eq': '='}
                # Native scalar validation also enforces exact decimal literals.
                from decimal import Decimal
                lv, rv = format(Decimal(left['value']), 'f'), format(Decimal(right['value']), 'f')
                if not Decimal(lv).is_finite() or not Decimal(rv).is_finite():
                    raise ValueError('Non-finite scalar value.')
                old_formula = f"({ops[left['relation']]} {name} {_number(lv)})"
                new_formula = f"({ops[right['relation']]} {name} {_number(rv)})"
                names = [name]
                change['comparison_scope'] = {'kind': 'scalar', 'unit': right.get('unit'), 'other_requirements_excluded': True}
            else:
                if left['kind'] != right['kind']:
                    raise ValueError('Property kind changed; no automatic cross-kind comparison is defined.')
                base, names, table = _context(new_behavior)
                h = new_behavior['horizon']
                indices = None
                if right['kind'] == 'bounded_response':
                    hi = max(left['window']['max'], right['window']['max'])
                    indices = list(range(max(0, h - hi + 1)))
                    if not indices:
                        raise ValueError('No common fully observed response window fits within the horizon.')
                old_formula = _formula(left, h, table, indices)
                new_formula = _formula(right, h, table, indices)
                change['comparison_scope'] = {'kind': 'finite_contract', 'horizon': h, 'step': new_behavior['step'],
                                               'complete_trigger_steps': indices, 'late_triggers_excluded': indices is not None,
                                               'correlation': 'First Boolean response per trigger; overlapping transaction identities are not modeled.'}
            feasible = _query(base, None, names, directory, stem + '_context')
            change['context_feasibility'] = feasible
            if feasible['verdict'] != 'sat':
                change.update(status='unknown', summary='Comparison context is infeasible or unknown; no vacuous equivalence is claimed.')
                continue
            permitted = _query(base, f'(and {new_formula} (not {old_formula}))', names, directory, stem + '_permitted')
            forbidden = _query(base, f'(and {old_formula} (not {new_formula}))', names, directory, stem + '_forbidden')
            change.update(newly_permitted=permitted, newly_forbidden=forbidden)
            pair = permitted['verdict'], forbidden['verdict']
            status = {('sat', 'unsat'): 'weakened', ('unsat', 'sat'): 'strengthened', ('sat', 'sat'): 'changed', ('unsat', 'unsat'): 'equivalent'}.get(pair, 'unknown')
            change.update(status=status, summary={
                'weakened': 'The proposal permits valuations that violate the previous contract.',
                'strengthened': 'The proposal excludes valuations permitted by the previous contract.',
                'changed': 'The proposal both permits and excludes previously different valuations.',
                'equivalent': 'No difference was found within the recorded comparison context and finite scope; source interpretation still requires review.',
                'unknown': 'At least one comparison query was inconclusive.'}[status])
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            change.update(status='not_comparable', summary=str(exc))
    return out
