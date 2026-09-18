"""Immutable evidence profile for a requirements formalization run.

No solver/provider calls, filesystem reads, or aggregate confidence score. The
assessment separates syntactic evidence, consistency, finite design checks, and
unestablished engineering meaning. Mutable reviewer decisions are deliberately
excluded so a completion snapshot never masquerades as current acceptance.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import PurePath
from typing import Any

SCHEMA = 'review_quality/1'


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _ids(value: Any) -> set[str]:
    return {item for item in value if isinstance(item, str) and item} if isinstance(value, list) else set()


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def assess_formalization(run: dict) -> dict:
    """Describe only recorded evidence and generation-time review obligations.

    Artifact names link to entries already present in ``run.artifacts``. An
    absent verdict is never filled in from a stage label or an intended result.
    Re-evaluation after reviewer decisions returns the same profile because
    acceptance and decision ledgers belong to separate, live review views.
    """
    if not isinstance(run, dict):
        raise ValueError('A quality assessment requires a run object.')
    artifacts = {item.get('name') for item in _rows(run.get('artifacts')) if isinstance(item.get('name'), str)}

    def evidence(*names):
        return list(dict.fromkeys(name for name in names if name and name in artifacts))

    dimensions = []

    def dimension(identifier, label, status, summary, **details):
        dimensions.append({'id': identifier, 'label': label, 'status': status, 'summary': summary, **details})

    sources = _rows(run.get('requirements'))
    source_counts = Counter(row.get('id') for row in sources if isinstance(row.get('id'), str) and row['id'])
    source_ids = set(source_counts)
    source_total = len(source_ids)
    invalid_source_rows = len(sources) - sum(source_counts.values())
    duplicates = sorted(rid for rid, count in source_counts.items() if count > 1)
    located = {row['id'] for row in sources if row.get('id') in source_ids and isinstance(row.get('text'), str) and row['text'].strip()
               and isinstance(row.get('source'), dict) and (row['source'].get('input_location') or row['source'].get('location'))}
    source_status = 'unavailable' if not source_total else 'incomplete' if invalid_source_rows or duplicates or located != source_ids else 'preserved'
    dimension('source_coverage', 'Source preservation', source_status,
              f'{source_total} distinct source IDs retained; {len(located)} have source text and a recorded location. Source authority remains user supplied.' if source_total else 'No source requirements are available; coverage cannot be established.',
              counts={'requirements': source_total, 'rows': len(sources), 'with_text_and_location': len(located), 'invalid_rows': invalid_source_rows},
              requirement_ids={'retained': sorted(source_ids), 'missing_text_or_location': sorted(source_ids - located), 'duplicate_ids': duplicates},
              evidence=evidence(run.get('input_file'), 'requirements.json', 'prompt_coverage.json'))

    tlr = _dict(run.get('tlr'))
    interpretations = defaultdict(list)
    for row in _rows(tlr.get('requirements')):
        if isinstance(row.get('id'), str):
            interpretations[row['id']].append(row)
    categories = {key: [] for key in ('supported', 'pending_review', 'needs_interpretation', 'missing', 'ambiguous')}
    for rid in sorted(source_ids):
        entries = interpretations[rid]
        if not entries:
            categories['missing'].append(rid)
        elif len(entries) != 1 or rid in duplicates:
            categories['ambiguous'].append(rid)
        else:
            status = entries[0].get('status')
            # An LLM candidate never becomes trusted source meaning by label alone.
            if status == 'supported' and run.get('engine') == 'local' and tlr.get('schema') == 'review_tlr/1':
                categories['supported'].append(rid)
            elif status == 'pending_review' or (status == 'supported' and run.get('engine') == 'pipeline'):
                categories['pending_review'].append(rid)
            else:
                categories['needs_interpretation'].append(rid)
    local_supported, candidates = len(categories['supported']), len(categories['pending_review'])
    unresolved = source_total - local_supported - candidates
    interpretation_status = ('unavailable' if not source_total else 'partial' if unresolved and local_supported + candidates
                             else 'needs_interpretation' if unresolved else 'pending_review' if candidates else 'supported_scope')
    dimension('interpretation_coverage', 'Interpretation coverage', interpretation_status,
              f'{local_supported} source IDs match the local grammar; {candidates} have candidates pending semantic review; {unresolved} need interpretation, an unambiguous record, or a missing artifact. These counts measure representation, not source fidelity.',
              counts={'requirements': source_total, **{key: len(value) for key, value in categories.items()}},
              requirement_ids={**categories, 'unexpected_ids': sorted(set(interpretations) - source_ids)},
              evidence=evidence('interpretation.json', 'requirements_tlr.json', 'pipeline_review_interpretation.json', 'pipeline_model_tlr.json', 'initial_tlr.json'))

    native = _dict(tlr.get('raw')) or tlr
    typecheck = _dict(run.get('native_typecheck')) or _dict(native.get('typecheck'))
    behavior = _dict(run.get('behavior'))
    proposal = _dict(_dict(run.get('behavior_proposal')).get('candidate'))
    candidate = behavior or proposal
    inspection = _dict(run.get('candidate_inspection'))
    inspection_matches = bool(candidate and inspection.get('schema') == 'review_candidate/1' and inspection.get('behavior_sha256') == _hash(candidate))
    symbols = _rows(tlr.get('symbols') or native.get('symbol_table'))
    typed_symbols = {row['name'] for row in symbols if isinstance(row.get('name'), str) and row.get('type') in {'Bool', 'Int', 'Real'}}
    if typecheck.get('ok') is False or (inspection and candidate and not inspection_matches):
        type_status = 'failed'
    elif (typecheck.get('ok') is True and not typecheck.get('skipped')) or inspection_matches:
        type_status = 'validated_scope'
    elif typed_symbols:
        type_status = 'recorded_structure'
    else:
        type_status = 'unavailable'
    dimension('types_units', 'Types and units', type_status,
              'Recorded native type checks and candidate schema/unit validation cover their respective encodings. Declared types alone do not establish physical correctness or source meaning.',
              counts={'declared_typed_symbols': len(typed_symbols), 'candidate_variables': len(_rows(candidate.get('variables')))},
              details={'native_typecheck': deepcopy(typecheck) or None, 'candidate_inspection_matches': inspection_matches,
                       'native_typecheck_was_run': bool(typecheck and not typecheck.get('skipped')),
                       'local_grammar_scope': local_supported > 0,
                       'note': 'A missing native typecheck record is not reported as a pass.'},
              evidence=evidence('requirements_tlr.json', 'pipeline_model_tlr.json', 'initial_tlr.json', 'candidate_inspection.json'))

    analysis = _dict(run.get('analysis'))
    raw_verdict = analysis.get('solver_status')
    verdict = raw_verdict if raw_verdict in {'sat', 'unsat', 'unknown', 'not_run'} else 'unknown' if analysis else 'not_run'
    checked = _ids(analysis.get('checked_ids')) & source_ids
    consistency_summary = {
        'sat': 'The encoded constraint set has a satisfying assignment under its premises. This establishes consistency of that encoding, not requirement fidelity or design compliance.',
        'unsat': 'The encoded constraints are inconsistent under their premises. A conflicting subset does not identify which source requirement should change.',
        'unknown': 'No conclusive solver verdict establishes consistency or inconsistency of the encoded constraint set.',
        'not_run': 'Requirement consistency was not checked; missing evidence is not a pass.',
    }[verdict]
    dimension('consistency', 'Encoded requirement consistency', verdict, consistency_summary,
              counts={'requirements': source_total, 'associated_source_ids': len(checked)},
              requirement_ids={'associated': sorted(checked), 'without_check_association': sorted(source_ids - checked)},
              details={'reported_status': analysis.get('status'), 'solver_status': raw_verdict, 'scope': analysis.get('scope'),
                       'unsat_core': deepcopy(analysis.get('unsat_core') or [])},
              evidence=evidence('analysis.json', 'constraints.smt2', 'solver_result.json', 'pipeline_model_sat.smt2', 'pipeline_recheck.json'))

    semantic = _dict(analysis.get('pipeline_semantic_checks'))
    checks = _dict(semantic.get('checks'))
    probe_rows = []
    for name, check in checks.items():
        check = _dict(check)
        omitted = bool(check.get('skipped') or check.get('skipped_administrative_guards'))
        status = 'failed' if check.get('passed') is False else 'partial' if omitted else 'passed' if check.get('passed') is True else 'unknown'
        probe_rows.append({'name': name, 'status': status, 'skipped': omitted,
                           'solver_error_count': len(check.get('solver_errors', [])) if isinstance(check.get('solver_errors'), list) else 0})
    probe_status = ('not_run' if not semantic else 'failed' if semantic.get('passed') is False
                    else 'findings' if any(p['status'] == 'failed' for p in probe_rows)
                    else 'partial' if any(p['status'] in {'partial', 'unknown'} for p in probe_rows)
                    else 'passed_scope' if semantic.get('passed') is True and probe_rows else 'unknown')
    dimension('semantic_probes', 'Encoding diagnostics', probe_status,
              'Named-assertion coverage, shared-state checks, pairwise conflicts, vacuity, and symbol diagnostics are separate from whole-formula consistency. Their success does not measure agreement with stakeholder intent.' if semantic else 'No legacy encoding diagnostics are recorded for this engine/run. This does not invalidate separately recorded consistency evidence.',
              details={'generation_gate_passed': semantic.get('passed'), 'probes': probe_rows,
                       'limitations': deepcopy(analysis.get('semantic_limitations') or []), 'generation_blocker': analysis.get('generation_blocker')},
              evidence=evidence('pipeline_model_semantic_checks.json', 'analysis.json'))

    behavioral = _dict(run.get('behavioral_analysis'))
    feasibility = _dict(behavioral.get('model_feasibility')).get('verdict', 'not_run')
    feasibility = feasibility if feasibility in {'sat', 'unsat', 'unknown', 'not_run'} else 'unknown'
    by_check = defaultdict(list)
    for check in _rows(behavioral.get('checks')):
        if isinstance(check.get('id'), str):
            by_check[check['id']].append(check)
    property_verdicts = {pid: records[0].get('verdict', 'unknown') if len(records) == 1 else 'unknown' for pid, records in by_check.items()}
    verdict_counts = dict(sorted(Counter(property_verdicts.values()).items()))
    expected_properties = {row['id'] for row in _rows(behavior.get('properties')) if isinstance(row.get('id'), str)}
    missing_properties = sorted(expected_properties - set(property_verdicts))
    if not behavior:
        behavior_status = 'pending_review' if proposal or behavioral.get('status') == 'pending_review' else 'not_run'
    elif feasibility == 'unsat':
        behavior_status = 'infeasible'
    elif feasibility != 'sat':
        behavior_status = 'not_run' if feasibility == 'not_run' and not property_verdicts else 'unknown'
    elif 'counterexample' in property_verdicts.values():
        behavior_status = 'counterexample'
    elif property_verdicts and not missing_properties and set(property_verdicts.values()) == {'bounded_pass'}:
        behavior_status = 'bounded_pass'
    elif property_verdicts and not missing_properties and set(property_verdicts.values()) == {'unproved'}:
        behavior_status = 'unproved'
    else:
        behavior_status = 'partial'
    dimension('behavior', 'Separate design behavior', behavior_status,
              'The candidate remains unchecked pending explicit design review.' if behavior_status == 'pending_review'
              else 'No separate design behavior was checked. Scalar consistency does not establish temporal behavior.' if behavior_status == 'not_run'
              else 'Results concern the supplied candidate and full finite-horizon executions only. Feasibility is separate from violation search; unbounded liveness and architectural equivalence remain unestablished.',
              counts={'candidate_properties': len(expected_properties), 'recorded_properties': len(property_verdicts), 'verdicts': verdict_counts},
              details={'model_feasibility': feasibility, 'reported_status': behavioral.get('status'),
                       'scope': deepcopy(behavioral.get('scope') or {}), 'missing_property_results': missing_properties,
                       'property_verdicts': property_verdicts},
              evidence=evidence('behavior.json', 'design_proposal.json', 'behavioral_analysis.json', 'behavior_analysis.json', 'design_review.json'))

    compilation = _dict(run.get('compilation'))
    model = _dict(run.get('model'))
    compile_status = compilation.get('status')
    compile_status = compile_status if compile_status in {'passed', 'failed', 'unavailable', 'not_run', 'unknown'} else 'unknown' if compilation else 'not_run'
    if compile_status == 'passed' and not model:
        compile_status = 'unknown'
    trace_status = _dict(compilation.get('trace_compilation')).get('status')
    if compile_status == 'passed' and trace_status and trace_status != 'passed':
        compile_status = 'failed' if trace_status == 'failed' else 'unknown'
    model_name = PurePath(model.get('path') or 'model.sysml').name
    dimension('compilation', 'SysML compilation', compile_status,
              'Compilation checks syntax, references, and well-formedness of the generated artifact. It does not validate stakeholder meaning or prove that the architecture implements the checked dynamics.',
              details={'reported_status': compilation.get('status'), 'trace_status': trace_status, 'model_sha256': compilation.get('model_sha256')},
              evidence=evidence('compilation.json', model_name, 'model_with_trace.sysml'))

    diagnostics = _rows(inspection.get('diagnostics'))
    provenance = _dict(inspection.get('provenance'))
    origins = dict(sorted(Counter(_dict(record).get('origin', 'unspecified') for record in provenance.values()).items()))
    provenance_status = 'not_applicable' if not candidate else 'unavailable' if not inspection else 'stale' if not inspection_matches else 'findings' if diagnostics else 'recorded'
    dimension('provenance', 'Candidate provenance and modeling diagnostics', provenance_status,
              'Origins, source IDs, and rationales are recorded claims bound to expressions. Missing next-state references and repeated guarantees prompt review; these syntactic checks neither repair the model nor prove completeness.',
              counts={'expressions': len(provenance), 'diagnostics': len(diagnostics), 'origins': origins},
              details={'diagnostics': deepcopy(diagnostics), 'candidate_inspection_matches': inspection_matches},
              evidence=evidence('candidate_inspection.json', 'candidate_provenance.json'))

    assumptions = {row['id'] for row in _rows(run.get('assumptions')) if isinstance(row.get('id'), str)}
    contracts = {row['id']: row for row in _rows(_dict(run.get('contracts')).get('contracts')) if isinstance(row.get('id'), str)}
    bindings_required = bool(behavior and run.get('review_workflow_version', 0) >= 2)
    binding_variables = {row['name'] for row in _rows(behavior.get('variables')) if isinstance(row.get('name'), str) and row.get('role') != 'parameter'}
    binding_contracts = {cid for cid, row in contracts.items() if row.get('property') or row.get('scalar')}
    dimension('review_obligations', 'Engineering review obligations at generation', 'pending_at_generation' if source_total else 'unavailable',
              'Every generated interpretation remains subject to engineering review. These generation-time counts do not change when decisions are recorded; consult the live review ledger for current acceptance and pending decisions.',
              counts={'source_interpretations': source_total, 'assumptions': len(assumptions), 'contracts': len(contracts),
                      'required_variable_bindings': len(binding_variables) if bindings_required else 0,
                      'required_contract_bindings': len(binding_contracts) if bindings_required else 0,
                      'revision_comparison': 1 if run.get('parent_run_id') else 0},
              details={'snapshot_only': True, 'source_fidelity': 'unestablished', 'architecture_behavior_equivalence': 'unestablished'},
              evidence=evidence('assumptions.json', 'contracts.json', 'contract_changes.json', 'correction_summary.json'))

    integrity = _dict(run.get('integrity'))
    integrity_status = integrity.get('status')
    integrity_status = integrity_status if integrity_status in {'passed', 'failed'} else 'unknown' if integrity else 'not_run'
    dimension('integrity', 'Artifact and snapshot integrity', integrity_status,
              'Recorded hashes bind producer snapshots, downloaded artifacts, and analysis results. Matching hashes establish artifact identity, not semantic correctness.',
              details={'issues': deepcopy(integrity.get('issues') or [])},
              evidence=evidence('source.txt', 'source.csv', 'source.json', 'analysis_selection.json', 'toolchain.json'))

    return {'schema': SCHEMA, 'snapshot': 'analysis_completion', 'source_hash': run.get('source_hash'),
            'engine': run.get('engine'), 'analysis_mode': run.get('analysis_mode', 'requirements'),
            'summary': 'An evidence profile across separate dimensions; no aggregate score or probability of semantic correctness is assigned.',
            'dimensions': dimensions,
            'review_scope': 'Generation-time obligations only. Current reviewer decisions and acceptance belong to the live review ledger.',
            'limitations': [
                'Source fidelity is unestablished: solver and compiler success cannot establish that generated expressions capture stakeholder intent.',
                'Source coverage measures retained IDs and available interpretations, not completeness of document extraction or semantic translation.',
                'A satisfying constraint set can still formalize the wrong meaning; a contradictory set does not identify the authoritative correction.',
                'Bounded behavior evidence applies to the separately supplied candidate, not automatically to the generated domain architecture or physical system.',
                'Finite checks exclude shorter nonextendable executions and do not establish deadlock freedom, continuous-time behavior, or unbounded liveness.',
                'Provenance and reviewed architecture bindings are engineering records; they do not prove source derivation or behavior implementation.',
            ]}
