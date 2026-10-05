from copy import deepcopy
import json
import unittest
from canonical_assertions import validate_verdict
from test_canonical_assertion_judging import MODEL, suite, verdict


class CitationLineTests(unittest.TestCase):
    def response(self):
        requirement = suite()['requirements'][0]
        prompt = json.dumps({'target_requirement_id': requirement['id'], 'assertions': requirement['assertions'],
                             'sysml_with_line_numbers': ''.join(f'{n}: {line}' for n, line in enumerate(MODEL.splitlines(keepends=True), 1))})
        raw = verdict(prompt)
        for row in raw['assertions']:
            for span in row['evidence']:
                span.pop('quote')
        return requirement, raw

    def test_line_selectors_materialize_exact_original_without_invented_quote(self):
        requirement, raw = self.response()
        original = deepcopy(raw)
        result = validate_verdict(raw, requirement, MODEL)
        for row in result['assertions']:
            evidence = row['evidence'][0]
            self.assertEqual(evidence['quote'], MODEL.splitlines(keepends=True)[2])
            self.assertEqual(evidence['normalization'], 'source_lines')
            self.assertNotIn('raw_quote', evidence)
        self.assertEqual(raw, original)

    def test_out_of_range_selector_is_rejected(self):
        requirement, raw = self.response()
        raw['assertions'][0]['evidence'][0].update(start_line=99, end_line=99)
        with self.assertRaises(ValueError):
            validate_verdict(raw, requirement, MODEL)

    def test_comment_only_selector_cannot_pass_executable_assertions(self):
        requirement, raw = self.response()
        model = 'package Candidate {\n    doc /* voltage must be at most 28 V */\n}\n'
        for row in raw['assertions']:
            row['evidence'][0].update(start_line=2, end_line=2)
        with self.assertRaisesRegex(ValueError, 'actual code evidence'):
            validate_verdict(raw, requirement, model)


if __name__ == '__main__':
    unittest.main()
