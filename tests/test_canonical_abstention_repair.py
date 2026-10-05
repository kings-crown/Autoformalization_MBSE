"""Bounded recovery execution, frozen baseline isolation, and actual tool evidence."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import canonical_cli as cli
from canonical_repair import DIAGNOSIS_INSTRUCTIONS, REPAIR_INSTRUCTIONS, RECOVERY_POLICY_VERSION
from canonical_tlr import validate_tlr, render_sysml, tlr_context
from review_sysml import compiler_capability


def fixture():
    sources = [
        {'id': 'R0', 'text': 'The battery voltage shall be at most 28 V.', 'source': {'document': 'fixture'}},
        {'id': 'R1', 'text': 'The system shall support multicast.', 'source': {'document': 'fixture'}},
        {'id': 'R2', 'text': 'The pump shall stop and remain stopped.', 'source': {'document': 'fixture'}}]
    tlr = {'schema': 'mbse_tlr/1', 'abstraction_policy': 'mbse_abstraction/1',
           'variables': [{'name': 'voltage', 'type': 'Real', 'unit': 'V', 'description': 'Battery voltage.'}],
           'assumptions': [], 'requirements': [
        {'id': 'R0', 'status': 'supported', 'formula': {'op': '<=', 'args': [{'var': 'voltage'}, {'value': '28', 'unit': 'V'}]},
         'abstraction': {'kind': 'state_constraint', 'meaning': 'Voltage is at most 28 V.', 'scope': 'Battery state.', 'limitations': []}},
        {'id': 'R1', 'status': 'unresolved', 'reason_code': 'missing_context', 'reason': 'Implementation architecture is unspecified.'},
        {'id': 'R2', 'status': 'unsupported', 'reason_code': 'profile_limit', 'reason': 'Persistence requires temporal history.'}]}
    return sources, tlr


def recover(tlr):
    proposed = deepcopy(tlr)
    proposed['variables'].append({'name': 'multicast_available', 'type': 'Bool', 'description': 'Availability of multicast support.'})
    proposed['requirements'][1] = {'id': 'R1', 'status': 'supported', 'formula': {'var': 'multicast_available'},
        'abstraction': {'kind': 'capability', 'meaning': 'Multicast support is available.', 'scope': 'Named operation availability.',
                        'limitations': ['No delivery or runtime invocation guarantee.'], 'subject': 'system',
                        'operation': 'multicast', 'symbol': 'multicast_available'}}
    return proposed


def diagnose(sources, tlr, repair_ids=('R1',), rule='capability'):
    table = {r['id']: r for r in sources}
    return {'schema': 'abstention_diagnosis/1', 'requirements': [
        {'id': r['id'], 'decision': 'repair' if r['id'] in repair_ids else 'retain_unsupported',
         'reason': 'Named availability is representable.' if r['id'] in repair_ids else 'Requires unsupported temporal history.',
         'rule': rule if r['id'] in repair_ids else None,
         'source_basis': [{'source_id': r['id'], 'quote': table[r['id']]['text']}] if r['id'] in repair_ids else [],
         'repair_instruction': 'Represent the stated obligation at the permitted abstraction level.' if r['id'] in repair_ids else None}
        for r in tlr['requirements'] if r['status'] != 'supported']}


def proposal(sources, before, after):
    texts = {row['id']: row['text'] for row in sources}
    statuses = {row['id']: row['status'] for row in after['requirements']}
    reviews = []
    for row in before['requirements']:
        if row['status'] == 'supported':
            continue
        recovered = statuses[row['id']] == 'supported'
        reviews.append({'id': row['id'], 'outcome': 'proposed' if recovered else 'retained',
            'considered_rules': {
                'state_constraint': 'Consider the complete source obligation over the current state.',
                'capability': 'Consider whether the source explicitly requires availability of a named operation.',
                'event_relation': 'Consider whether a guarded current-state event relation captures the entire obligation.'},
            'reason': 'The proposal applies a permitted abstraction.' if recovered else 'The complete obligation still requires temporal history.',
            'source_basis': [{'source_id': row['id'], 'quote': texts[row['id']]}],
            'blocking_detail': None if recovered else 'Remaining stopped refers to later states that this profile cannot express.'})
    return {'schema': 'abstention_proposal/1', 'tlr': deepcopy(after), 'reviews': reviews}


def response_mock(*responses):
    return Mock(side_effect=[json.dumps(r) if isinstance(r, dict) else r for r in responses])


class UnifiedRecoveryTests(unittest.TestCase):
    def test_deprecated_budget_uses_one_feedback_proposal_without_a_diagnosis_call(self):
        from test_canonical_feedback_execution import proposal as feedback_proposal
        sources, initial = fixture()
        calls = response_mock(initial, feedback_proposal(sources, initial, initial))
        with tempfile.TemporaryDirectory() as tmp:
            result = cli.run_candidate(sources, Path(tmp) / 'run', 'B', model='offline',
                generator=calls, compile_model=False, abstention_repairs=2)
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(result['feedback_repair']['stop_reason'], 'no_change_after_review')
            self.assertEqual(result['configuration']['diagnosis_calls'], 0)
            self.assertEqual(result['configuration']['feedback_repair_budget'], 2)
            self.assertEqual([c.args[4] for c in calls.call_args_list], ['generation', 'feedback_call'])
            self.assertNotIn('repair', result)

    def test_overlapping_budget_options_fail_before_any_inference(self):
        sources, initial = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'deprecated alias'):
                cli.run_candidate(sources, Path(tmp) / 'run', 'B', tlr=initial,
                    abstention_repairs=1, feedback_repairs=1)

    def test_supported_rules_are_reviewed_by_the_same_loop(self):
        from test_canonical_feedback_execution import fixture as numeric, proposal as review, corrected
        sources, initial = numeric()
        fixed = corrected(initial)
        calls = response_mock(review(sources, initial, fixed))
        with tempfile.TemporaryDirectory() as tmp:
            result = cli.run_candidate(sources, Path(tmp) / 'run', 'B', tlr=initial,
                generator=calls, compile_model=False, abstention_repairs=1)
            self.assertEqual(result['configuration']['semantic_repairs'], 1)
            self.assertEqual(result['tlr'], validate_tlr(fixed, sources))


if __name__ == '__main__':
    unittest.main()
