"""Candidate availability must stay separate from source fidelity and approval."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from review_interpretation import build_pipeline_interpretations
import review_pipeline_adapter as adapter


def source(rid='R1'):
    return {'id': rid, 'text': 'The controller shall provide service.',
            'source': {'document': 'Requirements.csv', 'location': 'row 2'},
            'owner': 'Engineer', 'authority': 'stakeholder'}


def native(*requirements):
    return {'requirements': [{'id': req['id'], 'text': req['text'],
                             'symbols': [{'name': req['id'].lower() + '_holds', 'type': 'Bool'}],
                             'ranges': []} for req in requirements], 'typecheck': {'ok': True}}


CANDIDATE = '''(declare-const service Bool)
(define-fun leaf () Bool
  service)
(define-fun candidate () Bool
  (and leaf true))
(declare-const en_R1 Bool)
(assert (! (=> en_R1
    candidate) :named req_R1))
(check-sat)'''


class InterpretationTests(unittest.TestCase):
    def test_available_candidate_is_pending_review_with_exact_source_evidence(self):
        req = source()
        tlr = native(req)
        before = deepcopy((req, tlr))
        row = build_pipeline_interpretations([req], tlr, CANDIDATE, 'candidate.smt2')[0]
        self.assertEqual(row['status'], 'pending_review')
        self.assertEqual(row['mapping_status'], 'unique')
        self.assertEqual(row['candidate_assertion']['expression'], '(=> en_R1\n    candidate)')
        self.assertIn(row['candidate_assertion']['command'], CANDIDATE)
        self.assertEqual(row['candidate_assertion']['artifact'], 'candidate.smt2')
        self.assertEqual(row['candidate_assertion']['sha256'], hashlib.sha256(CANDIDATE.encode()).hexdigest())
        self.assertEqual(row['source'], req['source'])
        self.assertEqual(row['raw'], tlr['requirements'][0])
        self.assertIn('Boolean placeholder', row['summary'])
        self.assertEqual((req, tlr), before)

    def test_reference_closure_preserves_definitions_without_substitution(self):
        row = build_pipeline_interpretations([source()], native(source()), CANDIDATE)[0]
        self.assertEqual([item['name'] for item in row['referenced_definitions']], ['leaf', 'candidate'])
        for item in row['referenced_definitions']:
            self.assertIn(item['command'], CANDIDATE)
            self.assertEqual(item['command_sha256'], hashlib.sha256(item['command'].encode()).hexdigest())
        self.assertIn('candidate', row['candidate_assertion']['expression'])
        self.assertNotIn('service', row['candidate_assertion']['expression'])

    def test_missing_native_or_missing_candidate_requires_interpretation(self):
        for tlr, fragment in ((None, CANDIDATE), (native(source()), ''), ({'requirements': None}, CANDIDATE)):
            with self.subTest(tlr=tlr, fragment=bool(fragment)):
                row = build_pipeline_interpretations([source()], tlr, fragment)[0]
                self.assertEqual(row['status'], 'needs_interpretation')
                self.assertNotEqual(row['status'], 'unsupported')

    def test_unresolved_numeric_scope_is_not_magically_resolved_by_a_candidate(self):
        tlr = native(source())
        tlr['requirements'][0].update(formalization_status='needs_interpretation', unresolved_ranges=[{
            'original_text': 'For at least 95% of attempts, time is less than 30 seconds.',
            'executable': False, 'candidate_is_complete': False}])
        row = build_pipeline_interpretations([source()], tlr, CANDIDATE)[0]
        self.assertEqual(row['status'], 'needs_interpretation')
        self.assertEqual(row['mapping_status'], 'unique')
        self.assertIsNotNone(row['candidate_assertion'])
        self.assertIn('95%', row['unresolved_ranges'][0]['original_text'])

    def test_duplicate_assertions_and_sanitized_id_collisions_are_ambiguous(self):
        row = build_pipeline_interpretations([source()], native(source()), CANDIDATE + '\n(assert (! true :named req_R1))')[0]
        self.assertEqual(row['mapping_status'], 'ambiguous')
        self.assertEqual(len(row['candidate_assertions']), 2)
        self.assertIsNone(row['candidate_assertion'])
        reqs = [source('A-B'), source('A_B')]
        rows = build_pipeline_interpretations(reqs, native(*reqs), '(assert (! true :named req_A_B))')
        self.assertTrue(all(row['mapping_status'] == 'ambiguous' for row in rows))
        self.assertTrue(all(row['status'] == 'needs_interpretation' for row in rows))

    def test_duplicate_native_entries_and_stale_source_need_interpretation(self):
        req = source()
        tlr = native(req, req)
        row = build_pipeline_interpretations([req], tlr, CANDIDATE)[0]
        self.assertEqual(row['status'], 'needs_interpretation')
        self.assertEqual(len(row['raw_candidates']), 2)
        tlr = native(req)
        tlr['requirements'][0]['text'] = 'Different source.'
        self.assertEqual(build_pipeline_interpretations([req], tlr, CANDIDATE)[0]['status'], 'needs_interpretation')

    def test_strict_parser_does_not_confuse_named_background_with_requirement(self):
        fragment = '(declare-const p Bool)\n(assert (! p :named background))\n' + CANDIDATE
        row = build_pipeline_interpretations([source()], native(source()), fragment)[0]
        self.assertEqual(row['mapping_status'], 'unique')
        self.assertEqual(row['candidate_assertion']['expression'], '(=> en_R1\n    candidate)')

    def test_quoted_identifiers_comments_and_let_bindings_are_respected(self):
        fragment = '''(define-fun |helper (x);| () Bool true)
(define-fun shadow () Bool false)
(define-fun used ((shadow Bool)) Bool
  (and shadow |helper (x);|))
(assert (! (let ((shadow true))
  ; preserve this comment ) (
  (used shadow)) :named |req_R1|))'''
        row = build_pipeline_interpretations([source()], native(source()), fragment)[0]
        self.assertEqual(row['status'], 'pending_review')
        self.assertEqual([item['name'] for item in row['referenced_definitions']], ['helper (x);', 'used'])
        self.assertIn('; preserve this comment ) (', row['candidate_assertion']['expression'])

    def test_duplicate_referenced_definition_is_not_a_unique_interpretation(self):
        fragment = CANDIDATE + '\n(define-fun candidate () Bool false)'
        row = build_pipeline_interpretations([source()], native(source()), fragment)[0]
        self.assertEqual(row['status'], 'needs_interpretation')
        self.assertIn('ambiguous', row['reason'])

    def test_invalid_or_scoped_context_is_never_treated_as_available(self):
        for fragment in (CANDIDATE + '\n(define-fun unfinished', '(push 1)\n' + CANDIDATE + '\n(pop 1)'):
            row = build_pipeline_interpretations([source()], native(source()), fragment)[0]
            self.assertEqual(row['mapping_status'], 'invalid_context')
            self.assertEqual(row['status'], 'needs_interpretation')
            self.assertIsNone(row['candidate_assertion'])

    def test_source_fields_cannot_override_review_authority(self):
        req = {**source(), 'status': 'approved', 'review_status': 'accepted', 'source_fidelity': 'verified'}
        row = build_pipeline_interpretations([req], native(req), CANDIDATE)[0]
        self.assertEqual(row['status'], 'pending_review')
        self.assertEqual(row['review_status'], 'pending')
        self.assertEqual(row['source_fidelity'], 'pending')
        self.assertNotIn('approved', row['interpretation_scope'].split(';')[0])

    def test_adapter_exposes_available_and_missing_ids_without_unsupported_conflation(self):
        reqs = [source(), source('R2')]
        tlr = native(*reqs)
        def child(command, **kwargs):
            directory = Path(kwargs['env']['MBSE_REVIEW_CAPTURE_DIR'])
            (directory / 'pipeline_model_tlr.json').write_text(json.dumps(tlr))
            (directory / 'pipeline_model_sat.smt2').write_text(CANDIDATE)
            return Mock(poll=Mock(return_value=1), returncode=1)
        with tempfile.TemporaryDirectory() as directory, patch.object(adapter.subprocess, 'Popen', side_effect=child), patch.object(adapter, '_selected_model', return_value=('fixture-model', 'test')), patch.object(adapter, '_recheck', return_value={'status': 'ok', 'result': 'sat'}):
            progress = []
            result = adapter.run_existing_pipeline(Path(directory), 'Fixture', reqs,
                                                   lambda *args: progress.append(args), propose_behavior=False)
        stage = next(item for item in progress if item[0] == 'interpretation' and item[1] != 'running')
        self.assertEqual(stage[1], 'partial')
        self.assertIn('1 candidate interpretation(s) available with engineer review pending', stage[2])
        self.assertIn('1 requirement(s) need interpretation', stage[2])
        self.assertEqual(result['analysis']['pending_review_ids'], ['R1'])
        self.assertEqual(result['analysis']['needs_interpretation_ids'], ['R2'])
        self.assertEqual(result['analysis']['missing_mapping_ids'], ['R2'])
        self.assertEqual(result['analysis']['unsupported_ids'], [])
        self.assertTrue(result['analysis']['approval_blocked'])
        self.assertEqual(result['analysis']['source_fidelity'], 'pending')


if __name__ == '__main__':
    unittest.main()
