"""Reviewed associations from behavior/contracts to exact domain SysML declarations.

This is a conservative text-index association ledger. It does not parse arbitrary
SysML, establish an implementation relation, or alter generated model evidence.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import re
from typing import Any

from review_behavior import _unit
from review_contracts import _hash
from review_model_index import build_model_inspection, _tokenize

SCHEMA = 'review_architecture_binding/1'
SCOPE = ('Reviewed association to declarations in the exact generated SysML text; '
         'not proof of behavior implementation, semantic equivalence, or requirement satisfaction.')
_LEDGER = 'architecture_binding_reviews'
_SCALARS = {'Boolean': 'Bool', 'Integer': 'Int', 'Real': 'Real', 'String': 'String'}
_PHYSICAL = {
    'DurationValue': 's', 'LengthValue': 'm', 'MassValue': 'kg',
    'ThermodynamicTemperatureValue': 'K', 'ElectricCurrentValue': 'A',
    'ElectricPotentialValue': 'V', 'PowerValue': 'W', 'EnergyValue': 'J',
}


def _model_hash(run: dict) -> str:
    return hashlib.sha256(str((run.get('model') or {}).get('text') or '').encode('utf-8')).hexdigest()


def _metadata(element: dict, text: str) -> dict:
    """Recognize standard types and local unit documentation, not inferred names."""
    if element['kind'] != 'attribute':
        return {'type': None, 'unit': None, 'unit_factor': None,
                'metadata_basis': 'container_declaration', 'metadata_status': 'not_a_value'}
    declared = element.get('type_name') or ''
    simple = declared.removeprefix('ScalarValues::')
    typ = _SCALARS.get(simple)
    physical = _PHYSICAL.get(declared.removeprefix('ISQ::')) if declared.startswith('ISQ::') else None
    if physical:
        typ = 'Real'
    _, _, comments = _tokenize(text[element['start_offset']:element['end_offset']])
    # Match documented unit fields only; never infer units from variable names,
    # requirements, generator metadata, or unrelated declarations.
    documented = []
    for comment in comments:
        for match in re.finditer(r'(?:^|[;\n]|/\*)\s*(?:canonical\s+)?unit\s*:\s*([^;\n*]+)', comment.text, re.I):
            unit = match.group(1).strip()
            if unit == 'dimensionless (unit 1)':
                unit = '1'
            documented.append(unit)
    normalized = []
    unknown = []
    for raw in documented:
        try:
            unit, factor = _unit(raw)
            normalized.append((unit, str(factor)))
        except ValueError:
            unknown.append(raw)
    conflicts = len(set(normalized)) > 1 or len(set(unknown)) > 1 or bool(unknown and normalized)
    if physical and normalized and any(unit != physical for unit, _ in normalized):
        conflicts = True
    if typ == 'Bool' and documented:
        conflicts = True
    unit, factor = (normalized[0] if normalized else (physical, '1') if physical else ('1', '1') if typ == 'Bool' else (None, None))
    return {'type': typ, 'unit': unit, 'unit_factor': factor,
            'documented_units': documented,
            'metadata_basis': 'recognized_standard_type_and_local_unit_documentation',
            'metadata_status': 'conflicting' if conflicts else 'unresolved' if unknown or typ is None or unit is None else 'recognized',
            'metadata_note': 'Unit comments are declared metadata; this index does not independently type-check SysML.'}


def binding_choices(run: dict) -> list[dict]:
    """Return selectable real declarations, reindexed from exact model text.

    Duplicate qualified names and diagnostic projections are not bindable. The
    returned ID plus model hash identifies one declaration, not an instance path
    inherited through a part/port type. Containment must be lexically explicit.
    """
    model = run.get('model') or {}
    text = str(model.get('text') or '')
    index = build_model_inspection(text, run.get('requirements') or [], run.get('tlr'))
    elements = index['elements']
    by_id = {e['id']: e for e in elements}
    counts = Counter(e.get('qualified_name') for e in elements if e.get('qualified_name'))

    def excluded(e: dict) -> bool:
        while e:
            if e.get('name', '').startswith('ContractReview_') or e['kind'] in {'requirement', 'constraint', 'subject'}:
                return True
            # Evidence/trace artifacts are not domain architecture owners.
            if ((e['kind'] == 'package' and e.get('name', '').lower().endswith(('_trace', '_evidence')))
                    or e.get('name') in {'SolverEvidence', 'VerificationEvidence', 'TraceabilityMatrix'}
                    or (e.get('type_name') or '').split('::')[-1] in {'SolverEvidence', 'VerificationEvidence', 'TraceabilityMatrix'}):
                return True
            if counts[e.get('qualified_name')] > 1:
                return True
            e = by_id.get(e.get('parent_id'))
        return False

    choices = []
    for entry in elements:
        if entry['kind'] not in {'part', 'port', 'attribute'} or excluded(entry) or (entry['kind'] == 'attribute' and entry.get('definition')):
            continue
        if not entry.get('qualified_name') or entry.get('role') != 'declaration':
            continue
        choice = {key: deepcopy(entry.get(key)) for key in (
            'id', 'qualified_name', 'name', 'kind', 'type_name', 'parent_id',
            'children_ids', 'definition', 'start_line', 'end_line')}
        choice.update(_metadata(entry, text), model_sha256=index['text_sha256'], scope=SCOPE)
        choices.append(choice)
    # Value choices are lexical descendants only. Inherited or redefined paths
    # need a proper SysML resolver and are intentionally not invented here.
    choices_by_id = {e['id']: e for e in choices}
    for entry in choices:
        values = []
        if entry['kind'] in {'part', 'port'}:
            for candidate in choices:
                if candidate['kind'] != 'attribute':
                    continue
                parent = choices_by_id.get(candidate.get('parent_id'))
                while parent:
                    if parent['id'] == entry['id']:
                        values.append(candidate['id'])
                        break
                    parent = choices_by_id.get(parent.get('parent_id'))
        entry['value_element_ids'] = values
    return choices


def binding_targets(run: dict) -> list[dict]:
    behavior = run.get('behavior') or {}
    result = [{'target_kind': 'variable', 'target_id': v['name'], 'label': v['name'],
               'type': v['type'], 'unit': v.get('unit', '1'), 'role': v['role'],
               'target_sha256': _hash(v), 'required': v['role'] != 'parameter'}
              for v in behavior.get('variables', [])]
    for contract in (run.get('contracts') or {}).get('contracts', []):
        content = contract.get('property') or contract.get('scalar')
        scalar = contract.get('scalar') or {}
        result.append({'target_kind': 'contract', 'target_id': contract['id'],
                       'label': contract['id'], 'type': scalar.get('type'), 'unit': scalar.get('unit'),
                       'target_sha256': _hash({'requirement_ids': contract.get('requirement_ids'),
                                               'property': contract.get('property'), 'scalar': contract.get('scalar')}),
                       'required': bool(content), 'executable': bool(content),
                       'requirement_ids': deepcopy(contract.get('requirement_ids', []))})
    return result


def _compatibility(target: dict, element: dict) -> dict:
    if target['target_kind'] == 'contract' and (element['kind'] != 'attribute' or not target.get('type')):
        return {'status': 'association_only', 'issues': [],
                'detail': 'Contract ownership association only; no type equivalence or implementation proof is asserted.'}
    issues, unknown = [], []
    if element.get('metadata_status') == 'conflicting':
        issues.append('SysML type and unit declarations contain conflicting metadata.')
    typ = element.get('type')
    if typ is None:
        unknown.append('SysML value type is not recognized by the conservative index.')
    elif typ != target['type']:
        issues.append(f"Behavior type {target['type']} differs from SysML type {typ}; no coercion is inferred.")
    if target['type'] == 'Bool' and element.get('unit') not in {None, '1'}:
        issues.append('A Boolean variable cannot bind to a value with a physical unit.')
    if target['type'] != 'Bool':
        if element.get('unit') is None or element.get('metadata_status') == 'unresolved':
            unknown.append('SysML value unit is unestablished or unsupported.')
        else:
            unit, factor = _unit(target.get('unit'))
            if element['unit'] != unit:
                issues.append(f"Behavior unit {unit} differs from SysML unit {element['unit']}.")
            elif element.get('unit_factor') != str(factor):
                issues.append('Unit scales differ; an explicit value conversion is required before binding.')
    return {'status': 'incompatible' if issues else 'unresolved' if unknown else 'recognized_compatible',
            'issues': issues + unknown,
            'detail': 'Recognized declaration compatibility is an association check, not a SysML compiler or behavior equivalence proof.'}


def validate_binding(run: dict, payload: dict) -> dict:
    """Validate one review decision and return append-only canonical record data."""
    if not isinstance(payload, dict):
        raise ValueError('Architecture binding review must be an object.')
    for key in ('source_hash', 'evidence_hash'):
        if not run.get(key) or payload.get(key) != run.get(key):
            raise ValueError(f'Architecture binding {key} is missing or stale; reload this run.')
    decision = payload.get('decision')
    if decision not in {'accept', 'reject', 'defer'}:
        raise ValueError('Binding decision must be accept, reject, or defer.')
    for key, limit in [('reviewer', 200), ('rationale', 6000)]:
        if not isinstance(payload.get(key), str) or not payload[key].strip() or len(payload[key]) > limit:
            raise ValueError(f'Provide a nonempty {key} of at most {limit} characters.')
    if not isinstance(payload.get('acknowledge_unresolved_compatibility', False), bool):
        raise ValueError('Unresolved compatibility acknowledgment must be Boolean.')
    matches = [t for t in binding_targets(run) if t['target_kind'] == payload.get('target_kind') and t['target_id'] == payload.get('target_id')]
    if len(matches) != 1:
        raise ValueError('Binding target is missing or ambiguous in the current candidate.')
    target = matches[0]
    fingerprints = {'model_sha256': _model_hash(run), 'behavior_sha256': _hash(run.get('behavior')),
                    'target_sha256': target['target_sha256']}
    for key, value in fingerprints.items():
        if key in payload and payload[key] != value:
            raise ValueError(f'Architecture binding {key} is stale; reload this run.')
    choices = {e['id']: e for e in binding_choices(run)}
    element = choices.get(payload.get('element_id'))
    if element is None:
        raise ValueError('Choose an unambiguous domain part, port, or attribute from the exact current model; diagnostic projections are excluded.')
    value_id = payload.get('value_element_id') or None
    value_element = element
    if target['target_kind'] == 'variable' and element['kind'] != 'attribute':
        if value_id not in element['value_element_ids']:
            raise ValueError('A variable bound to a part or port must identify a lexically contained value attribute; inherited paths are not resolved.')
        value_element = choices[value_id]
    elif value_id and value_id != element['id']:
        raise ValueError('A separate value attribute is only used when a variable is bound through a part or port.')
    compatibility = _compatibility(target, value_element)
    acknowledged = payload.get('acknowledge_unresolved_compatibility', False)
    if decision == 'accept' and compatibility['status'] == 'incompatible':
        raise ValueError('Incompatible architecture binding: ' + ' '.join(compatibility['issues']))
    if decision == 'accept' and compatibility['status'] == 'unresolved' and not acknowledged:
        raise ValueError('Compatibility is unresolved: ' + ' '.join(compatibility['issues']) + ' Explicit reviewer acknowledgment is required; this does not validate the missing type or unit.')
    return {'schema': SCHEMA, 'source_hash': run['source_hash'], 'evidence_hash': run['evidence_hash'],
            **fingerprints, 'target_kind': target['target_kind'], 'target_id': target['target_id'],
            'element_id': element['id'], 'qualified_name': element['qualified_name'], 'element_kind': element['kind'],
            'value_element_id': value_id, 'value_qualified_name': value_element['qualified_name'] if target['target_kind'] == 'variable' else None,
            'decision': decision, 'reviewer': payload['reviewer'].strip(), 'rationale': payload['rationale'].strip(),
            'compatibility': compatibility, 'acknowledge_unresolved_compatibility': acknowledged, 'scope': SCOPE}


def binding_review_hash(records: list[dict]) -> str:
    return _hash(records)


def binding_status(run: dict) -> dict:
    """Current per-target status; a later reject/defer always revokes acceptance."""
    latest = {(r.get('target_kind'), r.get('target_id')): r for r in run.get(_LEDGER, []) if isinstance(r, dict)}
    targets = binding_targets(run)
    blockers = []
    for target in targets:
        record = latest.get((target['target_kind'], target['target_id']))
        status, issue = 'pending', None
        if record:
            try:
                canonical = validate_binding(run, record)
                # Every stored review must carry the original exact fingerprints.
                if any(record.get(key) != canonical[key] for key in ('model_sha256', 'behavior_sha256', 'target_sha256')):
                    raise ValueError('Binding review does not carry current exact fingerprints.')
                status = record['decision']
            except ValueError as error:
                status, issue = 'stale', str(error)
        target.update(review_status=status, latest_review=deepcopy(record), issue=issue)
        if target['required'] and status != 'accept':
            blockers.append(f"{target['target_kind'].capitalize()} {target['target_id']} needs a current accepted architecture binding ({status}).")
    return {'schema': 'review_architecture_bindings/1', 'model_sha256': _model_hash(run),
            'behavior_sha256': _hash(run.get('behavior')), 'targets': targets,
            'choices': binding_choices(run), 'blockers': blockers,
            'review_hash': binding_review_hash(run.get(_LEDGER, [])), 'scope': SCOPE}


def binding_blockers(run: dict) -> list[str]:
    return binding_status(run)['blockers']
