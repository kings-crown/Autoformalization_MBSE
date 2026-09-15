"""Deterministic SysML v2 projection of reviewed contract candidates.

The package is an explicit finite diagnostic model, not inferred allocation to
an architectural part. Property constraints remain separate from model premises.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from review_contracts import behavior_from_contracts
from review_model_index import build_model_inspection


def _doc(text: Any) -> str:
    return str(text).replace('*/', '* /').replace('/*', '/ *').replace('\x00', '')


def _and(parts: list[str]) -> str:
    return 'true' if not parts else parts[0] if len(parts) == 1 else '(' + ' and '.join(parts) + ')'


def _or(parts: list[str]) -> str:
    return 'false' if not parts else parts[0] if len(parts) == 1 else '(' + ' or '.join(parts) + ')'


def _expr(ast: Any, step: int, table: dict, subject: str = '') -> str:
    if isinstance(ast, bool):
        return 'true' if ast else 'false'
    if 'value' in ast:
        return '(' + ast['value'] + ')' if ast['value'].startswith('-') else ast['value']
    if 'var' in ast:
        var = table[ast['var']]
        if var['role'] == 'parameter':
            return str(var['value']).lower() if var['type'] == 'Bool' else ('(' + var['value'] + ')' if var['value'].startswith('-') else var['value'])
        return subject + 'v_' + var['name'] + '_' + str(step + (ast.get('at') == 'next'))
    args = [_expr(arg, step, table, subject) for arg in ast['args']]
    op = ast['op']
    if op == 'ite':
        return '(if ' + args[0] + ' ? ' + args[1] + ' else ' + args[2] + ')'
    if len(args) == 1:
        return '(' + op + ' ' + args[0] + ')'
    op = {'=': '==', 'implies': 'implies'}.get(op, op)
    return '(' + (' ' + op + ' ').join(args) + ')'


def render_contract_sysml(bundle: dict) -> dict:
    """Return exact text/index/map; never mutate the bundle or run tools."""
    behavior = behavior_from_contracts(bundle)
    # Include guarantee/scalar content in the package identity to avoid old/new
    # projection collisions when engineer candidates are viewed together.
    identity = json.dumps({'behavior': bundle['behavior_sha256'], 'scalars': bundle.get('scalar_contracts_sha256'),
                           'source': bundle['requirements_sha256']}, sort_keys=True)
    package = 'ContractReview_' + hashlib.sha256(identity.encode()).hexdigest()[:12]
    lines = [f'package {package} {{', '    private import ScalarValues::*;',
             '    doc /* Finite diagnostic contract projection for engineer review.',
             '       Numeric attributes are canonical scalar magnitudes; physical units are metadata here.',
             '       The shared AST was dimension-checked before rendering. This package does not',
             '       allocate behavior to domain architecture or claim requirement satisfaction.',
             '       Source fidelity remains pending even when compiler and solver checks pass.',
             '    */']
    hints, mapping = [], {}
    table = {v['name']: v for v in (behavior or {}).get('variables', [])}
    if behavior:
        h = behavior['horizon']
        lines += [f"    doc /* Scope: H={h} transitions, states 0..{h}; step={behavior['step']['value']} s.",
                  '       Full horizon executions only. Deadlocks, shorter nonextendable executions,',
                  '       realizability and unbounded liveness are outside the finite checks. */',
                  '    part def FiniteTrace {']
        for var in behavior['variables']:
            scalar = {'Bool': 'Boolean', 'Int': 'Integer', 'Real': 'Real'}[var['type']]
            if var['role'] == 'parameter':
                # References use this same fixed literal in both backends.
                lines.append('        doc /* Fixed parameter ' + _doc(var['name']) + ': ' + _doc(var['value']) + ' ' + _doc(var.get('unit', '1')) + '. */')
                continue
            for step in range(h + 1):
                name = f"v_{var['name']}_{step}"
                lines += [f'        attribute {name} : ScalarValues::{scalar} {{',
                          '            doc /* Role: ' + var['role'] + '; canonical unit: ' + _doc(var.get('unit', '1')) + f'; step: {step}. */', '        }']
        lines.append('        doc /* ENVIRONMENT: domain bounds and explicit assumptions restrict admissible executions. */')
        for vi, var in enumerate(behavior['variables']):
            for step in range(1 if var['role'] == 'parameter' else h + 1):
                name = _expr({'var': var['name'], 'at': 'current'}, step, table)
                for bound, operator in (('lower', '>='), ('upper', '<=')):
                    if bound in var.get('bounds', {}):
                        lines.append(f"        assert constraint domain_{vi}_{step}_{bound} {{ {name} {operator} {var['bounds'][bound]} }}")
        for ai, assumption in enumerate(behavior['assumptions']):
            steps = [0] if assumption['scope'] == 'initial' else range(h) if assumption['scope'] == 'transition' else range(h + 1)
            lines.append('        doc /* Assumption ' + _doc(assumption['id']) + ': ' + _doc(assumption['text']) + ' */')
            for step in steps:
                lines.append(f"        assert constraint environment_{ai}_{step} {{ {_expr(assumption['predicate'], step, table)} }}")
        lines.append('        doc /* INITIAL: candidate initial-state premises, separate from guarantees. */')
        for i, ast in enumerate(behavior['initial']):
            lines.append(f'        assert constraint initial_{i} {{ {_expr(ast, 0, table)} }}')
        lines.append('        doc /* TRANSITIONS: candidate evolution, separate from guarantees. */')
        for step in range(h):
            for i, ast in enumerate(behavior['transitions']):
                lines.append(f'        assert constraint transition_{step}_{i} {{ {_expr(ast, step, table)} }}')
        lines.append('    }')
        lines.append('    part candidateTrace : FiniteTrace;')
    for number, row in enumerate(bundle['contracts'], 1):
        prop, scalar = row.get('property'), row.get('scalar')
        name = f'Contract_{number}'
        qname = package + '::' + name
        text = '\n'.join(ref['requirement_id'] + ': ' + ref['text'] + '\nSource: ' + json.dumps(ref['source'], ensure_ascii=False, sort_keys=True) for ref in row['source_refs'])
        lines += [f'    requirement {name} {{', '        doc /* ' + _doc(text),
                  '           Contract: ' + _doc(row['id']) + '; rule: ' + _doc(row['rule']['id']) + '@' + _doc(row['rule']['version']) + '.',
                  '           Rule basis: deterministic property pattern; original generator rule use is unestablished.',
                  '           Source alignment: pending; architecture owner: unallocated. */']
        formulae = []
        projection_kind = 'documentation_only'
        if prop and prop['kind'] != 'eventual_response':
            lines.append('        subject observedTrace : FiniteTrace = candidateTrace;')
            if prop['kind'] in {'always', 'robustness'}:
                formulae = [(f'guarantee_step_{step}', _expr(prop['predicate'], step, table, 'observedTrace.')) for step in range(behavior['horizon'] + 1)]
                projection_kind = 'finite_state_guarantee'
                if prop['kind'] == 'robustness':
                    lines.append('        doc /* Robustness is scoped to the declared disturbance domains; no worst-case margin optimization is encoded. */')
            else:
                lo, hi, h = prop['window']['min'], prop['window']['max'], behavior['horizon']
                complete = list(range(max(0, h - hi + 1)))
                pending = list(range(max(0, h - hi + 1), h + 1))
                lines.append(f'        doc /* First response after trigger; inclusive window [{lo}, {hi}] step offsets.')
                lines.append('           Complete trigger steps: ' + str(complete) + '; pending trigger steps: ' + str(pending) + '.')
                lines.append('           Trigger reachability and absence of overlapping outstanding commands are separate solver obligations.')
                lines.append('           Pending windows are not discharged by this projection. */')
                for step in complete:
                    trigger = _expr(prop['trigger'], step, table, 'observedTrace.')
                    early = ['(not ' + _expr(prop['response'], index, table, 'observedTrace.') + ')' for index in range(step, step + lo)]
                    within = _or([_expr(prop['response'], index, table, 'observedTrace.') for index in range(step + lo, step + hi + 1)])
                    formulae.append((f'guarantee_trigger_{step}', '(' + trigger + ' implies ' + _and(early + [within]) + ')'))
                projection_kind = 'finite_first_response_guarantee' if complete else 'pending_window_documentation'
        elif prop:
            lines.append('        doc /* Unbounded eventual response remains unproved; no finite surrogate constraint is emitted.')
            lines.append('           Trigger AST: ' + _doc(json.dumps(prop['trigger'], sort_keys=True)) + '.')
            lines.append('           Response AST: ' + _doc(json.dumps(prop['response'], sort_keys=True)) + '. */')
            projection_kind = 'unbounded_obligation_documentation'
        elif scalar:
            # The scalar model already has shared quantity identity across clauses.
            # Duplicating an independent subject here would lose that identity.
            lines.append('        doc /* Existing scalar constraint: ' + _doc(scalar['symbol']) + ' ' + _doc(scalar['relation']) + ' ' + _doc(scalar['value']) + ' ' + _doc(scalar['unit']) + '.')
            lines.append('           See the original shared scalar SysML requirement for the executable quantity relationship.')
            lines.append('           This projection retains its existing model references without creating an independent scalar quantity. */')
            projection_kind = 'existing_scalar_reference'
        else:
            lines.append('        doc /* No validated executable interpretation; source retained for investigation. */')
        for cname, expression in formulae:
            lines.append(f'        require constraint {cname} {{ {expression} }}')
        lines.append('    }')
        hint = {'kind': 'requirement', 'name': qname, 'contract_id': row['id'],
                'requirement_ids': list(row['requirement_ids']), 'projection_kind': projection_kind,
                'formalized': bool(formulae)}
        if len(row['requirement_ids']) == 1:
            hint['requirement_id'] = row['requirement_ids'][0]
        hints.append(hint)
        mapping[row['id']] = {'qualified_name': qname, 'projection_kind': projection_kind,
                              'formalization': 'encoded' if formulae else 'metadata_only',
                              'constraint_names': [qname + '::' + cname for cname, _ in formulae],
                              'source_alignment': 'pending'}
    lines += ['}', '']
    text = '\n'.join(lines)
    model = {'text': text, 'kind': 'finite_contract_projection', 'elements': hints,
             'package': package, 'contract_map': mapping,
             'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
             'summary': {'contracts': len(mapping), 'behavior_present': behavior is not None,
                         'architecture_allocation': 'unallocated', 'source_alignment': 'pending'},
             'limitations': list(bundle['limitations'])}
    inspection = build_model_inspection(model, bundle['requirements'], None)
    indexed = {e.get('qualified_name'): e for e in inspection['elements']}
    for cid, mapped in mapping.items():
        entry = indexed.get(mapped['qualified_name'])
        if entry:
            mapped.update({key: entry[key] for key in ('id', 'start_line', 'end_line', 'start_offset', 'end_offset')})
            mapped['text_sha256'] = model['text_sha256']
    for hint in hints:
        hint.update(mapping[hint['contract_id']])
    model['inspection'] = inspection
    return model
