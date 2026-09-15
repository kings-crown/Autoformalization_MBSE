"""Inspectable contract projections over the validated finite behavior AST.

Rules identify deterministic property patterns, not undocumented LLM reasoning.
Source alignment and semantic allocation remain engineer review obligations.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from review_behavior import validate_behavior, _num, _unit

SCHEMA = 'review_contracts/1'
RULE_VERSION = '1.0.0'
RULE_REGISTRY_PATH = Path(__file__).with_name('contract_rules.json')
RULE_REGISTRY = json.loads(RULE_REGISTRY_PATH.read_text(encoding='utf-8'))
RULES = RULE_REGISTRY['rules']
LIMITATIONS = [
    'Rule provenance is a deterministic classification of the validated property AST, not evidence that an LLM used that rule.',
    'Source references and shared generation do not prove source fidelity or equivalence to the inferred domain architecture.',
    'The SysML projection describes the finite diagnostic behavior model; semantic allocation to the system architecture remains unapproved.',
    'Finite checks cover full horizon executions; deadlocks, shorter nonextendable executions, realizability and unbounded liveness are not established.',
    'Initial predicates, transitions, domain bounds and environment assumptions restrict the checked executions and require review.',
]


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def _sources(requirements: Any) -> list[dict]:
    if not isinstance(requirements, list):
        raise ValueError('Contract sources must be a list.')
    ids = set()
    result = []
    for source in requirements:
        if not isinstance(source, dict) or not isinstance(source.get('id'), str) or not source['id'] or source['id'] in ids:
            raise ValueError('Contract sources require unique nonempty string IDs.')
        if not isinstance(source.get('text'), str):
            raise ValueError('Contract sources require exact text.')
        ids.add(source['id'])
        result.append(deepcopy(source))
    return result


def _source_ref(source: dict) -> dict:
    return {'requirement_id': source['id'], 'text': source['text'],
            'source': deepcopy(source.get('source', {})),
            'owner': deepcopy(source.get('owner')), 'authority': deepcopy(source.get('authority'))}


def _rule(kind: str | None) -> dict:
    rule = next((rule for rule in RULES if rule['kind'] == kind), None)
    if rule is None:
        raise ValueError('Unsupported contract rule kind.')
    return {**deepcopy(rule), 'basis': 'deterministic_property_pattern',
            'generation_provenance': 'No claim is made about rules used by the original generator.'}


def _owner_candidates(model: dict | None, ids: list[str]) -> list[dict]:
    if not isinstance(model, dict):
        return []
    index = model.get('inspection', model)
    result, seen = [], set()
    for element in index.get('elements', []):
        if (not isinstance(element, dict) or element.get('kind') != 'part'
                or element.get('mapping_basis') in {'documentation_reference', 'none'}
                or element.get('formalization') == 'metadata_only'):
            continue
        name = element.get('qualified_name')
        linked = set(element.get('source_requirement_ids', [])) & set(ids)
        if not name or name in seen or not linked:
            continue
        seen.add(name)
        result.append({'qualified_name': name, 'element_id': element.get('id'),
                       'requirement_ids': sorted(linked), 'basis': 'existing_model_index_hint',
                       'status': 'candidate_only'})
    return result



def _scalar_records(tlr: dict | None, by_id: dict, covered: set) -> list[dict]:
    if not isinstance(tlr, dict) or tlr.get('schema') != 'review_tlr/1':
        return []
    result, seen = [], set()
    symbols = {s['name']: s for s in tlr.get('symbols', []) if isinstance(s, dict) and s.get('name')}
    for clause in tlr.get('requirements', []):
        if not isinstance(clause, dict) or clause.get('id') not in by_id or clause['id'] in covered or clause.get('status') != 'supported':
            continue
        if clause['id'] in seen:
            raise ValueError('Duplicate supported scalar contract source.')
        seen.add(clause['id'])
        if clause.get('text') != by_id[clause['id']]['text']:
            raise ValueError('Scalar interpretation source text differs from the recorded source.')
        symbol = symbols.get(clause.get('symbol'))
        if not symbol or symbol.get('type') != 'Real' or symbol.get('unit') != clause.get('unit'):
            raise ValueError('Scalar contract symbol type or unit is inconsistent.')
        if clause.get('relation') not in {'le', 'ge', 'eq', 'lt', 'gt'}:
            raise ValueError('Scalar contract relation is unsupported.')
        unit, factor = _unit(clause.get('unit'))
        if factor != 1 or unit != clause.get('unit'):
            raise ValueError('Scalar contracts require normalized canonical units.')
        scalar = {key: deepcopy(clause.get(key, '')) for key in ('symbol', 'unit', 'relation', 'subject', 'quantity', 'trigger', 'response', 'context', 'kind')}
        scalar.update(type='Real', value=_num(clause['value']))
        if 'minimum' in symbol:
            scalar['minimum'] = _num(symbol['minimum'])
        result.append({'requirement_id': clause['id'], 'scalar': scalar})
    return result


def _existing_refs(model: dict | None, ids: list[str]) -> list[dict]:
    index = (model or {}).get('inspection', model or {})
    return [{key: deepcopy(e[key]) for key in ('id', 'qualified_name', 'start_line', 'end_line', 'kind', 'mapping_basis') if key in e}
            for e in index.get('elements', []) if isinstance(e, dict)
            and e.get('kind') in {'requirement', 'constraint'} and set(ids) & set(e.get('source_requirement_ids', []))]

def build_contract_bundle(requirements: list[dict], source_hash: str,
                          behavior: dict | None, tlr: dict | None = None,
                          model: dict | None = None, assumptions: list[dict] | None = None,
                          behavioral_analysis: dict | None = None) -> dict:
    """Create reviewed-source candidates; no solver or provider is run here."""
    sources = _sources(requirements)
    by_id = {source['id']: source for source in sources}
    normalized = validate_behavior(deepcopy(behavior), list(by_id)) if behavior is not None else None
    context = {key: value for key, value in normalized.items() if key != 'properties'} if normalized else None
    contracts, covered = [], set()
    for prop in (normalized or {}).get('properties', []):
        ids = prop['requirement_ids']
        covered.update(ids)
        contracts.append({'id': 'CONTRACT-' + prop['id'], 'property': deepcopy(prop),
                          'property_sha256': _hash(prop), 'requirement_ids': list(ids),
                          'source_refs': [_source_ref(by_id[rid]) for rid in ids],
                          'rule': _rule(prop['kind']), 'assumption_ids': [],
                          'behavior_assumption_ids': [a['id'] for a in normalized['assumptions']],
                          'owner': {'qualified_name': None, 'status': 'unallocated',
                                    'candidates': _owner_candidates(model, ids)},
                          'sysml': {}, 'checks': [], 'status': 'pending_review',
                          'source_alignment': 'pending', 'review_status': 'pending'})
    scalar_records = _scalar_records(tlr, by_id, covered)
    for record in scalar_records:
        rid, scalar = record['requirement_id'], record['scalar']
        covered.add(rid)
        contracts.append({'id': 'SCALAR-' + _hash(rid)[:16], 'property': None, 'property_sha256': None,
                          'scalar': scalar, 'scalar_sha256': _hash(scalar), 'requirement_ids': [rid],
                          'source_refs': [_source_ref(by_id[rid])], 'rule': _rule('scalar_bound'),
                          'assumption_ids': [], 'behavior_assumption_ids': [],
                          'owner': {'qualified_name': None, 'status': 'unallocated', 'candidates': _owner_candidates(model, [rid])},
                          'sysml': {'existing_elements': _existing_refs(model, [rid])}, 'checks': [],
                          'status': 'pending_review', 'source_alignment': 'pending', 'review_status': 'pending'})
    for source in sources:
        if source['id'] in covered:
            continue
        contracts.append({'id': 'UNRESOLVED-' + _hash(source['id'])[:16], 'property': None,
                          'property_sha256': None, 'requirement_ids': [source['id']],
                          'source_refs': [_source_ref(source)], 'rule': _rule(None),
                          'assumption_ids': [], 'behavior_assumption_ids': [],
                          'owner': {'qualified_name': None, 'status': 'unallocated',
                                    'candidates': _owner_candidates(model, [source['id']])},
                          'sysml': {}, 'checks': [], 'status': 'needs_interpretation',
                          'source_alignment': 'pending', 'review_status': 'pending',
                          'reason': 'No independently stated validated behavior property is linked to this source. Other scalar or domain artifacts may exist; no behavioral coverage is inferred from them.'})
    bundle = {'schema': SCHEMA, 'source_hash': source_hash, 'requirements': sources,
              'requirements_sha256': _hash(sources), 'behavior': normalized,
              'behavior_sha256': _hash(normalized), 'context_sha256': _hash(context),
              'rules': deepcopy(RULES), 'rule_registry': deepcopy(RULE_REGISTRY),
              'scalar_contracts': scalar_records, 'scalar_contracts_sha256': _hash(scalar_records),
              'contracts': contracts,
              'source_alignment': 'pending', 'approval_blocked': True,
              'limitations': list(LIMITATIONS)}
    if assumptions is not None or behavioral_analysis is not None:
        bundle = bind_contract_evidence(bundle, assumptions or [], behavioral_analysis or {}, None)
    return bundle


def behavior_from_contracts(bundle: dict) -> dict | None:
    """Reject divergent property/source mirrors before analyzer consumption.

    Hashes detect accidental or partial mutation; they are not a cryptographic
    authorization mechanism. The server must additionally bind its source ledger.
    """
    if not isinstance(bundle, dict) or bundle.get('schema') != SCHEMA:
        raise ValueError(f'Expected {SCHEMA}.')
    sources = _sources(bundle.get('requirements'))
    if bundle.get('requirements_sha256') != _hash(sources):
        raise ValueError('Contract source snapshot hash differs.')
    by_id = {source['id']: source for source in sources}
    raw = bundle.get('behavior')
    normalized = validate_behavior(deepcopy(raw), list(by_id)) if raw is not None else None
    if raw != normalized or bundle.get('behavior_sha256') != _hash(normalized):
        raise ValueError('Contract behavior must be canonical and match its recorded hash.')
    context = {k: v for k, v in normalized.items() if k != 'properties'} if normalized else None
    if bundle.get('context_sha256') != _hash(context):
        raise ValueError('Contract assumptions/model/scope context differs from its hash.')
    if bundle.get('rules') != RULES or bundle.get('rule_registry') != RULE_REGISTRY:
        raise ValueError('Contract rule registry differs from the supported version.')
    rows = bundle.get('contracts')
    if not isinstance(rows, list):
        raise ValueError('Contract entries must be a list.')
    properties, scalars, seen, covered, unresolved = [], [], set(), set(), set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] in seen:
            raise ValueError('Contract IDs must be unique.')
        seen.add(row['id'])
        ids = row.get('requirement_ids')
        if not isinstance(ids, list) or not ids or any(not isinstance(rid, str) for rid in ids) or len(ids) != len(set(ids)) or any(rid not in by_id for rid in ids):
            raise ValueError('Contract source IDs must refer to the recorded source snapshot.')
        if row.get('source_refs') != [_source_ref(by_id[rid]) for rid in ids]:
            raise ValueError('Contract source references differ from the recorded source.')
        prop = row.get('property')
        if prop is None and row.get('scalar') is not None:
            scalar = row['scalar']
            if len(ids) != 1 or row['id'] != 'SCALAR-' + _hash(ids[0])[:16] or not isinstance(scalar, dict):
                raise ValueError('Scalar contract identity is inconsistent.')
            if row.get('scalar_sha256') != _hash(scalar) or row.get('rule') != _rule('scalar_bound'):
                raise ValueError('Scalar contract content or rule differs.')
            if row.get('property_sha256') is not None:
                raise ValueError('Scalar contracts do not carry a behavior property hash.')
            scalars.append({'requirement_id': ids[0], 'scalar': scalar})
            covered.update(ids)
        elif prop is None:
            if len(ids) != 1 or row['id'] != 'UNRESOLVED-' + _hash(ids[0])[:16] or ids[0] in unresolved:
                raise ValueError('Unresolved contract source identity is inconsistent.')
            unresolved.add(ids[0])
            if row.get('rule') != _rule(None) or row.get('property_sha256') is not None:
                raise ValueError('Unresolved rows cannot carry an executable rule or property hash.')
        else:
            if not isinstance(prop, dict) or row['id'] != 'CONTRACT-' + str(prop.get('id')) or ids != prop.get('requirement_ids'):
                raise ValueError('Contract property/source identity differs.')
            if row.get('property_sha256') != _hash(prop) or row.get('rule') != _rule(prop.get('kind')):
                raise ValueError('Contract property or rule hash differs.')
            properties.append(prop)
            covered.update(ids)
    if scalars != bundle.get('scalar_contracts', []) or bundle.get('scalar_contracts_sha256') != _hash(scalars):
        raise ValueError('Scalar contract mirrors differ from the recorded scalar interpretation.')
    if properties != (normalized or {}).get('properties', []):
        raise ValueError('Contract properties differ from the canonical behavior property list.')
    if covered & unresolved or covered | unresolved != set(by_id):
        raise ValueError('Contract rows must preserve every source without duplicate unresolved coverage.')
    return deepcopy(normalized)


def _evidence_links(record: dict, artifact_hashes: dict) -> list[dict]:
    result = []
    for role, name in record.get('artifacts', {}).items():
        if not isinstance(name, str):
            continue
        supplied = artifact_hashes.get(name)
        if isinstance(supplied, dict):
            supplied = supplied.get('sha256')
        recorded = record.get('query_sha256') if role == 'query' else None
        digest = supplied or recorded
        mismatch = bool(supplied and recorded and supplied != recorded)
        result.append({'role': role, 'artifact': name, 'sha256': digest,
                       'hash_status': 'mismatch' if mismatch else 'recorded' if digest else 'not_supplied',
                       **({'expected_sha256': recorded} if mismatch else {})})
    return result


def bind_contract_evidence(bundle: dict, assumptions: list[dict] | None,
                           behavioral_analysis: dict | None, sysml_index: dict | None, run_dir: Path | None = None) -> dict:
    """Attach evidence references without promoting semantic review status.

    behavioral_analysis.artifact_hashes may supply exact on-disk result hashes.
    Missing hashes are explicit; no serialized approximation is called a file hash.
    """
    behavior_from_contracts(bundle)
    result = deepcopy(bundle)
    analysis = behavioral_analysis or {}
    hashes = dict(analysis.get('artifact_hashes', {}))
    if run_dir is not None:
        for path in Path(run_dir).iterdir():
            if path.is_file() and not path.is_symlink() and path.suffix in {'.json', '.smt2', '.sysml'}:
                hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    scalar_analysis = analysis.get('scalar_analysis', {})
    hashes.update(scalar_analysis.get('artifact_hashes', {}))
    checks = {item.get('id'): item for item in analysis.get('checks', []) if isinstance(item, dict)}
    index = (sysml_index or {}).get('inspection', sysml_index or {})
    indexed = {e.get('qualified_name'): e for e in index.get('elements', []) if isinstance(e, dict) and e.get('qualified_name')}
    for row in result['contracts']:
        ids = set(row['requirement_ids'])
        row['assumption_ids'] = list(dict.fromkeys(a['id'] for a in (assumptions or [])
            if isinstance(a, dict) and a.get('id') and ids & set(a.get('requirement_ids', []))))
        row['checks'] = []
        prop = row['property']
        check = checks.get(prop['id']) if prop else None
        if check and check.get('requirement_ids') == prop['requirement_ids'] and check.get('kind') == prop['kind']:
            # Newer analyzers can bind exact ASTs; a mismatch never becomes evidence.
            recorded_input = analysis.get('contract_input', {})
            expected = recorded_input.get('behavior_sha256', analysis.get('behavior_sha256'))
            context = recorded_input.get('context_sha256', analysis.get('context_sha256'))
            source = recorded_input.get('source_hash')
            if (source and source != bundle['source_hash']) or (expected and expected != bundle['behavior_sha256']) or (context and context != bundle['context_sha256']):
                row['evidence_binding'] = 'rejected_behavior_hash_mismatch'
            else:
                links, seen = [], set()
                records = [('model_feasibility', analysis.get('model_feasibility', {}))]
                records += [(key, check.get(key, {})) for key in ('evidence', 'trigger_reachability', 'overlap_check', 'pending_windows', 'finite_completion_witness')]
                for stage, record in records:
                    if not isinstance(record, dict) or not record:
                        continue
                    linked = _evidence_links(record, hashes)
                    for entry in linked:
                        key = (entry['role'], entry['artifact'])
                        if key not in seen:
                            seen.add(key)
                            links.append({**entry, 'stage': stage, 'verdict': record.get('verdict')})
                row['checks'] = [{'property_id': prop['id'], 'kind': prop['kind'],
                                  'verdict': check.get('verdict', 'unknown'), 'summary': check.get('summary', ''),
                                  'scope': deepcopy(check.get('scope', {})), 'artifacts': links,
                                  'behavior_binding': 'exact_hash' if expected else 'same_run_property_identity_only'}]
                row['evidence_binding'] = 'exact_hash' if expected else 'same_run_property_identity_only'
                if any(link['hash_status'] == 'mismatch' for link in links):
                    row['checks'][0].update(verdict='unknown', summary='Recorded query hash differs from the available artifact; solver verdict is not bound to this file.')
                    row['evidence_binding'] = 'rejected_query_hash_mismatch'
        elif row.get('scalar') and set(row['requirement_ids']) <= set(scalar_analysis.get('checked_ids', [])):
            record = {'artifacts': {'query': 'constraints.smt2', 'result': 'solver_result.json'},
                      'query_sha256': scalar_analysis.get('smt_sha256')}
            row['checks'] = [{'kind': 'scalar_consistency', 'verdict': scalar_analysis.get('solver_status', 'unknown'),
                              'scope': scalar_analysis.get('scope'), 'summary': scalar_analysis.get('summary', ''),
                              'artifacts': _evidence_links(record, hashes), 'behavior_binding': 'same_run_scalar_identity_only'}]
            if any(link['hash_status'] == 'mismatch' for link in row['checks'][0]['artifacts']):
                row['checks'][0].update(verdict='unknown', summary='Scalar query hash differs from the available artifact; solver verdict is not bound to this file.')
                row['evidence_binding'] = 'rejected_query_hash_mismatch'
        elif check:
            row['evidence_binding'] = 'rejected_property_identity_mismatch'
        reference = row.get('sysml', {})
        name = reference.get('qualified_name')
        element = indexed.get(name) if name else None
        if element:
            row['sysml'] = {**reference, **{k: deepcopy(element[k]) for k in ('id', 'qualified_name', 'start_line', 'end_line', 'start_offset', 'end_offset') if k in element},
                            'text_sha256': index.get('text_sha256'), 'binding': 'exact_text_index'}
    return result
