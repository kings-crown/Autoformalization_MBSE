"""Behavioral regression cases for the local formalization boundary."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import review_profile as profile


class ProfileTests(unittest.TestCase):
    def tlr(self, *clauses):
        source = json.dumps([{'id': f'R{i}', 'text': text} for i, text in enumerate(clauses)])
        return profile.interpret(profile.parse_requirements(source, 'json', 'Test requirements'))

    def analysis(self, tlr):
        if not shutil.which('z3'):
            self.skipTest('Real Z3 executable unavailable')
        with tempfile.TemporaryDirectory() as directory:
            return profile.analyze(tlr, Path(directory))

    def test_source_provenance_and_decimals_are_preserved(self):
        text = 'id,requirement,owner,source\nR1,"The battery shall have a voltage of at least 10.5 V.",Electrical,Spec section3\n'
        rows = profile.parse_requirements(text, 'csv', 'Electrical spec')
        self.assertEqual(rows[0]['owner'], 'Electrical')
        self.assertEqual(rows[0]['source']['location'], 'Spec section3')
        self.assertEqual(rows[0]['source']['line_start'], 2)
        tlr = profile.interpret(rows)
        self.assertEqual(tlr['requirements'][0]['value'], '10.5')
        with tempfile.TemporaryDirectory() as directory:
            contract = profile.existing_contract(tlr, profile.digest(text), Path(directory) / 'source.csv')
        self.assertEqual(contract['requirements'][0]['ranges'][0]['lower'], '10.5')
        self.assertTrue(contract['typecheck']['ok'])

    def test_long_decimal_is_exact_and_not_rounded(self):
        value = '1.0000000000000000000000000001'
        tlr = self.tlr(f'battery.voltage >= {value} V', 'battery.voltage <= 1 V')
        self.assertEqual(tlr['requirements'][0]['value'], value)
        self.assertEqual(self.analysis(tlr)['status'], 'unsat')

    def test_unit_conversion_shares_quantity(self):
        tlr = self.tlr('battery.voltage >= 10500 mV', 'battery.voltage <= 10 V')
        self.assertEqual(tlr['requirements'][0]['value'], '10.5')
        self.assertEqual(len(tlr['symbols']), 1)
        self.assertEqual(self.analysis(tlr)['unsat_core'], ['R0', 'R1'])

    def test_si_symbols_are_case_sensitive_but_words_are_not(self):
        tlr = self.tlr('battery.voltage >= 1 MV', 'battery.current >= 1 MA', 'battery.voltage >= 1 VOLTS')
        self.assertEqual([r['status'] for r in tlr['requirements']], ['unsupported', 'unsupported', 'supported'])
        self.assertEqual(tlr['requirements'][2]['unit'], 'V')

    def test_required_response_conflict_maps_real_core(self):
        tlr = self.tlr('After each START command, the controller shall produce the response within 5 seconds.',
                       'After each START command, the controller shall produce the response no earlier than 8 seconds.')
        result = self.analysis(tlr)
        self.assertEqual(result['status'], 'unsat')
        self.assertEqual(set(result['unsat_core']), {'R0', 'R1'})
        self.assertTrue(any('required for each' in x for x in result['assumptions']))

    def test_different_event_anchors_do_not_create_false_conflict(self):
        tlr = self.tlr('After each RELEASE command, the controller shall produce the response within 5 seconds.',
                       'After each START command, the controller shall produce the response no earlier than 8 seconds.')
        self.assertEqual(len(tlr['symbols']), 2)
        result = self.analysis(tlr)
        self.assertEqual(result['status'], 'sat')
        self.assertEqual(len(result['witness']), 2)

    def test_closed_and_open_boundaries_differ(self):
        self.assertEqual(self.analysis(self.tlr('battery.voltage >= 5 V', 'battery.voltage <= 5 V'))['status'], 'sat')
        self.assertEqual(self.analysis(self.tlr('battery.voltage > 5 V', 'battery.voltage <= 5 V'))['status'], 'unsat')

    def test_unsupported_and_conditions_cannot_disappear_into_sat(self):
        clauses = ['battery.voltage >= 5 V', 'The controller shall respond safely.',
                   'The battery shall have a voltage of at most 12 V when charging.']
        tlr = self.tlr(*clauses)
        self.assertEqual([r['text'] for r in tlr['requirements']], clauses)
        result = self.analysis(tlr)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['unsupported_ids'], ['R1', 'R2'])
        self.assertEqual(result['checked_ids'], ['R0'])

    def test_incompatible_dimensions_are_not_independent_variables(self):
        tlr = self.tlr('battery.voltage >= 5 V', 'battery.voltage <= 8 kg')
        self.assertTrue(all(r['status'] == 'unsupported' for r in tlr['requirements']))
        self.assertEqual(self.analysis(tlr)['status'], 'not_run')

    def test_timing_word_does_not_become_arbitrary_scalar_bound(self):
        self.assertEqual(self.tlr('The battery shall have a voltage within 5 V')['requirements'][0]['status'], 'unsupported')

    def test_input_rejects_duplicate_ids_and_malformed_rows(self):
        for text, fmt in [('id,text\nR1,first\nR1,second', 'csv'),
                          ('id,text\nR1,first,extra', 'csv'), ('{"requirements":42}', 'json')]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                profile.parse_requirements(text, fmt, 'Input')

    def test_unsupported_only_has_no_solver_success(self):
        result = self.analysis(self.tlr('The system shall be reliable.'))
        self.assertEqual(result['status'], 'not_run')
        self.assertEqual(result['checked_ids'], [])


if __name__ == '__main__':
    unittest.main()
