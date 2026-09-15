"""Source-linked assumption ledger. Reporting an assumption never verifies it."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from review_profile import digest


def _expression_text(value: Any) -> str:
    """Readable projection of a validated behavior expression, never executable code."""
    if isinstance(value, bool):
        return str(value).lower()
    if not isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if 'var' in value:
        return f"next({value['var']})" if value.get('at') == 'next' else str(value['var'])
    if 'value' in value:
        return str(value['value']) + (f" [{value['unit']}]" if value.get('unit', '1') != '1' else '')
    args = [_expression_text(x) for x in value.get('args', [])]
    operator = value.get('op', '?')
    if operator == 'ite' and len(args) == 3:
        return f"if {args[0]} then {args[1]} else {args[2]}"
    if len(args) == 1:
        return f"{operator} ({args[0]})"
    return '(' + (' ' + operator + ' ').join(args) + ')'


def build_assumptions(tlr: dict | None, requirements: list[dict], engine: str,
                      run_dir: Path, behavior: dict | None = None,
                      behavioral_analysis: dict | None = None,
                      behavior_origin: str = "engineer_supplied") -> list[dict]:
    known = {str(r['id']) for r in requirements}
    ledger: dict[tuple, dict] = {}

    def add(statement, origin, ids=(), formalization='metadata_only', impact='', evidence=None):
        if not isinstance(statement, str) or not statement.strip():
            return
        statement = statement.strip()
        key = (origin, statement, formalization)
        row = ledger.setdefault(key, {
            'id': 'ASM-' + digest(json.dumps(key, ensure_ascii=False))[:12],
            'statement': statement, 'origin': origin, 'requirement_ids': [],
            'formalization': formalization, 'impact': impact,
            'evidence': [], 'review_status': 'pending',
        })
        row['requirement_ids'] = sorted(set(row['requirement_ids']) | (set(map(str, ids)) & known))
        if evidence and evidence not in row['evidence']:
            row['evidence'].append(evidence)

    tlr = tlr or {}
    if engine == 'local':
        for i, req in enumerate(tlr.get('requirements', [])):
            for j, statement in enumerate(req.get('assumptions', [])):
                add(statement, 'local_profile', [req['id']], impact=
                    'The interpretation depends on this premise. Acceptance records an engineering judgment; it does not add an executable predicate.',
                    evidence={'artifact': 'interpretation.json', 'pointer': f'/requirements/{i}/assumptions/{j}'})
        for i, symbol in enumerate(tlr.get('symbols', [])):
            if symbol.get('minimum') is not None:
                ids = [r['id'] for r in tlr.get('requirements', []) if r.get('symbol') == symbol['name']]
                add(f"{symbol['name']} >= {symbol['minimum']} {symbol.get('unit', '')}", 'local_profile', ids,
                    'encoded', 'This domain restriction is asserted in the scalar SMT model. Its applicability still needs review.',
                    {'artifact': 'interpretation.json', 'pointer': f'/symbols/{i}/minimum', 'solver_artifact': 'constraints.smt2'})
    else:
        def walk(value: Any, artifact: str, pointer='', ids=()):
            if isinstance(value, dict):
                own_ids = value.get('requirement_ids', value.get('requirementIds', ids))
                if not isinstance(own_ids, list):
                    own_ids = [own_ids] if own_ids else list(ids)
                if str(value.get('id', '')) in known:
                    own_ids = [str(value['id'])]
                for key, child in value.items():
                    child_pointer = pointer + '/' + str(key).replace('~', '~0').replace('/', '~1')
                    if key == 'unresolved_ranges' and isinstance(child, list):
                        for j, item in enumerate(child):
                            if not isinstance(item, dict):
                                continue
                            statement = 'Numeric interpretation pending: ' + str(item.get('reason', 'No numeric quantity was identified.'))
                            if item.get('original_text'):
                                statement += '\nSource clause: ' + str(item['original_text'])
                            add(statement, 'tlr_preprocessing', own_ids, 'unestablished',
                                'The heuristic observation is incomplete and is not an executable range. Review the complete clause, all thresholds, categories, and population conditions before accepting a formal interpretation.',
                                {'artifact': artifact, 'pointer': child_pointer + '/' + str(j)})
                    if key in ('assumptions', 'global_assumptions', 'implicit_assumptions'):
                        items = child if isinstance(child, list) else [child]
                        for j, item in enumerate(items):
                            if isinstance(item, str):
                                statement, item_ids = item, own_ids
                            elif isinstance(item, dict):
                                statement = item.get('statement') or item.get('text') or item.get('description')
                                item_ids = item.get('requirement_ids', item.get('requirementIds', own_ids))
                                if not isinstance(item_ids, list):
                                    item_ids = [item_ids] if item_ids else []
                            else:
                                continue
                            add(statement, 'llm_reported', item_ids, 'unestablished',
                                'Reported by the generator. Neither source support nor correspondence with the emitted solver/model predicate is established.',
                                {'artifact': artifact, 'pointer': child_pointer + '/' + str(j)})
                    walk(child, artifact, child_pointer, own_ids)
            elif isinstance(value, list):
                for i, child in enumerate(value):
                    walk(child, artifact, pointer + '/' + str(i), ids)

        # The capture files preserve original responses before repair or sanitation.
        paths = sorted(set(run_dir.glob('pipeline*_translate.json')) | set(run_dir.glob('llm-call-*.json')) | set(run_dir.glob('initial_tlr.json')))
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                continue
            if path.name.startswith('llm-call-'):
                response = payload.get('response', '')
                if isinstance(response, str):
                    try:
                        walk(json.loads(response), path.name, '/response')
                    except ValueError:
                        pass
                    for line_number, line in enumerate(response.splitlines(), 1):
                        match = re.match(r'\s*(?:;|//)\s*REVIEW_ASSUMPTION\s+(\{.*\})\s*$', line)
                        if match:
                            try:
                                item = json.loads(match.group(1))
                            except ValueError:
                                continue
                            ids = item.get('requirement_ids', [])
                            add(item.get('statement'), 'llm_reported', ids if isinstance(ids, list) else [],
                                'unestablished', item.get('reason') or 'An added premise reported by the generator; inspect the actual encoding.',
                                {'artifact': path.name, 'response_line': line_number})
            else:
                walk(payload, path.name)
        walk(tlr.get('raw', tlr), 'pipeline_review_interpretation.json')
        add('The generated interpretation, variable domains, event matching, guards, progress rules, and time horizon may contain premises not enumerated by the LLM.',
            'pipeline_audit', known, 'unestablished',
            'This audit is not an exhaustive assumption detector. Compare the source, captured generator responses, SMT assertions, and SysML before acceptance.',
            {'artifact': 'llm_assumption_policy.txt' if (run_dir / 'llm_assumption_policy.txt').exists() else 'pipeline_review_analysis.json'})

    if behavior:
        model_origin = "llm_proposed_model" if behavior_origin == "llm_proposed" else "engineer_model"
        ids = sorted({rid for p in behavior.get('properties', []) for rid in p.get('requirement_ids', [])})
        add('The supplied initial conditions and transition rules are an adequate candidate model of the system being reviewed.',
            model_origin, ids, 'unestablished',
            'The behavioral checker verifies this separate candidate transition model. It does not establish equivalence to the generated SysML behavior or the physical system.',
            {'artifact': 'behavior.json', 'pointer': '/transitions'})
        add('The source-linked behavioral properties accurately express the intended requirements.',
            model_origin, ids, 'unestablished',
            'Source IDs provide traceability, not a semantic equivalence proof. Inspect each trigger, response, deadline, and predicate against its source.',
            {'artifact': 'behavior.json', 'pointer': '/properties'})
        for field, label in (('initial', 'Initial-state premise'), ('transitions', 'Candidate behavior rule')):
            for i, predicate in enumerate(behavior.get(field, [])):
                add(f"{label}: {_expression_text(predicate)}", model_origin, ids, 'encoded',
                    'This expression is asserted in the transition model. Inspect its source support and whether it excludes relevant behavior; acceptance does not prove the expression describes the real system.',
                    {'artifact': 'behavior.json', 'pointer': f'/{field}/{i}'})
        for i, assumption in enumerate(behavior.get('assumptions', [])):
            add(f"{assumption['id']} [{assumption['scope']}]: {assumption.get('text', '')} — {_expression_text(assumption['predicate'])}", model_origin, ids, 'encoded',
                'This premise restricts admissible executions. Rejecting it requires a new candidate and fresh solver evidence.',
                {'artifact': 'behavior.json', 'pointer': f'/assumptions/{i}'})
        for i, variable in enumerate(behavior.get('variables', [])):
            if variable.get('bounds') or variable.get('role') == 'parameter':
                add(f"Domain of {variable['name']} ({variable['role']}): " + json.dumps({k: variable[k] for k in ('type', 'unit', 'bounds', 'value') if k in variable}, sort_keys=True),
                    model_origin, ids, 'encoded',
                    'These bounds delimit the uncertainty or reachable states considered by the solver; excluded cases are outside the result.',
                    {'artifact': 'behavior.json', 'pointer': f'/variables/{i}'})
        add(f"Synchronous discrete time with step {json.dumps(behavior.get('step'))}; analysis horizon {behavior.get('horizon')} steps.",
            'analysis_profile', ids, 'encoded',
            'The analysis covers the sampled transition model and stated horizon. It does not establish continuous-time behavior or unbounded liveness.',
            {'artifact': 'behavior.json', 'pointer': '/step'})
        for i, prop in enumerate(behavior.get('properties', [])):
            if prop.get('kind') in ('bounded_response', 'eventual_response'):
                add(f"Property {prop['id']} uses first-response-after-trigger semantics; request identity and overlap must agree with the modeled protocol.",
                    'analysis_profile', prop.get('requirement_ids', []), 'metadata_only',
                    'An event for another request must not discharge this obligation. Inspect the protocol model and any single-outstanding-request assumption.',
                    {'artifact': 'behavior.json', 'pointer': f'/properties/{i}'})
    return list(ledger.values())


def assumption_review_hash(reviews: list[dict]) -> str:
    return digest(json.dumps(reviews, sort_keys=True, ensure_ascii=False))


def assumption_blockers(run: dict) -> list[str]:
    latest = {r['assumption_id']: r for r in run.get('assumption_reviews', [])}
    unresolved = [a['id'] for a in run.get('assumptions', [])
                  if latest.get(a['id'], {}).get('decision') != 'accept']
    if not unresolved:
        return []
    return [f"{len(unresolved)} assumptions remain pending, rejected, or deferred; inspect their individual decisions."]
