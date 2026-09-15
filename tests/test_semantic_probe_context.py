"""Real SMT context must survive diagnostic query construction."""
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import requirements_pipeline as pipeline


class ContextTests(unittest.TestCase):
    def test_multiline_forms_and_quoted_text_are_preserved(self):
        definition = '''(define-fun |trigger (ready);| () Bool
  ; A comment with unbalanced punctuation: (
  true)'''
        source = '(set-info :source "literal (;) and ""quotes""")\n' + definition + '\n(check-sat)\n; comment\n(get-model)\n(exit)'
        context, _ = pipeline._semantic_probe_context(source, set(), drop_requirements=True)
        self.assertIn(definition, context)
        self.assertIn('"literal (;) and ""quotes"""', context)
        self.assertNotIn('(check-sat)', context)
        self.assertNotIn('(get-model)', context)
        self.assertNotIn('(exit)', context)

    def test_incomplete_forms_and_stateful_context_fail_conservatively(self):
        for source in ('(define-fun trigger () Bool\n true', '(assert "unterminated)',
                       '(assert |unterminated)', '(push 1)\n(assert true)\n(pop 1)', '(reset)'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                pipeline._semantic_probe_context(source, set(), drop_requirements=True)
        result = pipeline._check_vacuity('(push 1)', {'R1': '(=> trigger true)'})
        self.assertFalse(result['passed'])
        self.assertEqual(result['solver_errors'][0]['status'], 'context_error')

    def test_only_direct_positive_enable_assertions_are_removed(self):
        source = '''(declare-const en_R1 Bool)
(declare-const p Bool)
(assert en_R1)
(assert (! p :named background))
(assert (=> en_R1 p))
(assert (! (=> en_R1 p) :named req_R1))'''
        context, scope = pipeline._semantic_probe_context(source, {'en_R1'}, drop_requirements=True)
        self.assertNotIn('(assert en_R1)', context)
        self.assertIn('(assert (! p :named background))', context)
        self.assertIn('(assert (=> en_R1 p))', context)
        self.assertNotIn(':named req_R1', context)
        self.assertEqual(scope['removed_direct_enable_assertions'], ['en_R1'])
        self.assertEqual(scope['removed_requirement_assertions'], ['req_R1'])


@unittest.skipUnless(shutil.which('z3'), 'Real Z3 executable unavailable')
class ProbeTests(unittest.TestCase):
    def vacuity(self, source):
        return pipeline._check_vacuity(source, pipeline._extract_named_assertions(source))

    def test_multiline_helper_definition_and_interleaved_declaration_work(self):
        source = '''(set-logic QF_LIA)
(declare-const x Int)
(define-fun trigger () Bool
  (> x 0))
(assert (>= x 0))
(declare-const allowed Bool)
(assert (! (=> trigger allowed) :named req_R1))
(check-sat)
; final comment
(get-model)
(exit)'''
        result = self.vacuity(source)
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['checked_implication_count'], 1)
        self.assertEqual(result['solver_errors'], [])

    def test_background_assumption_really_makes_trigger_unreachable(self):
        source = '''(set-logic QF_LIA)
(declare-const x Int)
(define-fun trigger () Bool
  (> x 0))
(assert (= x 0))
(assert (! (=> trigger false) :named req_R1))
(check-sat)'''
        result = self.vacuity(source)
        self.assertFalse(result['passed'])
        self.assertEqual(result['vacuous_requirements'][0]['requirement_id'], 'R1')
        self.assertEqual(result['solver_errors'], [])

    def test_named_requirement_does_not_make_its_own_trigger_unreachable(self):
        source = '''(set-logic QF_LIA)
(declare-const trigger Bool)
(assert (! (=> trigger false) :named req_R1))
(check-sat)'''
        self.assertTrue(self.vacuity(source)['passed'])

    def test_background_forced_trigger_is_reported_as_contextual(self):
        source = '''(declare-const trigger Bool)
(assert trigger)
(assert (! (=> trigger true) :named req_R1))'''
        result = self.vacuity(source)
        self.assertEqual(len(result['tautological_antecedents']), 1)
        self.assertIn('retained background', result['tautological_antecedents'][0]['message'])

    def test_administrative_guard_is_explicitly_skipped_not_proved(self):
        source = '''(declare-const en_R1 Bool)
(declare-const event Bool)
(define-fun holds () Bool
  (=> event true))
(assert (! (=> en_R1 holds) :named req_R1))
(assert en_R1)
(check-sat)'''
        result = self.vacuity(source)
        self.assertTrue(result['passed'])
        self.assertEqual(result['checked_implication_count'], 0)
        self.assertEqual(result['skipped_administrative_guards'][0]['requirement_id'], 'R1')
        self.assertFalse(result['scope']['underlying_conditional_triggers_checked'])
        self.assertIn('not evidence', result['skipped_administrative_guards'][0]['reason'])

    def test_three_way_conflict_does_not_become_pairwise_conflicts(self):
        source = '''(set-option :produce-models true)
(set-logic QF_LIA)
(declare-const p Bool)
(declare-const q Bool)
(define-fun either_false () Bool
  (not (and p q)))
(declare-const en_R1 Bool)
(declare-const en_R2 Bool)
(declare-const en_R3 Bool)
(assert (! (=> en_R1 p) :named req_R1))
(assert (! (=> en_R2 q) :named req_R2))
(assert (! (=> en_R3 either_false) :named req_R3))
(assert en_R1)
(assert en_R2)
(assert en_R3)
(check-sat)
; a trailing comment previously defeated trailing-query removal
(get-model)
(exit)'''
        self.assertEqual(pipeline._solver_verdict(pipeline.run_z3_fragment(source.split('(check-sat)')[0] + '(check-sat)')), 'unsat')
        result = pipeline._check_pairwise_conflicts(source, ['R1', 'R2', 'R3'])
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['conflicts'], [])
        self.assertEqual(result['solver_errors'], [])
        self.assertEqual(len(result['scope']['removed_direct_enable_assertions']), 3)

    def test_genuine_pairwise_conflict_and_background_are_preserved(self):
        source = '''(set-logic QF_LIA)
(declare-const x Int)
(define-fun positive () Bool
  (> x 0))
(declare-const en_R1 Bool)
(declare-const en_R2 Bool)
(assert (= x 1))
(assert (! (=> en_R1 positive) :named req_R1))
(assert (! (=> en_R2 (< x 1)) :named req_R2))
(assert en_R1)
(assert en_R2)
(check-sat)'''
        result = pipeline._check_pairwise_conflicts(source, ['R1', 'R2'])
        self.assertFalse(result['passed'])
        self.assertEqual(len(result['conflicts']), 1)
        self.assertEqual(result['solver_errors'], [])

    def test_unknown_constant_errors_are_not_downgraded_to_pass(self):
        source = '''(declare-const result Bool)
(assert (! (=> missing_trigger result) :named req_R1))'''
        result = self.vacuity(source)
        self.assertFalse(result['passed'])
        self.assertEqual(len(result['solver_errors']), 1)
        self.assertIn('unknown constant', result['solver_errors'][0]['diagnostics'])

    def test_portfolio_disagreement_remains_inconclusive(self):
        result = {'status': 'ok', 'result': 'sat', 'cross_check': {'agree': False}}
        with patch.object(pipeline, 'run_z3_fragment', return_value=result):
            check = self.vacuity('(declare-const x Bool)\n(assert (! (=> x true) :named req_R1))')
        self.assertFalse(check['passed'])
        self.assertEqual(check['solver_errors'][0]['status'], 'inconclusive')

    def test_atomic_named_assertion_is_outside_implication_check(self):
        result = self.vacuity('(declare-const ready Bool)\n(assert (! ready :named req_R1))')
        self.assertTrue(result['passed'])
        self.assertEqual(result['checked_implication_count'], 0)

    def test_saved_v3_context_is_preserved_without_editing_artifacts(self):
        path = Path(__file__).resolve().parents[1] / 'out/review_workbench/run-cb72bca19a894957/pipeline_model_translate.json'
        if not path.exists():
            self.skipTest('Saved diagnostic fixture is unavailable')
        source = json.loads(path.read_text())['smt_fragment']
        named = pipeline._extract_named_assertions(source)
        result = pipeline._check_vacuity(source, named)
        self.assertEqual(result['solver_errors'], [])
        self.assertEqual(len(result['skipped_administrative_guards']), 9)
        self.assertEqual(result['checked_implication_count'], 0)
        pairs = pipeline._check_pairwise_conflicts(source, list(named))
        self.assertTrue(pairs['passed'], pairs)


if __name__ == '__main__':
    unittest.main()
