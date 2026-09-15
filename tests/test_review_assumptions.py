"""Assumption provenance, disclosure capture, and review-state boundaries."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from review_assumptions import build_assumptions, assumption_blockers, assumption_review_hash
import review_llm_entry as entry
from review_profile import interpret, parse_requirements


class AssumptionTests(unittest.TestCase):
    def test_local_event_premises_are_metadata_and_domain_is_encoded(self):
        reqs = parse_requirements('After each START command, the controller shall produce the response within 5 seconds.', 'text', 'fixture')
        with tempfile.TemporaryDirectory() as directory:
            rows = build_assumptions(interpret(reqs), reqs, 'local', Path(directory))
        event = next(r for r in rows if 'occurrence is required' in r['statement'])
        self.assertEqual(event['formalization'], 'metadata_only')
        domain = next(r for r in rows if r['formalization'] == 'encoded')
        self.assertIn('>= 0 s', domain['statement'])
        self.assertEqual(event['requirement_ids'], [reqs[0]['id']])

    def test_nested_llm_disclosures_and_raw_comments_preserve_provenance(self):
        reqs = [{'id': 'R1', 'text': 'source'}]
        raw = {'requirements': [{'id': 'R1', 'assumptions': ['No simultaneous requests.']}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'pipeline_model_translate.json').write_text(json.dumps(raw))
            (path / 'llm-call-001.json').write_text(json.dumps({'response': '; REVIEW_ASSUMPTION {"statement":"Power remains available.","requirement_ids":["R1"],"reason":"Progress depends on supply."}\n(check-sat)'}))
            rows = build_assumptions({'raw': raw}, reqs, 'pipeline', path)
        premise = next(r for r in rows if r['statement'] == 'No simultaneous requests.')
        self.assertEqual(premise['origin'], 'llm_reported')
        self.assertEqual(premise['formalization'], 'unestablished')
        self.assertEqual(premise['requirement_ids'], ['R1'])
        self.assertGreaterEqual(len(premise['evidence']), 2)
        power = next(r for r in rows if r['statement'] == 'Power remains available.')
        self.assertEqual(power['evidence'][0]['response_line'], 1)
        self.assertTrue(any(r['origin'] == 'pipeline_audit' for r in rows))

    def test_missing_disclosure_never_becomes_claim_of_no_assumptions(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = build_assumptions({}, [{'id': 'R1'}], 'pipeline', Path(directory))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['formalization'], 'unestablished')

    def test_latest_decision_controls_readiness_without_changing_assumption(self):
        row = {'id': 'A', 'statement': 'power'}
        run = {'assumptions': [row], 'assumption_reviews': []}
        self.assertTrue(assumption_blockers(run))
        first_hash = assumption_review_hash([])
        run['assumption_reviews'].append({'assumption_id': 'A', 'decision': 'accept'})
        self.assertFalse(assumption_blockers(run))
        self.assertNotEqual(first_hash, assumption_review_hash(run['assumption_reviews']))
        run['assumption_reviews'].append({'assumption_id': 'A', 'decision': 'reject'})
        self.assertTrue(assumption_blockers(run))
        self.assertEqual(row, {'id': 'A', 'statement': 'power'})

    def test_equal_assumption_text_does_not_merge_different_predicates(self):
        behavior = {'properties': [{'id': 'P', 'kind': 'always', 'requirement_ids': ['R1']}],
                    'assumptions': [{'id': 'A1', 'text': 'Operating mode', 'scope': 'initial', 'predicate': True},
                                    {'id': 'A2', 'text': 'Operating mode', 'scope': 'always', 'predicate': False}]}
        with tempfile.TemporaryDirectory() as directory:
            rows = build_assumptions({}, [{'id': 'R1'}], 'local', Path(directory), behavior)
        matching = [r for r in rows if 'Operating mode' in r['statement']]
        self.assertEqual(len(matching), 2)
        self.assertNotEqual(matching[0]['id'], matching[1]['id'])
        self.assertIn('true', matching[0]['statement'])
        self.assertIn('false', matching[1]['statement'])

    def test_wrapper_captures_original_response_without_additional_llm_call(self):
        mock = AsyncMock(return_value='(check-sat)')
        with tempfile.TemporaryDirectory() as directory, patch.object(entry.legacy, '_codex_chat_text', mock):
            entry.install_capture(Path(directory))
            result = asyncio.run(entry.legacy._codex_chat_text('SMT only', 'synthetic requirement', 'fixture-model'))
            captured = json.loads((Path(directory) / 'llm-call-001.json').read_text())
        self.assertEqual(result, '(check-sat)')
        self.assertEqual(captured['response'], result)
        self.assertIn('REVIEW_ASSUMPTION', captured['system_prompt'])
        self.assertEqual(captured['status'], 'completed')
        mock.assert_awaited_once()

    def test_wrapper_retains_failed_call_record(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(entry.legacy, '_codex_chat_text', AsyncMock(side_effect=RuntimeError('fixture unavailable'))):
            entry.install_capture(Path(directory))
            with self.assertRaises(RuntimeError):
                asyncio.run(entry.legacy._codex_chat_text('system', 'source', 'fixture-model'))
            captured = json.loads((Path(directory) / 'llm-call-001.json').read_text())
        self.assertEqual(captured['status'], 'failed')
        self.assertNotIn('response', captured)


if __name__ == '__main__':
    unittest.main()
