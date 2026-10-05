"""Source-alignment outcomes use frozen source judgments, never pipeline status."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from canonical_assertions import build_suite
from canonical_assertion_judging import (
    SOURCE_ALIGNMENT_POLICY, _study_summaries, _write_report, evaluate_packets,
)
from canonical_assertion_rescore import rescore_assessment
from canonical_assertion_repair import repair_assessment
from test_canonical_assertion_repair import CONFIG, correction, fixture


SOURCE = [{"id": "R1", "text": "The battery voltage shall be at most 28 V.",
           "source": {"context": {"scope": "Normal operation.",
                                   "definition": "Voltage is the terminal voltage, expressed in volts.",
                                   "limitations": "Persistence is outside the supported model."}}}]
MODEL = ("package Battery {\n"
         "  attribute voltage : Real;\n"
         "  require constraint { voltage <= 28; }\n"
         "  doc /* Persistence is outside the supported model. */\n"
         "  doc /* Voltage is the terminal voltage, expressed in volts. */\n"
         "}\n")
MODELS = ["first-judge", "second-judge"]


def suite(*, legacy=False, documentation_category=None):
    authored = [{"id": "R1", "assertions": [
        {"id": "BOUND", "statement": "Preserve the inclusive 28 V bound.",
         "category": "boundary", "evidence_requirement": "model",
         "source_basis": [{"source_id": "R1", "quote": "at most 28 V"}]},
        {"id": "DISCLOSURE", "statement": "Disclose the persistence limitation.",
         "category": "unsupported_semantics", "evidence_requirement": "documentation",
         "source_basis": [{"source_id": "R1", "quote": "Persistence is outside the supported model."}]},
    ]}]
    if documentation_category:
        authored[0]["assertions"].append({
            "id": "DEFINITION", "statement": "Preserve the source voltage definition and unit convention.",
            "category": documentation_category, "evidence_requirement": "documentation",
            "source_basis": [{"source_id": "R1", "quote": "Voltage is the terminal voltage, expressed in volts."}]})
    if legacy:
        # Old suites did not permit the frozen evidence_requirement field.
        for assertion in authored[0]["assertions"]:
            assertion.pop("evidence_requirement")
    return build_suite(SOURCE, authored, **({"schema": "sysml_assertions/1"} if legacy else {}))


def response(statuses=None, omit=()):
    def callback(system, prompt, model, directory, call_id):
        fields = json.loads(prompt)
        verdicts = []
        for assertion in fields["assertions"]:
            if assertion["category"] in omit:
                continue
            status = (statuses or {}).get(assertion["category"], "pass")
            line = (5 if assertion["id"] == "DEFINITION" else 4
                    if assertion.get("evidence_requirement") == "documentation" else 3)
            verdicts.append({"id": assertion["id"], "status": status,
                "rationale": "Compared the actual target with the contextualized source.",
                "evidence": [{"start_line": line, "end_line": line}] if status == "pass" else [],
                "counterexample": None if status == "pass" else "The source-boundary obligation is not established."})
        return json.dumps({"requirement_id": fields["target_requirement_id"], "assertions": verdicts})
    return Mock(side_effect=callback)


def assess(directory, *, first=None, second=None, legacy=False, model=MODEL, documentation_category=None):
    return evaluate_packets([{"id": "candidate", "requirements": SOURCE,
                             "fixed_context": None, "sysml": model}],
                            suite(legacy=legacy, documentation_category=documentation_category), MODELS, directory,
                            [first or response(), second or response()])


def alignment(report):
    return report["results"][0]["requirements"][0]["source_alignment"]


class SourceAlignmentTests(unittest.TestCase):
    def test_complete_pass_is_source_judgment_not_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = assess(Path(tmp) / "eval")
        self.assertEqual(alignment(report)["status"], "pass")
        self.assertEqual(alignment(report)["assessment_status"], "complete")
        self.assertEqual(report["source_alignment"]["policy"], SOURCE_ALIGNMENT_POLICY)
        self.assertEqual(report["source_alignment"]["planned"], 1)
        self.assertEqual(report["source_alignment"]["pass_rate"], 1)
        self.assertIn("not formal equivalence", report["source_alignment"]["scope"])

    def test_disclosure_pass_cannot_compensate_missing_obligation(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = assess(Path(tmp) / "eval", first=response({"coverage": "fail"}),
                            second=response({"coverage": "fail"}))
        self.assertEqual(report["joint"]["pass_rate"], 4 / 5)
        self.assertEqual(report["by_category"]["unsupported_semantics"]["pass_rate"], 1)
        self.assertEqual(alignment(report)["status"], "fail")
        self.assertEqual(report["source_alignment"]["pass_rate"], 0)
        self.assertEqual(report["status"], "completed")  # usable assessment, failed preservation

    def test_fidelity_and_coverage_pass_do_not_hide_substantive_defect(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = assess(Path(tmp) / "eval", first=response({"boundary": "fail"}),
                            second=response({"boundary": "fail"}))
        item = alignment(report)
        self.assertEqual(item["fidelity"]["status"], "pass")
        self.assertEqual(item["coverage"]["status"], "pass")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["blockers"], [{"id": "BOUND", "category": "boundary", "status": "fail"}])

    def test_model_assumption_disagreement_blocks_combined_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = assess(Path(tmp) / "eval", first=response({"assumptions": "fail"}))
        self.assertEqual(alignment(report)["status"], "disputed")
        self.assertEqual(alignment(report)["assessment_status"], "complete")
        self.assertEqual(alignment(report)["blockers"][0]["category"], "assumptions")

    def test_documentation_failure_separate_from_model_alignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = assess(Path(tmp) / "eval", first=response({"unsupported_semantics": "fail"}),
                            second=response({"unsupported_semantics": "fail"}))
        self.assertEqual(alignment(report)["status"], "pass")
        self.assertEqual(alignment(report)["documentation_assertion_ids"], ["DISCLOSURE"])
        self.assertEqual(report["by_category"]["unsupported_semantics"]["fail"], 1)
        self.assertEqual(report["joint"]["pass_rate"], 4 / 5)

    def test_documentary_context_and_units_remain_required_source_meaning(self):
        for category in ("context", "unit"):
            with self.subTest(category=category), tempfile.TemporaryDirectory() as tmp:
                report = assess(Path(tmp) / "eval", documentation_category=category,
                                first=response({category: "fail"}), second=response({category: "fail"}))
            item = alignment(report)
            self.assertEqual(item["fidelity"]["status"], "pass")
            self.assertEqual(item["coverage"]["status"], "pass")
            self.assertEqual(item["status"], "fail")
            self.assertIn("DEFINITION", item["required_assertion_ids"])
            self.assertNotIn("DEFINITION", item["required_model_assertion_ids"])
            self.assertNotIn("DISCLOSURE", item["required_assertion_ids"])
            self.assertEqual(item["blockers"], [{"id": "DEFINITION", "category": category, "status": "fail"}])
            self.assertEqual(report["source_alignment"]["documentation_metrics"]["fail"], 1)

    def test_missing_documentary_definition_is_pending_required_not_pending_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = assess(Path(tmp) / "eval", documentation_category="context",
                            first=response(omit=("context",)))
        item = alignment(report)
        self.assertEqual(item["status"], "unreviewed")
        self.assertEqual(item["assessment_status"], "incomplete")
        self.assertEqual(item["pending_model_assertions"], 0)
        self.assertEqual(item["pending_required_assertions"], 1)
        self.assertEqual(item["pending_required_judge_decisions"], 1)
        self.assertEqual(report["source_alignment"]["requirements_with_pending_required_judgments"], 1)
        self.assertEqual(report["source_alignment"]["requirements_with_pending_model_judgments"], 0)

    def test_known_failure_and_missing_judgment_both_remain_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = assess(Path(tmp) / "eval", first=response({"coverage": "fail"}, omit=("fidelity",)),
                            second=response({"coverage": "fail"}))
        item = alignment(report)
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["assessment_status"], "incomplete")
        self.assertEqual(item["pending_model_assertions"], 1)
        self.assertEqual(report["source_alignment"]["requirements_with_pending_model_judgments"], 1)
        self.assertEqual(report["source_alignment"]["pending_model_judge_decisions"], 1)
        self.assertEqual(report["joint"]["unreviewed"], 1)

    def test_unresolved_is_usable_uncertainty_not_success_or_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = assess(Path(tmp) / "eval", first=response({"fidelity": "unresolved"}))
        self.assertEqual(alignment(report)["status"], "unresolved")
        self.assertEqual(alignment(report)["assessment_status"], "complete")
        self.assertEqual(report["source_alignment"]["pending_model_assertions"], 0)

    def test_legacy_fidelity_not_assessed_even_if_every_assertion_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = assess(Path(tmp) / "eval", legacy=True)
        self.assertEqual(report["joint"]["pass_rate"], 1)
        self.assertEqual(alignment(report)["status"], "not_assessed")
        self.assertEqual(alignment(report)["missing_categories"], ["fidelity"])
        self.assertIsNone(report["source_alignment"]["pass_rate"])
        self.assertEqual(report["source_alignment"]["planned"], 1)

    def test_missing_artifact_is_retained_without_synthetic_judge_failure(self):
        first, second = response(), response()
        with tempfile.TemporaryDirectory() as tmp:
            report = assess(Path(tmp) / "eval", first=first, second=second, model="")
        first.assert_not_called(); second.assert_not_called()
        self.assertFalse(alignment(report)["candidate_available"])
        self.assertEqual(alignment(report)["status"], "unreviewed")
        self.assertEqual(report["source_alignment"]["missing_candidates"], 1)
        self.assertEqual(report["source_alignment"]["planned"], 1)
        self.assertEqual(report["joint"]["fail"], 0)
        self.assertEqual(report["joint"]["unreviewed"], 5)

    def test_missing_judge_stays_unreviewed_with_fixed_requirement_denominator(self):
        failed = Mock(side_effect=RuntimeError("No provider response"))
        with tempfile.TemporaryDirectory() as tmp:
            report = assess(Path(tmp) / "eval", first=failed)
        self.assertEqual(alignment(report)["status"], "unreviewed")
        self.assertEqual(report["source_alignment"]["planned"], 1)
        self.assertEqual(report["source_alignment"]["pending_model_judge_decisions"], 4)

    def test_study_summaries_ignore_admission_and_lead_with_primary_vectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "eval"
            report = assess(out, first=response({"fidelity": "fail"}), second=response({"fidelity": "fail"}))
            before = deepcopy(report["results"][0]["requirements"][0]["judges"])
            views = [{"condition": arm, "packet_id": "candidate", "repetition": 1,
                      "artifact_arm": arm, "admission": "admitted_consistent_encoding",
                      "source_review": "passed", "solver_status": "sat"} for arm in ("A", "B", "C")]
            _study_summaries(report, views)
            _write_report(out, report)
            text = (out / "report.md").read_text()
        self.assertEqual(before, report["results"][0]["requirements"][0]["judges"])
        for arm in ("A", "B", "C"):
            self.assertEqual(report["by_condition"][arm]["source_alignment"]["fail"], 1)
            self.assertEqual(report["by_condition"][arm]["source_alignment"]["planned"], 1)
        self.assertLess(text.index("Primary requirement outcomes"), text.index("Secondary assertion diagnostics"))
        self.assertIn("| Condition | Fidelity both pass | Coverage both pass |", text)

    def test_rescore_and_targeted_repair_recompute_additive_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, original = fixture(root)
            original_bytes = (old / "judgments.json").read_bytes()
            rescored = rescore_assessment(old, root / "rescore")
            self.assertEqual(alignment(rescored)["status"], "unreviewed")
            repaired = repair_assessment(old, root / "repair", [correction(), correction()], CONFIG)
            self.assertEqual(alignment(repaired)["status"], "pass")
            self.assertEqual(repaired["source_alignment"]["pass"], 1)
            self.assertEqual(repaired["source_alignment"]["planned"], 1)
            self.assertEqual(original_bytes, (old / "judgments.json").read_bytes())
            self.assertEqual(original["joint"]["unreviewed"], 1)


if __name__ == "__main__":
    unittest.main()
