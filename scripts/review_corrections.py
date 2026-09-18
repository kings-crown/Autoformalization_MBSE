"""Explain proposed revisions without authorizing changes or altering solver evidence."""
from copy import deepcopy

from review_contracts import _hash


def build_correction_summary(parent: dict | None, run: dict, comparisons: dict | None) -> dict:
    result = {'schema': 'review_corrections/1', 'parent_run_id': (parent or {}).get('id'),
              'source_hash': run.get('source_hash'), 'parent_source_hash': (parent or {}).get('source_hash'),
              'parent_evidence_hash': (parent or {}).get('evidence_hash'), 'changes': [],
              'scope': 'Proposed candidate changes for engineering review; no source amendment is authorized.'}
    rows = result['changes']

    def add(category, subject, before, after, explanation, **extra):
        if before == after:
            return
        rows.append({'category': category, 'subject': subject, 'before': deepcopy(before),
                     'after': deepcopy(after), 'explanation': explanation, **deepcopy(extra)})

    if parent:
        old_sources = {r['id']: r for r in parent.get('requirements', [])}
        new_sources = {r['id']: r for r in run.get('requirements', [])}
        for rid in sorted(old_sources.keys() | new_sources.keys()):
            add('source', rid, old_sources.get(rid), new_sources.get(rid),
                'Source wording or its recorded context changed. Confirm the source, operating context, and authority; solver results do not authorize this amendment.')
        old = parent.get('behavior') or (parent.get('behavior_proposal') or {}).get('candidate') or {}
        new = run.get('behavior') or (run.get('behavior_proposal') or {}).get('candidate') or {}
        for key in ('initial', 'transitions'):
            add('design', key, old.get(key), new.get(key),
                'Candidate behavior changed. Recheck the unchanged obligations against these dynamics; a passing result does not establish that the physical design implements them.')
        for key in ('horizon', 'step'):
            add('scope', key, old.get(key), new.get(key),
                'The finite analysis scope changed. Results must be read with the new time scale and horizon.')
        add('scope', 'analysis mode', parent.get('analysis_mode', 'requirements'), run.get('analysis_mode', 'requirements'),
            'The selected analysis changed. Requirements consistency, unchecked proposals, and reviewed design checks support different conclusions.')
        old_vars = {v['name']: v for v in old.get('variables', [])}
        new_vars = {v['name']: v for v in new.get('variables', [])}
        for name in sorted(old_vars.keys() | new_vars.keys()):
            a, b = old_vars.get(name), new_vars.get(name)
            add('design', 'variable ' + name,
                {k: v for k, v in a.items() if k != 'bounds'} if a else None,
                {k: v for k, v in b.items() if k != 'bounds'} if b else None,
                'A variable declaration or fixed design parameter changed. Check identity, role, type, units, and architectural bindings.')
            add('environment', 'bounds for ' + name, (a or {}).get('bounds'), (b or {}).get('bounds'),
                'Admissible values changed. Narrower bounds can hide counterexamples; justify the domain independently of the guarantee.')
        add('environment', 'assumptions', old.get('assumptions', []), new.get('assumptions', []),
            'Assumed scenario or domain premises changed. Inspect whether each is an environmental claim or design restriction; a passing result under stronger assumptions is conditional on their justification.')
        old_props = {p['id']: p for p in old.get('properties', [])}
        new_props = {p['id']: p for p in new.get('properties', [])}
        comparison_rows = (comparisons or {}).get('changes', [])
        for pid in sorted(old_props.keys() | new_props.keys()):
            cid = 'CONTRACT-' + pid
            evidence = next((c for c in comparison_rows if c.get('contract_id') == cid), None)
            add('interpretation', pid, old_props.get(pid), new_props.get(pid),
                (evidence or {}).get('summary') or 'The formal obligation changed. No comparable semantic result is available; inspect the source and both interpretations.',
                contract_id=cid, status=(evidence or {}).get('status', 'not_comparable'), evidence=evidence)
        # Scalar obligations may change without any behavior candidate.
        for comparison in comparison_rows:
            cid = comparison.get('contract_id', '')
            if not cid.startswith('SCALAR-') or comparison.get('status') == 'unchanged':
                continue
            old_contract = next((c for c in (parent.get('contracts') or {}).get('contracts', []) if c['id'] == cid), {})
            new_contract = next((c for c in (run.get('contracts') or {}).get('contracts', []) if c['id'] == cid), {})
            add('interpretation', cid, old_contract.get('scalar'), new_contract.get('scalar'),
                comparison.get('summary', 'Inspect the revised scalar obligation.'), contract_id=cid,
                status=comparison.get('status', 'not_comparable'), evidence=comparison)
        def claimed_provenance(candidate):
            return {pointer: {k: v for k, v in entry.items() if k != 'expression_sha256'}
                    for pointer, entry in (candidate.get('candidate_provenance') or {}).items()
                    if entry.get('origin') != 'unspecified' or entry.get('requirement_ids') or entry.get('rationale')}
        add('provenance', 'equation provenance', claimed_provenance(parent), claimed_provenance(run),
            'The claimed origin or rationale of candidate expressions changed. These are reviewer declarations, not automatically verified source support.')
    result['summary'] = (f'{len(rows)} proposed changes grouped by source, interpretation, design, environment, scope, and provenance.'
                         if parent else 'Initial candidate; there is no previous revision to compare.')
    result['sha256'] = _hash(result)
    return result
