"""Conversion success must never manufacture final source-to-SysML fidelity."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from canonical_cli import SYSML_INSTRUCTIONS, run_candidate, run_study
from canonical_tlr import validate_tlr
from source_review_support import pass_source_review, review_response


def fixture(upper="28"):
    sources = [
        {"id": "R1", "text": "Battery voltage shall be at most 28 V."},
        {"id": "R2", "text": "Battery voltage shall be at least 20 V."},
    ]
    candidate = {
        "schema": "mbse_tlr/1", "abstraction_policy": "mbse_abstraction/1",
        "variables": [{"name": "voltage", "type": "Real", "unit": "V",
                       "description": "Voltage measured at the battery terminals."}],
        "assumptions": [],
        "requirements": [
            {"id": source["id"], "status": "supported",
             "formula": {"op": op, "args": [{"var": "voltage"}, {"value": value, "unit": "V"}]},
             "abstraction": {"kind": "state_constraint", "meaning": source["text"],
                             "scope": "Battery terminal voltage at one observation.", "limitations": []}}
            for source, op, value in zip(sources, ("<=", ">="), (upper, "20"))
        ],
    }
    return sources, validate_tlr(candidate, sources)


def rejecting_review(reject_ids):
    def review(system, prompt, model, directory, call_id):
        response = review_response(json.loads(prompt))
        for row in response["requirements"]:
            if row["id"] in reject_ids:
                row.update(disposition="revise", reason="The generated bound does not preserve the source endpoint.")
                for dimension in row["dimensions"]:
                    if dimension["dimension"] == "precision":
                        dimension.update(status="fail", explanation=row["reason"])
        return json.dumps(response)
    return review


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


class CanonicalAssuranceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('z3'), 'Requires real Z3')
    def test_wrong_source_bound_is_not_fidelity_even_when_encoding_is_admitted(self):
        sources, tlr = fixture('30')
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_cli._compile', return_value={'status':'passed'}):
            result = run_candidate(sources, Path(tmp) / 'C', 'C', tlr=tlr)
            self.assertEqual(result['analysis']['consistency_status'], 'sat')
            self.assertEqual(result['admission'], 'admitted_consistent_encoding')
            self.assertEqual(result['assurance']['formal_coverage']['emitted'], 2)
            self.assertEqual(result['assurance']['source_to_rule']['status'], 'not_run')
            self.assertEqual(result['assurance']['source_to_sysml']['status'], 'not_assessed')
            self.assertEqual(result['source_fidelity'], 'not_assessed')

    def test_unsupported_draft_retains_source_without_claiming_executable_coverage(self):
        sources, tlr = fixture()
        tlr['requirements'] = [{'id':s['id'],'status':'unsupported','reason_code':'profile_limit',
            'reason':'Declared abstraction cannot represent the required obligation.'} for s in sources]
        with tempfile.TemporaryDirectory() as tmp:
            result = run_candidate(sources, Path(tmp)/'B', 'B', tlr=tlr, compile_model=False)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['assurance']['formal_coverage']['emitted'], 0)
            self.assertEqual(result['assurance']['source_to_sysml']['status'], 'not_assessed')
            self.assertTrue(result['model_file'])

    def test_failed_shared_seed_preserves_all_final_assessment_slots(self):
        sources, _ = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            result = run_study(sources, Path(tmp)/'study', repetitions=1,
                a_sysml='package Demo {}', generator=Mock(return_value='invalid'),
                compile_model=False, feedback_repairs=1)
            self.assertEqual(result['summary']['initial_generation_failures'], 1)
            for arm in ('B','C'):
                branch = result['rows'][0][arm]
                self.assertEqual(branch['status'], 'failed')
                self.assertFalse(branch['assurance']['model_available'])
                self.assertEqual(branch['assurance']['source_to_sysml']['status'], 'not_assessed')

    def test_historical_gate_report_keeps_its_historical_eligibility_scope(self):
        from canonical_assurance import conversion_assurance
        sources, tlr = fixture()
        report = conversion_assurance({'tlr':tlr,'model_file':'model.sysml',
            'source_review':{'status':'partial','eligible_ids':['R2'],'complete':False}}, sources)
        self.assertEqual(report['formal_coverage']['emitted_requirement_ids'], ['R2'])
        self.assertFalse(report['source_to_rule']['complete'])


if __name__ == '__main__': unittest.main()
