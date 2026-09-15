"""Display source-linked pipeline candidates without treating them as approved meaning.

This pure helper does not invoke a solver/provider, edit artifacts, or resolve
native TLR uncertainties. Exact SMT commands remain separate from native TLR.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
from typing import Any

import requirements_pipeline as legacy


def _symbol(value: str) -> str:
    return value[1:-1] if value.startswith('|') and value.endswith('|') else value


def _child_texts(command: str) -> list[str]:
    """Return exact immediate-child text after the strict parser validated a form."""
    children, index = [], 1
    while index < len(command) - 1:
        if command[index].isspace():
            index += 1
            continue
        if command[index] == ';':
            end = command.find('\n', index)
            index = len(command) if end < 0 else end + 1
            continue
        start, depth, quote = index, 0, None
        while index < len(command):
            char = command[index]
            if quote:
                if char == quote:
                    if quote == '"' and index + 1 < len(command) and command[index + 1] == '"':
                        index += 2
                        continue
                    quote = None
                index += 1
                if not quote and depth == 0:
                    break
                continue
            if char in ('"', '|'):
                quote = char
            elif char == ';':
                end = command.find('\n', index)
                index = len(command) if end < 0 else end + 1
                continue
            elif char == '(':
                depth += 1
            elif char == ')':
                if depth == 0:
                    break
                depth -= 1
                if depth == 0:
                    index += 1
                    break
            elif char.isspace() and depth == 0:
                break
            index += 1
        children.append(command[start:index])
    return children


def _free_symbols(form: Any, bound: frozenset[str] = frozenset()) -> set[str]:
    """Find helper references, respecting ordinary SMT binders and annotations."""
    if isinstance(form, str):
        if form.startswith(('"', ':')):
            return set()
        name = _symbol(form)
        return set() if name in bound else {name}
    if not isinstance(form, list) or not form:
        return set()
    if form[0] == '!' and len(form) > 1:
        return _free_symbols(form[1], bound)
    if form[0] == 'let' and len(form) == 3 and isinstance(form[1], list):
        bindings = [item for item in form[1] if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)]
        rhs = set().union(*(_free_symbols(item[1], bound) for item in bindings)) if bindings else set()
        names = frozenset(_symbol(item[0]) for item in bindings)
        return rhs | _free_symbols(form[2], bound | names)
    if form[0] in ('forall', 'exists', 'lambda') and len(form) == 3 and isinstance(form[1], list):
        names = frozenset(_symbol(item[0]) for item in form[1] if isinstance(item, list) and item and isinstance(item[0], str))
        return _free_symbols(form[2], bound | names)
    return set().union(*(_free_symbols(item, bound) for item in form))


def build_pipeline_interpretations(
    requirements: list[dict],
    raw_tlr: dict | None,
    fragment: str,
    artifact_name: str = 'pipeline_model_sat.smt2',
) -> list[dict]:
    """Associate each source with one candidate assertion; keep authority pending.

    ``pending_review`` means native TLR plus a unique named candidate exists.
    ``needs_interpretation`` means a missing/ambiguous binding or explicit native
    uncertainty remains. Neither status establishes source fidelity or approval.
    """
    artifact_hash = hashlib.sha256(fragment.encode('utf-8')).hexdigest()
    provenance = {'artifact': artifact_name, 'sha256': artifact_hash}
    native = defaultdict(list)
    native_entries = raw_tlr.get('requirements', []) if isinstance(raw_tlr, dict) else []
    for item in native_entries if isinstance(native_entries, list) else []:
        if isinstance(item, dict) and isinstance(item.get('id'), str):
            native[item['id']].append(item)
    source_names = defaultdict(list)
    for req in requirements:
        source_names['req_' + legacy._sanitize_req_id(str(req['id']))].append(str(req['id']))
    assertions, definitions = defaultdict(list), defaultdict(list)
    context_error = None
    try:
        commands = legacy._semantic_probe_commands(fragment)
        if any(form[0] in {'push', 'pop', 'reset', 'reset-assertions'} for _, form in commands):
            raise ValueError('Scoped/reset SMT scripts require an explicit active-context mapping; this presentation does not flatten them.')
        for index, (command, form) in enumerate(commands):
            if form[0] in {'define-fun', 'define-fun-rec'} and len(form) == 5 and isinstance(form[1], str) and isinstance(form[2], list):
                name = _symbol(form[1])
                bound = frozenset(_symbol(item[0]) for item in form[2] if isinstance(item, list) and item and isinstance(item[0], str))
                definitions[name].append({'name': name, 'command': command,
                    **provenance, 'command_sha256': hashlib.sha256(command.encode()).hexdigest(),
                    '_index': index, '_references': _free_symbols(form[4], bound)})
            if form[0] != 'assert' or len(form) != 2:
                continue
            annotation = form[1]
            if not isinstance(annotation, list) or len(annotation) < 4 or annotation[0] != '!':
                continue
            attributes = annotation[2:]
            names = [_symbol(attributes[i + 1]) for i, value in enumerate(attributes[:-1])
                     if value == ':named' and isinstance(attributes[i + 1], str)]
            if len(names) != 1:
                if names:
                    raise ValueError('A candidate assertion has ambiguous :named annotations.')
                continue
            body_text = _child_texts(command)[1]
            expression = _child_texts(body_text)[1]
            assertions[names[0]].append({'name': names[0], 'expression': expression, 'command': command,
                **provenance, 'command_sha256': hashlib.sha256(command.encode()).hexdigest(),
                '_references': _free_symbols(annotation[1])})
    except (ValueError, IndexError, TypeError) as exc:
        context_error = str(exc)

    def public(record):
        return deepcopy({key: value for key, value in record.items() if not key.startswith('_')})

    rows = []
    for req in requirements:
        rid = str(req['id'])
        raw_matches = native[rid]
        raw = raw_matches[0] if len(raw_matches) == 1 else None
        key = 'req_' + legacy._sanitize_req_id(rid)
        matches = assertions[key]
        mapping = 'invalid_context' if context_error else 'ambiguous' if len(matches) > 1 or len(source_names[key]) > 1 else 'unique' if len(matches) == 1 else 'missing'
        candidate = matches[0] if mapping == 'unique' else None
        referenced, visited, duplicate_definitions = [], set(), []
        queue = list(candidate['_references']) if candidate else []
        while queue:
            name = queue.pop()
            if name in visited:
                continue
            visited.add(name)
            found = definitions.get(name, [])
            if len(found) > 1:
                duplicate_definitions.append(name)
            for item in found:
                referenced.append(item)
                queue.extend(item['_references'])
        referenced.sort(key=lambda item: item['_index'])
        reasons = []
        if context_error:
            reasons.append('Candidate SMT context could not be mapped safely: ' + context_error)
        elif mapping == 'missing':
            reasons.append('No named candidate SMT assertion maps to this source requirement.')
        elif mapping == 'ambiguous':
            reasons.append('Candidate assertion names or sanitized source identifiers are ambiguous.')
        if not raw_matches:
            reasons.append('No native TLR entry maps to this source requirement.')
        elif len(raw_matches) != 1:
            reasons.append('Multiple native TLR entries map to this source requirement.')
        if raw and (raw.get('unresolved_ranges') or raw.get('formalization_status') == 'needs_interpretation'):
            reasons.append('Native TLR numeric binding or applicability is unresolved; the candidate does not automatically resolve retained source uncertainty.')
        if raw and isinstance(raw.get('text'), str) and legacy.normalise_whitespace(raw['text']) != legacy.normalise_whitespace(str(req.get('text', ''))):
            reasons.append('Native TLR text differs from the current source text; alignment needs review.')
        if duplicate_definitions:
            reasons.append('Referenced helper definitions are ambiguous: ' + ', '.join(sorted(duplicate_definitions)) + '.')
        if isinstance(raw_tlr, dict) and isinstance(raw_tlr.get('typecheck'), dict) and raw_tlr['typecheck'].get('ok') is False:
            reasons.append('The native TLR reports a structural type-check failure.')
        status = 'needs_interpretation' if reasons else 'pending_review'
        reason = ' '.join(reasons) if reasons else 'A native TLR entry and uniquely mapped SMT candidate are available for engineer review; source fidelity and semantic approval remain pending.'
        summary = reason
        if raw and any(isinstance(symbol, dict) and symbol.get('type') == 'Bool' and str(symbol.get('name', '')).endswith('_holds') for symbol in (raw.get('symbols') or [])):
            summary = 'The native TLR contains a Boolean placeholder. ' + summary
        entry = deepcopy(raw) if raw else {}
        entry.update(deepcopy(req))
        entry.update({'raw': deepcopy(raw), 'raw_candidates': deepcopy(raw_matches) if len(raw_matches) > 1 else [],
                      'status': status, 'reason': reason, 'summary': summary,
                      'review_status': 'pending', 'source_fidelity': 'pending',
                      'mapping_status': mapping, 'candidate_assertion': public(candidate) if candidate else None,
                      'candidate_assertions': [public(item) for item in matches],
                      'referenced_definitions': [public(item) for item in referenced],
                      'candidate_provenance': dict(provenance),
                      'interpretation_scope': 'Candidate association only; helper bodies are shown unchanged, never substituted or approved.'})
        rows.append(entry)
    return rows
