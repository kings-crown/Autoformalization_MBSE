"""Offline assessment of separate feedback-study arms and legacy shared artifacts."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from canonical_abstractions import POLICY
from canonical_assertions import build_suite
from canonical_assertion_judging import evaluate_study
from canonical_cli import read_json, write_json
from canonical_judging import judge_study, packets_from_study

SOURCES = [{"id": "R1", "text": "Voltage shall be at most 28 V.",
            "source": {"context": {"scope": "Battery terminal voltage in normal operation."}}}]
CONTEXT = {"variables": [{"name": "voltage", "type": "Real", "unit": "V"}], "background": []}
MODEL = "package Candidate {\n    attribute voltage : Real;\n    require constraint { voltage <= 28; }\n}\n"


def make_study(directory, models=None, legacy=False, repetitions=1):
    directory.mkdir()
    arms = ("A", "BC") if legacy else ("A", "B", "C")
    if models is None:
        models = {arm: MODEL.replace("28", str(28 + index)) for index, arm in enumerate(arms)}
    rows = []
    for rep in range(1, repetitions + 1):
        row = {"repetition": rep}
        for arm in arms:
            row[arm] = {"status": "completed" if models.get(arm) else "failed",
                        "admission": "admitted_consistent_encoding", "private_feedback": "HIDDEN_SOLVER_FEEDBACK"}
            if models.get(arm) is not None:
                path = directory / f"rep-{rep:03d}" / arm
                path.mkdir(parents=True)
                (path / "model.sysml").write_bytes(models[arm].encode())
        if not legacy:
            row["initial"] = {"status": "completed", "private_feedback": "HIDDEN_INITIAL"}
            initial = directory / f"rep-{rep:03d}" / "initial"
            initial.mkdir(parents=True)
            (initial / "model.sysml").write_text("package InitialMustNotBeJudged {}")
        rows.append(row)
    write_json(directory / "sources.json", SOURCES)
    write_json(directory / "study.json", {"rows": rows})
    write_json(directory / "study_configuration.json", {
        "context": CONTEXT, "model": "HIDDEN_GENERATOR", "abstraction_policy": POLICY,
        "feedback_repairs": 2, "feedback_policy": "HIDDEN_POLICY"})
    return directory


def suite():
    return build_suite(SOURCES, [{"id": "R1", "assertions": [{
        "id": "R1-bound", "statement": "The inclusive voltage bound is 28 V.", "category": "boundary",
        "source_basis": [{"source_id": "R1", "quote": "at most 28 V"}]}]}], CONTEXT, "Offline fixture", schema="sysml_assertions/1")


def assertion_response(system, prompt, model, directory, call_id):
    fields = json.loads(prompt)
    state = "fail" if "voltage <= 30;" in fields["sysml_with_line_numbers"] else "pass"
    return json.dumps({"requirement_id": fields["target_requirement_id"], "assertions": [
        {"id": row["id"], "status": state, "rationale": "Compare the source boundary to the actual constraint.",
         "evidence": [{"start_line": 3, "end_line": 3}],
         "counterexample": None if state == "pass" else "Voltage 29 V violates the source limit."}
        for row in fields["assertions"]]})


def fidelity_response(system, prompt, model, directory, call_id):
    fields = json.loads(prompt)
    defective = "voltage <= 30;" in fields["sysml"]
    return json.dumps({"requirements": [{"id": "R1", "label": "incorrect" if defective else "faithful",
        "justification": "Compare the source boundary to the actual constraint.", "omitted_obligations": [],
        "invented_assumptions": [], "unsupported_semantics": [],
        "distinguishing_scenario": "Voltage 29 V." if defective else None}]})


class FeedbackJudgingTests(unittest.TestCase):
    def test_separate_packets_use_actual_arms_exclude_initial_and_admit_only_C(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_study(Path(tmp) / "study")
            packets, views, sources = packets_from_study(run)
        self.assertEqual(sources, SOURCES)
        self.assertEqual(len(packets), 3)
        self.assertEqual([v["condition"] for v in views], ["A", "B", "C"])
        self.assertEqual([v["artifact_arm"] for v in views], ["A", "B", "C"])
        self.assertEqual([v["admission"] for v in views], ["not_assessed", "not_assessed", "admitted_consistent_encoding"])
        for index, packet in enumerate(packets):
            self.assertIn(f"voltage <= {28 + index};", packet["sysml"])
            self.assertEqual(packet["fixed_context"], CONTEXT)
            self.assertEqual(set(packet), {"id", "requirements", "sysml", "fixed_context"})
            self.assertNotIn("InitialMustNotBeJudged", packet["sysml"])

    def test_exact_identical_arms_and_repetitions_reuse_one_blind_packet(self):
        callback = Mock(side_effect=assertion_response)
        with tempfile.TemporaryDirectory() as tmp:
            run = make_study(Path(tmp) / "study", {arm: MODEL for arm in ("A", "B", "C")}, repetitions=2)
            report = evaluate_study(run, suite(), ["one", "two"], Path(tmp) / "judges", [callback, callback])
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(report["summary"]["unique_packets"], 1)
        self.assertEqual(len({row["packet_id"] for row in report["observations"]}), 1)
        for condition in ("A", "B", "C"):
            self.assertEqual(report["by_condition"][condition]["joint"]["planned"], 6)
        for call in callback.call_args_list:
            prompt = json.loads(call.args[1])
            self.assertEqual(set(prompt), {"target_requirement_id", "source_packet", "fixed_context", "assertions", "abstraction_policy", "sysml_with_line_numbers"})
            self.assertEqual(prompt["source_packet"], SOURCES)
            self.assertEqual(prompt["fixed_context"], CONTEXT)
            for private in ("HIDDEN_", "artifact_arm", "feedback_repairs", "admission", "InitialMustNotBeJudged"):
                self.assertNotIn(private, call.args[1])

    def test_distinct_B_and_C_assessments_are_not_reused(self):
        callback = Mock(side_effect=assertion_response)
        with tempfile.TemporaryDirectory() as tmp:
            run = make_study(Path(tmp) / "study")
            report = evaluate_study(run, suite(), ["one", "two"], Path(tmp) / "judges", [callback, callback])
        self.assertEqual(callback.call_count, 6)
        self.assertEqual(report["by_condition"]["B"]["joint"]["pass"], 3)
        self.assertEqual(report["by_condition"]["C"]["joint"]["fail"], 3)
        self.assertEqual(report["by_condition"]["C"]["admitted"]["requirements_with_jointly_failed_assertions"], 1)
        self.assertNotIn("admitted", report["by_condition"]["B"])

    def test_legacy_fidelity_judge_accepts_separate_feedback_arms(self):
        callback = Mock(side_effect=fidelity_response)
        with tempfile.TemporaryDirectory() as tmp:
            run = make_study(Path(tmp) / "study")
            report = judge_study(run, ["one", "two"], Path(tmp) / "judges", callback)
        self.assertEqual(callback.call_count, 6)
        self.assertEqual(report["by_condition"]["B"]["jointly_faithful"], 1)
        self.assertEqual(report["by_condition"]["C"]["admitted_jointly_defective"], 1)
        for call in callback.call_args_list:
            self.assertNotIn("HIDDEN_", call.args[1])
            self.assertNotIn("artifact_arm", json.loads(call.args[1]))

    def test_missing_separate_arms_retain_distinct_unreviewed_denominators(self):
        callback = Mock(side_effect=assertion_response)
        with tempfile.TemporaryDirectory() as tmp:
            run = make_study(Path(tmp) / "study", {"A": MODEL, "B": None, "C": " \n"})
            packets, views, _ = packets_from_study(run)
            self.assertEqual(len(packets), 1)
            self.assertIsNone(views[1]["packet_id"])
            self.assertIsNone(views[2]["packet_id"])
            report = evaluate_study(run, suite(), ["one", "two"], Path(tmp) / "judges", [callback, callback])
        self.assertEqual(callback.call_count, 2)
        by_arm = {row["condition"]: row for row in report["observations"]}
        self.assertEqual(by_arm["B"]["packet_id"], "missing-001-B")
        self.assertEqual(by_arm["C"]["packet_id"], "missing-001-C")
        for condition in ("B", "C"):
            self.assertEqual(report["by_condition"][condition]["joint"]["unreviewed"], 3)
            self.assertEqual(report["by_condition"][condition]["joint"]["planned"], 3)

    def test_missing_arm_record_does_not_load_a_stale_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_study(Path(tmp) / "study")
            payload = read_json(run / "study.json")
            del payload["rows"][0]["B"]
            write_json(run / "study.json", payload)
            packets, views, _ = packets_from_study(run)
        self.assertEqual(len(packets), 2)
        self.assertIsNone(next(v for v in views if v["condition"] == "B")["packet_id"])

    def test_compilation_failure_does_not_hide_substantive_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_study(Path(tmp) / "study")
            payload = read_json(run / "study.json")
            payload["rows"][0]["A"].update(status="failed", compilation={"status": "failed"})
            write_json(run / "study.json", payload)
            packets, views, _ = packets_from_study(run)
        self.assertEqual(len(packets), 3)
        self.assertIsNotNone(views[0]["packet_id"])

    def test_line_ending_differences_are_not_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = make_study(Path(tmp) / "study", {"A": MODEL, "B": MODEL, "C": MODEL.replace("\n", "\r\n")})
            packets, views, _ = packets_from_study(run)
        self.assertEqual(len(packets), 2)
        self.assertEqual(views[0]["packet_id"], views[1]["packet_id"])
        self.assertNotEqual(views[1]["packet_id"], views[2]["packet_id"])
        self.assertIn("\r\n", packets[1]["sysml"])

    def test_legacy_missing_BC_keeps_one_shared_empty_packet(self):
        callback = Mock(side_effect=assertion_response)
        with tempfile.TemporaryDirectory() as tmp:
            run = make_study(Path(tmp) / "study", {"A": MODEL, "BC": None}, legacy=True)
            report = evaluate_study(run, suite(), ["one", "two"], Path(tmp) / "judges", [callback, callback])
        self.assertEqual(callback.call_count, 2)
        structured = [r for r in report["observations"] if r["condition"] in {"B", "C"}]
        self.assertEqual({r["packet_id"] for r in structured}, {"missing-001-BC"})
        self.assertTrue(all(r["joint"]["unreviewed"] == 3 for r in structured))


if __name__ == "__main__":
    unittest.main()
