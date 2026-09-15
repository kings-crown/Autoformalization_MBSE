"""Native preflight must preserve unresolved numerical meaning without ill-typed ranges."""
import csv
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import requirements_pipeline as legacy
import review_llm_entry as entry
import review_pipeline_adapter as adapter
from review_assumptions import build_assumptions

ROOT = Path(__file__).resolve().parents[1]


class NativeTlrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = ROOT / 'examples/pure/eirene_fun7_harvest_requirements.csv'
        cls.source = cls.path.read_text()
        cls.requirements = legacy.load_requirements_source(cls.path)

    def build(self, requirements=None):
        with patch.dict(os.environ, {'MBSE_TLR_TYPECHECK_MODE': 'error'}):
            return legacy._build_tlf_payload(self.path, self.source, self.requirements if requirements is None else requirements)

    def test_all_240_rows_pass_strict_structural_check_with_explicit_unresolved_scope(self):
        payload = self.build()
        self.assertTrue(payload['typecheck']['ok'])
        self.assertEqual(len(payload['requirements']), 240)
        self.assertEqual([r['id'] for r in payload['requirements']], [r['id'] for r in self.requirements])
        pending = {r['id'] for r in payload['requirements'] if r.get('formalization_status') == 'needs_interpretation'}
        self.assertEqual(pending, {'EIRENE_FUN7-R055', 'EIRENE_FUN7-R208'})
        warned = {w['requirement_id'] for w in payload['typecheck']['warnings'] if w['code'] == 'UNRESOLVED_NUMERIC_BINDING'}
        self.assertEqual(warned, pending)
        for req in payload['requirements']:
            types = {s['name']: s['type'] for s in req['symbols']}
            self.assertTrue(all(types[r['symbol']] in ('Int', 'Integer', 'Real') for r in req['ranges']))

    def test_full_call_categories_and_population_condition_survive_compact_prompt(self):
        compact = legacy._compact_tlf_for_prompt(self.build())
        for suffix, expected in [('055', ['2', '5', '5', '7', '10']), ('208', ['95', '30'])]:
            rid = 'EIRENE_FUN7-R' + suffix
            req = next(r for r in compact['requirements'] if r['id'] == rid)
            unresolved = req['unresolved_ranges'][0]
            source = next(r['text'] for r in self.requirements if r['id'] == rid)
            self.assertEqual(unresolved['original_text'], source)
            self.assertEqual([n['text'] for n in unresolved['numeric_mentions']], expected)
            self.assertFalse(unresolved['executable'])
            self.assertFalse(unresolved['candidate_is_complete'])
            self.assertEqual(req['ranges'], [])
            self.assertEqual(req['symbols'][0]['type'], 'Bool')

    def test_boolean_arithmetic_still_fails_native_typecheck(self):
        payload = self.build()
        row = next(r for r in payload['requirements'] if r['id'] == 'EIRENE_FUN7-R055')
        row['ranges'] = [row['unresolved_ranges'][0]['candidate']]
        result = legacy._native_typecheck_tlf(payload)
        self.assertFalse(result['ok'])
        self.assertIn('RANGE_NON_NUMERIC_SYMBOL', [e['code'] for e in result['errors']])

    def test_resolved_numeric_quantity_keeps_its_bound(self):
        payload = self.build([{'id': 'VOLTAGE', 'text': 'The voltage shall be less than 28 V.'}])
        req = payload['requirements'][0]
        self.assertEqual(req['ranges'][0]['upper'], '28')
        self.assertEqual(req['ranges'][0]['symbol'], 'voltage')
        self.assertNotIn('unresolved_ranges', req)
        self.assertTrue(payload['typecheck']['ok'])

    def test_preflight_is_saved_without_provider_calls_and_exposed_in_ledger(self):
        original = legacy._build_tlf_payload
        with tempfile.TemporaryDirectory() as directory, patch.object(legacy, '_build_tlf_payload', original), patch.object(legacy, '_codex_chat_text') as provider:
            entry.install_tlr_capture(Path(directory))
            payload = self.build()
            saved = json.loads((Path(directory) / 'initial_tlr.json').read_text())
            self.assertEqual(saved, payload)
            ledger = build_assumptions({'raw': payload}, self.requirements, 'pipeline', Path(directory))
            pending = [a for a in ledger if a['origin'] == 'tlr_preprocessing']
            self.assertEqual(len(pending), 2)
            self.assertTrue(all(a['formalization'] == 'unestablished' for a in pending))
            self.assertTrue(any('95%' in a['statement'] for a in pending))
            provider.assert_not_called()

    def test_adapter_preserves_native_artifact_if_later_generation_fails(self):
        payload = self.build()
        progress = []
        def child(command, **kwargs):
            directory = Path(kwargs['env']['MBSE_REVIEW_CAPTURE_DIR'])
            (directory / 'initial_tlr.json').write_text(json.dumps(payload))
            return Mock(poll=Mock(return_value=1), returncode=1)
        with tempfile.TemporaryDirectory() as directory, patch.object(adapter.subprocess, 'Popen', side_effect=child), patch.object(adapter, '_selected_model', return_value=('fixture-model', 'test')):
            result = adapter.run_existing_pipeline(Path(directory), 'Preflight fixture', self.requirements,
                                                   lambda *args: progress.append(args), propose_behavior=False)
        self.assertEqual(result['tlr']['origin'], 'native_preflight')
        self.assertEqual(len(result['tlr']['requirements']), 240)
        row = next(r for r in result['tlr']['requirements'] if r['id'] == 'EIRENE_FUN7-R055')
        self.assertEqual(row['status'], 'needs_interpretation')
        self.assertIn('unresolved', row['reason'])
        self.assertTrue(any(stage == 'interpretation' and status == 'partial' for stage, status, _ in progress))


if __name__ == '__main__':
    unittest.main()
