"""Losslessly share repeated documentary context in inference prompts only.

Authoritative source records and validation inputs stay expanded. Intern whole
shared-context lists, rather than merging context IDs, to preserve each source's
exact context membership, ordering and any conflicting records with the same ID.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json


CONTEXT_ENCODING_POLICY = (
    'A source.context.shared object containing only $ref is a JSON Pointer to '
    'the complete shared-context list in shared_source_context. Resolve pointers '
    'within the object containing source_context_encoding and shared_source_context, '
    'including when that object is nested under original_request for format recovery. '
    'Read that list at exactly that source.context.shared location, as though '
    'it were written there in full. Each reference applies only to its own '
    'requirement; never apply other registry entries to it. Context record IDs, '
    'context_ids, citations, order and scope remain exactly as supplied. These '
    'are documentary source records, not additional requirements or automatic '
    'background assumptions. References are a prompt encoding only. When emitting '
    'TLR, omit text and source in output requirement records; the controller '
    'restores the complete original source.'
)


def source_prompt_fields(sources, field='requirements'):
    """Return an independent prompt view, interning repeated nonempty lists.

    Sources without repeated source.context.shared lists retain their existing
    prompt shape. Full JSON values determine identity; authored IDs are never
    used as deduplication keys. Empty, absent and unique lists remain inline.
    An existing whole-value $ref keeps the entire prompt in its original shape
    so literal source data cannot be confused with our reference encoding.
    """
    rows = deepcopy(sources)
    contexts = []
    for row in rows:
        source = row.get('source')
        context = source.get('context') if isinstance(source, dict) else None
        shared = context.get('shared') if isinstance(context, dict) else None
        if isinstance(shared, dict) and set(shared) == {'$ref'}:
            return {field: rows}
        if isinstance(shared, list) and shared:
            key = json.dumps(shared, ensure_ascii=False, sort_keys=True, allow_nan=False)
            contexts.append((context, key))
    counts = Counter(key for _, key in contexts)
    names, registry = {}, {}
    for context, key in contexts:
        if counts[key] < 2:
            continue
        if key not in names:
            name = f'context_{len(names) + 1}'
            names[key] = name
            registry[name] = context['shared']
        context['shared'] = {'$ref': f'#/shared_source_context/{names[key]}'}
    result = {field: rows}
    if registry:
        result['shared_source_context'] = registry
        result['source_context_encoding'] = {
            'schema': 'mbse_prompt_context/1', 'policy': CONTEXT_ENCODING_POLICY}
    return result
