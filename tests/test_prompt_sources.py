"""Prompt context compaction preserves documentary content and applicability."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from canonical_cli import _prompt
from canonical_feedback import feedback_prompt
from canonical_repair import diagnosis_prompt, repair_prompt
from canonical_tlr import validate_tlr
from prompt_sources import source_prompt_fields


def source(rid, shared):
    return {'id': rid, 'text': f'{rid}: The system shall eventually recover.',
            'source': {'page': 7, 'context': {'local': f'Applies to {rid}.',
                                             'shared': deepcopy(shared)}}}


def resolve_sources(payload, field='requirements'):
    """Resolve standard JSON Pointers independently of the compactor."""
    rows = deepcopy(payload[field])
    for row in rows:
        context = row.get('source', {}).get('context', {})
        shared = context.get('shared')
        if isinstance(shared, dict) and set(shared) == {'$ref'}:
            value = payload
            for segment in shared['$ref'].removeprefix('#/').split('/'):
                value = value[segment.replace('~1', '/').replace('~0', '~')]
            context['shared'] = deepcopy(value)
    return rows


class PromptSourceTests(unittest.TestCase):
    def test_repeated_context_is_stored_once_and_roundtrips_without_mutation(self):
        shared = [{'id': 'D1', 'kind': 'definition', 'text': 'Each controller observes its own bus.',
                   'context_ids': ['R2'], 'citations': [{'page': 3, 'quote': 'Its own bus.'}]}]
        sources = [source('R1', shared), source('R2', shared)]
        original = deepcopy(sources)
        payload = source_prompt_fields(sources)
        self.assertEqual(resolve_sources(payload), original)
        self.assertEqual(sources, original)
        self.assertEqual(list(payload['shared_source_context'].values()), [shared])
        self.assertEqual(json.dumps(payload).count('Each controller observes its own bus.'), 1)
        self.assertIn('only to its own requirement', payload['source_context_encoding']['policy'])
        payload['shared_source_context']['context_1'][0]['text'] = 'Changed prompt view.'
        payload['requirements'][0]['source']['context']['local'] = 'Changed local view.'
        self.assertEqual(sources, original)

    def test_heterogeneous_lists_preserve_membership_order_and_local_scope(self):
        a = {'id': 'S1', 'kind': 'scope', 'text': 'Controller A only.'}
        b = {'id': 'S2', 'kind': 'scope', 'text': 'Controller B only.'}
        sources = [source('A1', [a]), source('B1', [b]), source('A2', [a]),
                   source('B2', [b]), source('AB', [a, b]), source('BA', [b, a])]
        payload = source_prompt_fields(sources)
        self.assertEqual(resolve_sources(payload), sources)
        self.assertEqual(len(payload['shared_source_context']), 2)
        self.assertNotEqual(payload['requirements'][0]['source']['context']['shared'],
                            payload['requirements'][1]['source']['context']['shared'])
        self.assertEqual(payload['requirements'][4]['source']['context']['shared'], [a, b])
        self.assertEqual(payload['requirements'][5]['source']['context']['shared'], [b, a])

    def test_duplicate_context_ids_do_not_merge_distinct_records(self):
        a = {'id': 'S1', 'text': 'Scope A.'}
        b = {'id': 'S1', 'text': 'Scope B.'}
        sources = [source('R1', [a, b, a]), source('R2', [a, b, a]),
                   source('R3', [b, a]), source('R4', [b, a])]
        payload = source_prompt_fields(sources)
        self.assertEqual(resolve_sources(payload), sources)
        self.assertEqual(list(payload['shared_source_context'].values()), [[a, b, a], [b, a]])

    def test_empty_missing_unique_and_nonlist_contexts_keep_existing_shape(self):
        sources = [source('R1', []), source('R2', []), source('R3', [{'id': 'S1', 'text': 'Once.'}]),
                   {'id': 'R4', 'text': 'No source.'},
                   {'id': 'R5', 'text': 'No context.', 'source': {'page': 2}},
                   {'id': 'R6', 'text': 'Local context.', 'source': {'context': {'note': 'Local only.'}}},
                   source('R7', {'legacy': 'An inline object.'})]
        self.assertEqual(source_prompt_fields(sources), {'requirements': sources})
        self.assertEqual(source_prompt_fields([], 'source_packet'), {'source_packet': []})

    def test_numeric_and_boolean_context_values_are_not_coalesced(self):
        sources = [source('R1', [{'value': 1}]), source('R2', [{'value': 1}]),
                   source('R3', [{'value': True}]), source('R4', [{'value': True}])]
        payload = source_prompt_fields(sources)
        self.assertEqual(len(payload['shared_source_context']), 2)
        expanded = resolve_sources(payload)
        self.assertIs(type(expanded[0]['source']['context']['shared'][0]['value']), int)
        self.assertIs(type(expanded[2]['source']['context']['shared'][0]['value']), bool)

    def test_authored_references_remain_data_and_cannot_shadow_registry(self):
        shared = [{'id': 'S1', 'text': 'Literal metadata.',
                   'example': {'$ref': '#/shared_source_context/context_1'}}]
        sources = [source('R1', shared), source('R2', shared)]
        payload = source_prompt_fields(sources)
        self.assertEqual(resolve_sources(payload), sources)
        self.assertEqual(payload['shared_source_context']['context_1'], shared)
        for reference in ('#/shared_source_context/context_1', 'https://example.test/context'):
            with self.subTest(reference=reference):
                legacy = sources + [source('R3', {'$ref': reference})]
                self.assertEqual(source_prompt_fields(legacy), {'requirements': legacy})

    def test_large_document_prompt_scales_with_context_once(self):
        shared = [{'id': f'S{i}', 'text': ('Document context. ' * 30) + str(i)} for i in range(53)]
        sources = [source(f'R{i}', shared) for i in range(86)]
        prompt = _prompt(sources, None, 'Large document')
        payload = json.loads(prompt)
        self.assertEqual(resolve_sources(payload), sources)
        self.assertLess(len(prompt), len(json.dumps({'requirements': sources}, indent=2)) / 20)

    def test_generation_feedback_and_abstention_prompts_share_same_encoding(self):
        shared = [{'id': 'S1', 'kind': 'scope', 'text': 'Recovery concerns the primary controller.'}]
        sources = [source('R1', shared), source('R2', shared)]
        original = deepcopy(sources)
        tlr = validate_tlr({'schema': 'mbse_tlr/1', 'abstraction_policy': 'mbse_abstraction/1',
            'variables': [], 'assumptions': [], 'requirements': [
                {'id': row['id'], 'status': 'unsupported', 'reason_code': 'profile_limit',
                 'reason': 'Eventuality requires temporal semantics.'} for row in sources]},
            sources, require_abstractions=True)
        accepted = deepcopy(tlr)
        prompts = [('generation', json.loads(_prompt(sources, None, 'Model')), 'requirements'),
                   ('feedback', json.loads(feedback_prompt(sources, None, tlr)), 'source_packet'),
                   ('diagnosis', json.loads(diagnosis_prompt(sources, None, tlr)), 'source_packet'),
                   ('repair', json.loads(repair_prompt(sources, None, tlr)), 'source_packet')]
        for name, payload, field in prompts:
            with self.subTest(prompt=name):
                self.assertEqual(resolve_sources(payload, field), sources)
                self.assertEqual(list(payload['shared_source_context'].values()), [shared])
                self.assertEqual(payload['source_context_encoding']['schema'], 'mbse_prompt_context/1')
                for key in ('current_tlr', 'accepted_tlr'):
                    if key in payload:
                        self.assertTrue(all('source' not in row and 'text' not in row
                                            for row in payload[key]['requirements']))
        self.assertEqual(sources, original)
        self.assertEqual(tlr, accepted)


if __name__ == '__main__':
    unittest.main()
