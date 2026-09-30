"""Compatibility code must not manufacture evidence or discard obligations."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import requirements_pipeline as pipeline


class EncodingPreservationTests(unittest.TestCase):
    def test_model_call_retains_raw_answer_and_exact_requested_model(self):
        import json
        from canonical_cli import _ask
        answer = '```json\n{"requirements": []}\n```'
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, '_run_codex_exec', return_value=answer) as call:
            self.assertEqual(_ask('instructions', 'input', 'explicit-model', Path(tmp), 'generation'), answer)
            call.assert_called_once_with('System instructions:\ninstructions\n\nUser request:\ninput\n', 'explicit-model')
            log = json.loads((Path(tmp) / 'generation.json').read_text())
            self.assertEqual(log['response'], answer)
            self.assertEqual(log['model'], 'explicit-model')
            self.assertIsNone(log['input_tokens'])
            self.assertIsNone(log['estimated_cost'])

    def test_nonlinear_assertion_is_preserved_for_explicit_diagnosis(self):
        source = '(set-logic QF_LIA)\n(declare-const x Int)\n(declare-const y Int)\n(assert (<= (* x y) 20))\n(check-sat)\n'
        actual, notes = pipeline.sanitize_qf_lia_fragment(source)
        self.assertEqual(actual, source)
        self.assertIn('preserved', notes[0])

    def test_default_translation_emits_no_synthetic_unsat_artifact(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, 'run_z3_fragment', side_effect=AssertionError('No artificial negative query may be run')):
            prefix = Path(tmp) / 'case'
            # Test the output boundary directly; generated SAT formulas are preserved.
            files = asyncio.run(pipeline.write_smt_outputs(prefix, '(declare-const x Int)\n(assert (<= x 28))\n(check-sat)', {'status': 'sat'}, None))
            self.assertEqual([row['mode'] for row in files], ['sat'])
            self.assertFalse((Path(tmp) / 'case_unsat.smt2').exists())
            self.assertNotIn('(assert false)', (Path(tmp) / 'case_sat.smt2').read_text())

    def test_blank_negative_condition_cannot_become_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'explicit additional assertions'):
                asyncio.run(pipeline.write_smt_outputs(Path(tmp) / 'case', '(check-sat)', {}, '  '))
            self.assertFalse((Path(tmp) / 'case_unsat.smt2').exists())


if __name__ == '__main__':
    unittest.main()
