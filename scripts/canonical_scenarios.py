"""Source-grounded development scenarios and static repair change evidence.

Fixtures are declared development assistance, not held-out evaluation or a proof
of source intent. Queries use the same typed expressions as SMT and SysML. No
provider call or automatic change to source, vocabulary or expectations occurs.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from typing import Any

import mutation_core as solver_core
from canonical_tlr import conjunction, tlr_context, validate_tlr, _references

SCHEMA = 'development_scenarios/1'
RUN_SCHEMA = 'development_scenario_run/1'


def _write(path, value):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write('\n')


def _keys(value, required, label):
    if not isinstance(value, dict) or set(value) != set(required):
        raise ValueError(f'{label} has missing or unexpected fields.')


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 100000:
        raise ValueError(f'{label} must be nonempty text.')
    return value


def _strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _source_index(sources):
    if not isinstance(sources, list) or not sources:
        raise ValueError('Scenario sources must be a nonempty list.')
    targets, quotes = {}, {}

    def visit(value):
        if isinstance(value, dict):
            rid = value.get('id')
            if isinstance(rid, str) and rid.strip():
                quotes.setdefault(rid, []).extend(_strings(value))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for source in sources:
        if not isinstance(source, dict):
            raise ValueError('Source record must be an object.')
        rid = _text(source.get('id'), 'Source ID')
        text = _text(source.get('text'), 'Source text')
        if rid in targets:
            raise ValueError('Duplicate source requirement ID.')
        targets[rid] = text
        quotes.setdefault(rid, []).extend([text, *_strings(source.get('source', {}))])
        visit(source.get('source', {}))
    return targets, quotes


def _mapping(rows, key):
    return {row[key]: row for row in rows}


def _binding(suite, tlr):
    """Preserve shared definitions; missing symbols are an explicit recovery gap."""
    actual = _mapping(tlr['variables'], 'name')
    declared = _mapping(suite['context']['variables'], 'name')
    for name, variable in declared.items():
        if name not in actual:
            continue
        candidate = {k: v for k, v in actual[name].items() if k != 'description'}
        if candidate != variable or actual[name].get('description') != suite['symbol_meanings'].get(name):
            raise ValueError(f'Scenario symbol {name} changed type, units, bounds or meaning.')
    if _mapping(tlr['assumptions'], 'id') != _mapping(suite['context']['background'], 'id'):
        raise ValueError('Scenario background and candidate assumptions differ; revise the input explicitly.')
    variables = list(suite['context']['variables'])
    variables.extend({k: v for k, v in variable.items() if k != 'description'}
                     for name, variable in actual.items() if name not in declared)
    context = solver_core.validate_context(variables, suite['context']['background'])
    return context, sorted(set(declared) - set(actual))


def validate_scenario_suite(suite: Any, sources: list[dict], tlr: dict | None = None) -> dict:
    """Validate fixed expectations and literal source grounding without an LLM.

    Each target requires a quote from its own original text. Supplemental quotes
    may reference actual nested context IDs. Quotation checks do not validate the
    correctness of the expected result. Missing candidate symbols are permitted
    so a recovery proposal can introduce explicitly declared source meanings.
    """
    _keys(suite, {'schema', 'role', 'origin', 'context', 'symbol_meanings', 'scenarios'}, 'Scenario suite')
    if suite['schema'] != SCHEMA or suite['role'] != 'development':
        raise ValueError('Scenarios must declare development_scenarios/1 and development role.')
    _keys(suite['origin'], {'kind', 'description'}, 'Scenario origin')
    if suite['origin']['kind'] not in {'llm_prepared', 'human_prepared', 'fixture'}:
        raise ValueError('Scenario preparation origin must be explicitly declared.')
    _text(suite['origin']['description'], 'Scenario origin description')
    _keys(suite['context'], {'variables', 'background'}, 'Scenario context')
    context = solver_core.validate_context(suite['context']['variables'], suite['context']['background'])
    meanings = suite['symbol_meanings']
    if not isinstance(meanings, dict) or set(meanings) != {v['name'] for v in context['variables']}:
        raise ValueError('Symbol meanings must describe every declared scenario variable exactly once.')
    for description in meanings.values():
        _text(description, 'Symbol meaning')
    targets, quotes = _source_index(sources)
    rows = suite['scenarios']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
        raise ValueError('Provide between 1 and 500 development scenarios.')
    normalized, seen = [], set()
    for row in rows:
        _keys(row, {'id', 'requirement_ids', 'description', 'predicate', 'expected', 'source_basis', 'rationale'}, 'Scenario')
        sid = _text(row['id'], 'Scenario ID')
        if sid in seen:
            raise ValueError('Duplicate scenario ID.')
        seen.add(sid)
        ids = row['requirement_ids']
        if (not isinstance(ids, list) or not ids or any(not isinstance(rid, str) for rid in ids)
                or len(set(ids)) != len(ids) or set(ids) - set(targets)):
            raise ValueError(f'{sid}: scenario targets must reference unique source requirement IDs.')
        if row['expected'] not in {'sat', 'unsat'}:
            raise ValueError('Scenario expected result must be sat or unsat.')
        _text(row['description'], 'Scenario description')
        _text(row['rationale'], 'Scenario rationale')
        basis = row['source_basis']
        if not isinstance(basis, list) or not basis or len(basis) > 64:
            raise ValueError('Scenario source basis must contain literal source quotes.')
        grounded = set()
        for item in basis:
            _keys(item, {'source_id', 'quote'}, 'Scenario source quote')
            rid = _text(item['source_id'], 'Quoted source ID')
            quote = _text(item['quote'], 'Source quote')
            if rid not in quotes or not any(quote in text for text in quotes[rid]):
                raise ValueError(f'{sid}: source quote is not literal text of the referenced source/context.')
            if rid in targets and quote in targets[rid]:
                grounded.add(rid)
        if set(ids) - grounded:
            raise ValueError(f'{sid}: every target needs its own literal source requirement quote.')
        predicate = solver_core.validate_formula(row['predicate'], context)
        if not _references(predicate):
            raise ValueError('A scenario must constrain declared variables, not be a truth constant.')
        normalized.append({**deepcopy(row), 'predicate': predicate})
    result = {**deepcopy(suite), 'context': context, 'scenarios': normalized}
    if tlr is not None:
        _binding(result, validate_tlr(tlr, sources))
    return result


def _settings(timeout_seconds, solver):
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 3600):
        raise ValueError('timeout_seconds must be finite, positive and at most 3600.')
    if not isinstance(solver, str) or not solver.strip() or '\x00' in solver:
        raise ValueError('solver must be a local Z3 executable name or path.')


def run_scenarios(tlr: dict, suite: dict, sources: list[dict], output_dir: str | Path,
                  timeout_seconds: float = 10, solver: str = 'z3') -> dict:
    """Persist Gamma, model consistency, scenario feasibility and expected queries.

    Expected SAT means a permitted valuation, not that an action must occur.
    Encode a required outcome by expecting UNSAT for its violating scenario.
    Scenarios are only scored when Gamma, the supported candidate conjunction,
    and Gamma AND scenario are SAT, and all target requirements are represented.
    """
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError('Scenario output directory must be empty; evidence is never overwritten.')
    result = {'schema': RUN_SCHEMA, 'status': 'encoding_error', 'scenarios': [],
              'background_status': 'not_run', 'consistency_status': 'not_run',
              'limitations': ['These are declared development expectations, not held-out evaluation.',
                 'Literal source grounding does not establish the correctness of an expectation.',
                 'SAT establishes an allowed static valuation, not an obligation or execution trace.',
                 'UNSAT is interpreted only after background, scenario and candidate feasibility checks.']}
    try:
        _settings(timeout_seconds, solver)
        fixed = validate_scenario_suite(suite, sources)
        result.update(suite=fixed, scenarios=[{'id': row['id'], 'requirement_ids': row['requirement_ids'],
            'expected': row['expected'], 'status': 'not_run', 'description': row['description']}
            for row in fixed['scenarios']])
        normalized = validate_tlr(tlr, sources)
        context, missing = _binding(fixed, normalized)
        result.update(missing_bindings=missing,
                      requirement_ids=[r['id'] for r in normalized['requirements']])
        _write(directory / 'tlr.json', normalized)
        _write(directory / 'suite.json', fixed)
        base, names = solver_core._base(context)
        version = solver_core._version(solver, timeout_seconds)
        supported = [r for r in normalized['requirements'] if r['status'] == 'supported']
        supported_ids = {r['id'] for r in supported}
        expression = solver_core.emit_formula(conjunction([r['formula'] for r in supported]), context)
        result['supported_requirement_ids'] = sorted(supported_ids)

        def run(label, predicate, purpose):
            evidence = solver_core._query(directory, label, base, predicate, names, solver, timeout_seconds, version)
            evidence['purpose'] = purpose
            evidence['query_smt2'] = (directory / evidence['artifacts']['query']).read_text(encoding='utf-8')
            if 'witness_evidence' in evidence:
                witness = evidence['witness_evidence']
                witness['query_smt2'] = (directory / witness['artifacts']['query']).read_text(encoding='utf-8')
            return evidence

        background = run('background', None, 'Feasibility of Gamma, the declared domains and environmental assumptions only.')
        result.update(background=background, background_status=background['status'])
        if background['status'] == 'sat' and supported:
            consistency = run('candidate_consistency', expression, 'Feasibility of Gamma AND every supported candidate requirement.')
        else:
            consistency = {'status': 'blocked' if background['status'] != 'sat' else 'unsupported',
                           'reason': 'Feasible background and at least one supported candidate are required.'}
        result.update(consistency=consistency, consistency_status=consistency['status'])
        for index, row in enumerate(fixed['scenarios'], 1):
            entry = result['scenarios'][index - 1]
            if background['status'] != 'sat':
                entry.update(status='blocked' if background['status'] == 'unsat' else 'inconclusive',
                             reason='Background feasibility has not been established.', code='background_not_feasible')
                continue
            scenario = solver_core.emit_formula(row['predicate'], context)
            feasible = run(f'scenario_{index:04d}_feasibility', scenario, 'Feasibility of Gamma AND scenario, before assuming candidate requirements.')
            entry['feasibility'] = feasible
            if feasible['status'] != 'sat':
                entry.update(status='blocked' if feasible['status'] == 'unsat' else 'inconclusive',
                             code='infeasible_scenario' if feasible['status'] == 'unsat' else 'scenario_feasibility_inconclusive',
                             reason='An infeasible or inconclusive scenario cannot establish an expected outcome.')
                continue
            gaps = sorted(_references(row['predicate']) & set(missing))
            if gaps:
                entry.update(status='not_run', code='binding_gap', missing_bindings=gaps,
                             reason='Candidate does not yet declare the scenario symbols with their fixed source meanings.')
                continue
            uncovered = sorted(set(row['requirement_ids']) - supported_ids)
            if uncovered:
                entry.update(status='unsupported', code='unrepresented_target', unsupported_requirement_ids=uncovered,
                             reason='Every target must have an executable candidate formula to receive scenario credit.')
                continue
            if consistency['status'] != 'sat':
                entry.update(status='blocked' if consistency['status'] == 'unsat' else 'inconclusive',
                             code='candidate_not_feasible', reason='The supported candidate conjunction must be SAT before scenario scoring.')
                continue
            check = run(f'scenario_{index:04d}_candidate', f'(and {expression} {scenario})',
                        'Expected result of Gamma AND supported candidate conjunction AND scenario.')
            entry['check'] = check
            if check['status'] in {'sat', 'unsat'}:
                entry['status'] = 'passed' if check['status'] == row['expected'] else 'failed'
            else:
                entry.update(status='inconclusive', reason='No conclusive solver result is available.')
        counts = {status: sum(row['status'] == status for row in result['scenarios'])
                  for status in ('passed', 'failed', 'not_run', 'unsupported', 'blocked', 'inconclusive')}
        result['counts'] = counts
        result['diagnostics'] = [{'id': row['id'], 'code':
            'rule_mismatch' if row['status'] == 'failed' else
            'missing_binding' if row.get('code') == 'binding_gap' else
            'unsupported_semantics' if row['status'] == 'unsupported' else
            'solver_inconclusive' if row['status'] == 'inconclusive' else row.get('code', row['status']),
            'status': row['status']} for row in result['scenarios'] if row['status'] != 'passed']
        if background['status'] == 'unsat':
            result['diagnostics'].append({'code': 'inconsistent_background', 'status': 'blocked'})
        if consistency['status'] == 'unsat':
            result['diagnostics'].append({'code': 'inconsistent_specification', 'status': 'blocked'})
        result['status'] = ('blocked' if background['status'] == 'unsat' or consistency['status'] == 'unsat'
                            else 'inconclusive' if counts['inconclusive'] or consistency['status'] in {'unknown', 'timeout', 'solver_error', 'encoding_error'}
                            else 'blocked' if counts['blocked']
                            else 'failed' if counts['failed'] else 'incomplete' if counts['not_run'] or counts['unsupported'] else 'passed')
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError) as exc:
        result.update(status='encoding_error', diagnostic=str(exc), diagnostics=[{
            'code': 'context_revision_required' if 'background' in str(exc).lower() else
            'definition_mismatch' if 'changed type' in str(exc) else 'encoding_error',
            'reason': str(exc)}])
    _write(directory / 'scenarios.json', result)
    return result


def compare_scenario_runs(before: dict, after: dict) -> dict:
    """Gate loss of previous passes; unresolved expectations never become passes."""
    for value in (before, after):
        if value.get('schema') != RUN_SCHEMA or not isinstance(value.get('scenarios'), list):
            raise ValueError('Compare two development scenario run reports.')
    if before.get('suite') != after.get('suite'):
        raise ValueError('Scenario expectations/context changed between attempts.')
    old, new = (_mapping(value['scenarios'], 'id') for value in (before, after))
    if set(old) != set(new):
        raise ValueError('Scenario IDs changed between attempts.')
    changes = [{'id': sid, 'before': old[sid]['status'], 'after': new[sid]['status']} for sid in old
               if old[sid]['status'] != new[sid]['status']]
    regressions = [r['id'] for r in changes if r['before'] == 'passed' and r['after'] != 'passed']
    improvements = [r['id'] for r in changes if r['before'] != 'passed' and r['after'] == 'passed']
    fatal = (after.get('status') in {'encoding_error', 'blocked', 'inconclusive'}
             or after.get('consistency_status') != 'sat')
    return {'schema': 'development_scenario_delta/1', 'acceptable': not regressions and not fatal,
            'regressions': regressions, 'improvements': improvements, 'changes': changes,
            'before_counts': deepcopy(before.get('counts', {})), 'after_counts': deepcopy(after.get('counts', {})),
            'reason': 'Preserve all previously passing development tests and reject inconclusive or invalid candidates. Remaining failed/unrepresented tests remain visible.',
            'limitation': 'Passing this guard is not source fidelity, complete evaluation or engineer approval.'}


def compare_tlr_semantics(before: dict, after: dict, output_dir: str | Path,
                          timeout_seconds: float = 10, solver: str = 'z3') -> dict:
    """Explain changed supported formulas under Gamma without assuming guarantees.

    Declaration or meaning changes have no implicit variable mapping. Such cases
    remain unsupported rather than comparing unrelated symbols or vacuous rules.
    """
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError('Semantic change output directory must be empty.')
    result = {'schema': 'development_semantic_changes/1', 'status': 'compared', 'requirements': [],
              'scope': 'Domains and environmental assumptions only; no implementation or other requirement guarantees.'}
    try:
        _settings(timeout_seconds, solver)
        old, new = (validate_tlr(value) for value in (before, after))
        old_rows, new_rows = (_mapping(value['requirements'], 'id') for value in (old, new))
        if set(old_rows) != set(new_rows):
            raise ValueError('Semantic comparison cannot add or remove source requirements.')
        same_context = (_mapping(old['variables'], 'name') == _mapping(new['variables'], 'name')
                        and _mapping(old['assumptions'], 'id') == _mapping(new['assumptions'], 'id'))
        for index, rid in enumerate(old_rows, 1):
            a, b = old_rows[rid], new_rows[rid]
            if a == b:
                continue
            row = {'id': rid, 'before_status': a['status'], 'after_status': b['status']}
            result['requirements'].append(row)
            if not same_context:
                row.update(status='unsupported', reason='Declared vocabulary, meanings or background differ; no reviewed mapping was supplied.')
            elif a['status'] != 'supported' or b['status'] != 'supported':
                row.update(status='unsupported', reason='Both versions must supply a supported formula for bidirectional comparison.')
            else:
                evidence = solver_core.compare_formulas(tlr_context(old), a['formula'], b['formula'],
                    directory / f'requirement_{index:04d}', timeout_seconds=timeout_seconds, solver=solver)
                row.update(status=evidence['status'], classification=evidence['classification'],
                           evidence=evidence, artifacts={'directory': f'requirement_{index:04d}'})
        if not same_context:
            result.update(status='unsupported', reason='Changed vocabulary/background requires an explicitly reviewed mapping.')
        elif any(r['status'] != 'compared' for r in result['requirements']):
            result['status'] = 'partial'
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError) as exc:
        result.update(status='encoding_error', diagnostic=str(exc))
    _write(directory / 'semantic_changes.json', result)
    return result
