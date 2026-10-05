"""Typed capability recovery patterns with explicit source bindings.

Applicability and source meaning are LLM claims; deterministic expansion
produces a candidate, not evidence of semantic fidelity.
"""
from copy import deepcopy
import re
from mutation_core import STATIC_MAX_VARIABLES

POLICY_VERSION = 'explicit_abstention_patterns/1'
INSTRUCTIONS = '''For every currently unsupported/unresolved requirement, add exactly one abstention_diagnostics entry to the proposal envelope:
{"id":source_ID,"decision":"capability_candidate"|"repair_other"|"blocked","reason":specific_explanation,"source_basis":[{"source_id":source_ID,"quote":literal_own_requirement_quote}],"realization_details":[nonblocking_implementation_details],"missing_slots":[missing_source_meanings],"unsupported_operators":[required_unavailable_operators],"alternatives":[{"interpretation":meaning,"obligation_difference":material_difference}]}.
A blocked decision needs at least one concrete missing semantic slot, unavailable operator, or two distinct interpretations with materially different obligations. Unspecified implementation, algorithm, address realization, scheduling, concurrency, or preparation questions alone cannot justify blocking a pure capability: explain why the detail changes the required meaning, otherwise list it only in realization_details. Source-supported concurrency qualifications must remain in operation/scope; actual ordering/history cannot be turned into availability. repair_other means actually propose a supported state_constraint or event_relation, not merely recommend another future review.
For capability_candidate, instantiate the ENTIRE requirement at capability level and add one capability_bindings entry:
{"id":source_ID,"subject":named_subject,"operation":named_operation,"scope":complete_source_scope,"symbol":Boolean_identifier,"meaning":complete_availability_definition,"limitations":[explicit_limits],"whole_obligation":true,"residual_obligations":[],"source_basis":[{"source_id":source_ID,"quote":literal_own_requirement_quote}]}.
This is not permission to project one conjunct, remove a guard, or turn actual behavior into an availability flag. If residual obligations require different semantics, use repair_other or blocked. For capability_candidate, missing_slots, unsupported_operators and alternatives must be empty. The controller expands the binding into an unbounded Bool variable (description=meaning), formula {"var":symbol}, and capability metadata. Leave that requirement's original withheld row in tlr (or provide the exact corresponding expansion); do not invent a second formula. Mark its reviews outcome changed, quoting the original source and explaining the interpretation. A new variable's context review is deterministically recorded from the binding; do not needlessly duplicate it. Existing symbol definitions and fixed context cannot be overwritten by a binding. Use a fresh descriptive symbol if required, within the 24-symbol profile. A symbol starts with an ASCII letter, contains only ASCII letters, digits or underscores, and has at most 48 characters. For repair_other/blocked do not supply a capability binding. Include capability_bindings:[] when none apply.
If a frozen obligation inventory is supplied, include explicit coverage on the proposed TLR row for the expanded capability, even when leaving the original withheld placeholder for deterministic compilation. Its formula_path and operation slot must point to the expanded /formula. The controller preserves these supplied mappings without inventing them, copying old mappings, or granting coverage; structural coverage and independent component review still apply.
Every applicable capability must therefore receive an actual candidate expansion in this same bounded proposal, before an unchanged proposal can stop recovery. These structured diagnoses/bindings are recorded claims, not automatic acceptance. Source-to-rule review and compilation still gate every changed candidate; no judge answers or held-out reference formulas are available.'''
INSTRUCTIONS = INSTRUCTIONS.replace('24-symbol profile', f'{STATIC_MAX_VARIABLES}-symbol profile')


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError(f'{label} requires nonempty text of at most 4000 characters')
    return value


def _keys(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f'{label} requires exactly {sorted(fields)}')


def _texts(value, label):
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError(f'{label} needs a list of at most 20 entries')
    for item in value:
        _text(item, label)


def _basis(value, rid, sources):
    from canonical_feedback import source_quote_index
    index = source_quote_index(sources)
    own = next(r['text'] for r in sources if r['id'] == rid)
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise ValueError('Pattern source_basis needs literal source quotations')
    own_quote = False
    for item in value:
        _keys(item, {'source_id', 'quote'}, 'Pattern source basis')
        sid, quote = item['source_id'], _text(item['quote'], 'Pattern quote')
        if not isinstance(sid, str) or sid not in index or not any(quote in s for s in index[sid]):
            raise ValueError('Pattern quote must match literal source/context text')
        own_quote |= sid == rid and quote in own
    if not own_quote:
        raise ValueError('Pattern diagnosis/binding requires its own source text quote')


def compile_capability(binding, source_row):
    """Compile validated explicit slots; does not infer or certify applicability."""
    fields = {'id', 'subject', 'operation', 'scope', 'symbol', 'meaning', 'limitations',
              'whole_obligation', 'residual_obligations', 'source_basis'}
    _keys(binding, fields, 'Capability binding')
    if binding['id'] != source_row['id']:
        raise ValueError('Capability binding must identify its exact source row')
    for key in ('subject', 'operation', 'scope', 'symbol', 'meaning'):
        _text(binding[key], 'Capability ' + key)
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,47}', binding['symbol']):
        raise ValueError('Capability symbol must be a safe descriptive identifier of at most 48 characters')
    _texts(binding['limitations'], 'Capability limitations')
    if not binding['limitations']:
        raise ValueError('Capability limitations must disclose the abstraction boundary')
    if binding['whole_obligation'] is not True or binding['residual_obligations'] != []:
        raise ValueError('Capability pattern cannot silently project a compound obligation')
    variable = {'name': binding['symbol'], 'type': 'Bool', 'description': binding['meaning']}
    row = {k: deepcopy(v) for k, v in source_row.items() if k in {'id', 'text', 'source'}}
    row.update(status='supported', formula={'var': binding['symbol']}, abstraction={
        'kind': 'capability', **{k: deepcopy(binding[k]) for k in
                               ('subject', 'operation', 'scope', 'symbol', 'meaning', 'limitations')}})
    return variable, row


def expand_proposal(raw, sources, before, *, required=False, context_reviews=False):
    """Record each withheld diagnosis and expand applicable patterns atomically.

    When required is false, proposals without pattern fields pass through.
    Supplying pattern fields always activates full validation.
    """
    supplied = isinstance(raw, dict) and any(k in raw for k in ('abstention_diagnostics', 'capability_bindings'))
    if not supplied and not required:
        return deepcopy(raw), {'policy': POLICY_VERSION, 'status': 'legacy_unstructured', 'diagnostics': [], 'bindings': []}
    if not isinstance(raw, dict):
        raise ValueError('Structured pattern proposal must be an object')
    withheld = {r['id']: r for r in before['requirements'] if r['status'] != 'supported'}
    diagnostics = raw.get('abstention_diagnostics', [] if not withheld else None)
    bindings = raw.get('capability_bindings', [])
    if not isinstance(diagnostics, list) or not isinstance(bindings, list):
        raise ValueError('Every abstention requires structured abstention_diagnostics and capability_bindings')
    by_id = {}
    for item in diagnostics:
        _keys(item, {'id', 'decision', 'reason', 'source_basis', 'realization_details',
                     'missing_slots', 'unsupported_operators', 'alternatives'}, 'Abstention diagnosis')
        rid = item['id']
        if not isinstance(rid, str) or rid not in withheld or rid in by_id:
            raise ValueError('Diagnose each current abstention exactly once')
        _text(item['reason'], 'Abstention reason')
        _basis(item['source_basis'], rid, sources)
        for field in ('realization_details', 'missing_slots', 'unsupported_operators'):
            _texts(item[field], field)
        alts = item['alternatives']
        if not isinstance(alts, list) or len(alts) > 10:
            raise ValueError('Material alternatives must be a bounded list')
        for alt in alts:
            _keys(alt, {'interpretation', 'obligation_difference'}, 'Material alternative')
            for key in alt:
                _text(alt[key], key)
        if len({a['interpretation'] for a in alts}) != len(alts):
            raise ValueError('Material alternatives must be distinct interpretations')
        decision = item['decision']
        if decision not in {'capability_candidate', 'repair_other', 'blocked'}:
            raise ValueError('Unknown abstention decision')
        blockers = bool(item['missing_slots'] or item['unsupported_operators'] or len(alts) >= 2)
        if decision == 'blocked' and not blockers:
            raise ValueError('Realization detail alone cannot block a requirement; identify material missing meaning or operators')
        if decision == 'capability_candidate' and (item['missing_slots'] or item['unsupported_operators'] or alts):
            raise ValueError('Capability candidate must not conceal unresolved source obligations')
        by_id[rid] = item
    if set(by_id) != set(withheld):
        raise ValueError('Structured diagnosis must cover every current abstention')
    output = deepcopy(raw)
    output.pop('abstention_diagnostics', None); output.pop('capability_bindings', None)
    tlr = output['tlr']
    rows = {r['id']: r for r in tlr['requirements']}
    if len(rows) != len(tlr['requirements']):
        raise ValueError('Duplicate requirement rows cannot be hidden by pattern expansion')
    variables = {r['name']: r for r in tlr['variables']}
    if len(variables) != len(tlr['variables']):
        raise ValueError('Duplicate symbols cannot be hidden by pattern expansion')
    old_vars = {r['name']: r for r in before['variables']}
    compiled = []
    seen = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            raise ValueError('Capability binding must be an object')
        rid = binding.get('id')
        if not isinstance(rid, str) or rid not in by_id or rid in seen or by_id[rid]['decision'] != 'capability_candidate':
            raise ValueError('Capability binding needs exactly one capability_candidate diagnosis')
        seen.add(rid)
        _basis(binding.get('source_basis'), rid, sources)
        variable, row = compile_capability(binding, withheld[rid])
        current = rows.get(rid)
        if current is None:
            raise ValueError('A capability binding cannot add a missing source row')
        # Preserve supplied source fields for the immutable-source guard, and
        # proposed component mappings for the independent downstream coverage
        # gate. Never derive mappings from the old withheld row or the binding.
        for key in ('text', 'source', 'coverage'):
            if key in current:
                row[key] = deepcopy(current[key])
        if current.get('status') != 'supported' and ({k: v for k, v in current.items() if k not in {'text', 'source', 'coverage'}} !=
                {k: v for k, v in withheld[rid].items() if k not in {'text', 'source', 'coverage'}}):
            raise ValueError('Capability placeholder must preserve the original withheld row')
        if current.get('status') == 'supported':
            formula = deepcopy(current.get('formula'))
            if isinstance(formula, dict) and formula.get('at') == 'current':
                formula.pop('at')
            if formula != row['formula'] or current.get('abstraction') != row['abstraction']:
                raise ValueError('Inline capability must match deterministic pattern expansion')
        name = variable['name']
        for candidate in (old_vars.get(name), variables.get(name)):
            if candidate is not None and candidate != variable:
                raise ValueError('Capability binding cannot overwrite an existing symbol or its definition')
        if name not in variables:
            tlr['variables'].append(variable); variables[name] = variable
        rows[rid] = row
        if context_reviews and name not in old_vars:
            reviews = output.setdefault('context_reviews', [])
            if not any(r.get('kind') == 'variable' and r.get('id') == name and r.get('field') == '$record' for r in reviews):
                reviews.append({'kind': 'variable', 'id': name, 'field': '$record',
                    'reason': 'Instantiate the declared whole-source capability binding; applicability still requires source review.',
                    'source_basis': deepcopy(binding['source_basis'])})
        compiled.append({'id': rid, 'variable': variable, 'requirement': row})
    expected = {rid for rid, item in by_id.items() if item['decision'] == 'capability_candidate'}
    if seen != expected:
        raise ValueError('Every applicable capability requires an actual binding attempt in this bounded proposal')
    for rid, diagnosis in by_id.items():
        proposed = rows.get(rid, {})
        if diagnosis['decision'] == 'blocked' and proposed.get('status') == 'supported':
            raise ValueError('A blocked diagnosis cannot claim a supported candidate')
        if diagnosis['decision'] == 'repair_other' and (proposed.get('status') != 'supported' or proposed.get('abstraction', {}).get('kind') == 'capability'):
            raise ValueError('repair_other requires an actual non-capability supported proposal')
    tlr['requirements'] = [rows[r['id']] for r in tlr['requirements']]
    return output, {'policy': POLICY_VERSION, 'status': 'structured', 'diagnostics': deepcopy(diagnostics),
                    'bindings': deepcopy(bindings), 'expansions': compiled,
                    'claim': 'Pattern expansion is deterministic; source applicability remains a review claim, not a proof.'}


def evaluate_capability(binding, valuation):
    """Independent tiny reference interpreter for the capability pattern only.

    Intended for backend differential tests; not an interpreter for NL or SysML.
    """
    symbol = binding['symbol']
    if symbol not in valuation or type(valuation[symbol]) is not bool:
        raise ValueError('Capability replay needs an explicit Boolean availability observation')
    return valuation[symbol]
