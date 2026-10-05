"""Independent LLM-judging mechanics; all model responses are offline test data."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from canonical_cli import main, read_json, write_json
from canonical_abstractions import POLICY
from canonical_judging import _assessment, judge_packets, judge_study, packets_from_study


def assessment(labels):
    return {"requirements": [
        {"id": rid, "label": label, "justification": "Compared the source obligation and its represented constraint.",
         "omitted_obligations": [], "invented_assumptions": [], "unsupported_semantics": [],
         "distinguishing_scenario": None}
        for rid, label in labels.items()]}


def packet(pid="sample", expected=None):
    value = {"id": pid, "requirements": [{"id": "R1", "text": "Voltage shall not exceed 28 V.",
                                          "source": {"document": "source.csv", "location": "line 2"}}],
             "sysml": "package Candidate { doc /* Supplied test artifact. */ }"}
    if expected is not None:
        value["expected_labels"] = {"R1": expected}
    return value


def study_fixture(directory, repetitions=1, identical=False, context=None):
    sources = [{"id": "R" + str(i), "text": "Source obligation " + str(i)} for i in range(1, 6)]
    directory.mkdir()
    rows = []
    for rep in range(1, repetitions + 1):
        row = {"repetition": rep}
        for arm in ("A", "BC"):
            output = directory / f"rep-{rep:03d}" / arm
            output.mkdir(parents=True)
            text = "package SameCandidate {}" if identical else f"package Artifact{arm} {{}}"
            (output / "model.sysml").write_text(text)
            row[arm] = {"status": "completed", "admission": "admitted_consistent_encoding" if arm == "BC" else "not_assessed"}
        rows.append(row)
    write_json(directory / "study.json", {"rows": rows})
    write_json(directory / "sources.json", sources)
    write_json(directory / "study_configuration.json", {"model": "HIDDEN_GENERATOR", "context": context,
                                                       "conditions": ["A", "B", "C"], "semantic_repairs": 0})
    return sources


class CanonicalJudgingTests(unittest.TestCase):
    def test_prompts_are_independent_blind_and_exclude_reference_answers(self):
        data = packet(expected="incorrect")
        data["requirements"][0]["condition"] = "HIDDEN_CONDITION"
        data["requirements"][0]["solver_result"] = "HIDDEN_SOLVER_RESULT"
        data["fixed_context"] = {"variables": [{"name": "voltage", "type": "Real", "unit": "V", "bounds": {"lower": "0"}}], "background": []}
        original = deepcopy(data)
        calls = []
        def generate(system, prompt, model, directory, call_id):
            calls.append((system, prompt, model, directory, call_id))
            return json.dumps(assessment({"R1": "incorrect" if model == "judge-1" else "faithful"}))
        with tempfile.TemporaryDirectory() as tmp:
            report = judge_packets([data], ["judge-1", "judge-2"], Path(tmp) / "judged", generate)
        self.assertEqual(data, original)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertNotEqual(calls[0][3], calls[1][3])
        prompt = json.loads(calls[0][1])
        self.assertEqual(set(prompt), {"requirements", "sysml", "fixed_context", "abstraction_policy"})
        self.assertEqual(prompt["abstraction_policy"], POLICY)
        self.assertEqual(prompt["fixed_context"], data["fixed_context"])
        self.assertNotIn("expected_labels", prompt)
        self.assertNotIn("HIDDEN_CONDITION", calls[0][1])
        self.assertNotIn("HIDDEN_SOLVER_RESULT", calls[0][1])
        self.assertEqual([j["assessment"]["requirements"][0]["label"] for j in report["results"][0]["judges"]], ["incorrect", "faithful"])
        self.assertIn("no human-validated ground truth", report["claim"])

    def test_study_deduplicates_exact_packets_and_reuses_BC_judgments(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "study"
            sources = study_fixture(directory, repetitions=2, identical=True)
            generator = Mock(return_value=json.dumps(assessment({s["id"]: "faithful" for s in sources})))
            report = judge_study(directory, ["same-model", "same-model"], Path(tmp) / "judgments", generator)
        self.assertEqual(generator.call_count, 2)
        self.assertEqual(report["summary"]["unique_packets"], 1)
        self.assertEqual(report["summary"]["planned_calls"], 2)
        self.assertEqual(len(report["observations"]), 30)
        for condition in ("A", "B", "C"):
            self.assertEqual(report["by_condition"][condition]["jointly_faithful"], 10)
        self.assertEqual(report["by_condition"]["C"]["admitted_jointly_faithful"], 10)
        bc = [r for r in report["observations"] if r["condition"] in {"B", "C"}]
        self.assertEqual(len({r["packet_id"] for r in bc}), 1)
        self.assertTrue(all(r["judge_labels"] == ["faithful", "faithful"] for r in bc))

    def test_study_preserves_disagreements_and_partitions_uncertainty(self):
        def generate(system, prompt, model, directory, call_id):
            labels = {"R1": "faithful", "R2": "faithful", "R3": "unresolved", "R4": "incorrect", "R5": "faithful"} if model == "one" else {
                "R1": "faithful", "R2": "incorrect", "R3": "faithful", "R4": "partially_faithful", "R5": "faithful"}
            return json.dumps(assessment(labels))
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "study"
            study_fixture(directory)
            report = judge_study(directory, ["one", "two"], Path(tmp) / "judgments", generate)
        for condition in ("A", "B", "C"):
            counts = report["by_condition"][condition]
            self.assertEqual(counts["jointly_faithful"], 2)
            self.assertEqual(counts["jointly_defective"], 1)
            self.assertEqual(counts["disputed"], 1)
            self.assertEqual(counts["unresolved"], 1)
            self.assertEqual(counts["unreviewed"], 0)
            self.assertEqual(counts["label_disagreements"], 3)
            self.assertEqual(sum(counts[k] for k in ("jointly_faithful", "jointly_defective", "disputed", "unresolved", "unreviewed")), counts["planned_source_observations"])

    def test_false_assurance_counts_admitted_defects_and_keeps_uncertainty(self):
        calls = []
        def generate(system, prompt, model, directory, call_id):
            fields = json.loads(prompt)
            calls.append((fields["sysml"], model))
            if fields["sysml"] == "package MissingJudgment {}" and model == "two":
                return "invalid response"
            labels = {"R1": "incorrect", "R2": "faithful", "R3": "faithful" if model == "one" else "incorrect",
                      "R4": "unresolved", "R5": "faithful"}
            return json.dumps(assessment(labels))
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "study"
            study_fixture(directory, repetitions=3)
            study = read_json(directory / "study.json")
            study["rows"][1]["BC"]["admission"] = "withheld"
            write_json(directory / "study.json", study)
            (directory / "rep-003" / "BC" / "model.sysml").write_text("package MissingJudgment {}")
            report = judge_study(directory, ["one", "two"], Path(tmp) / "judgments", generate)
        self.assertEqual(len(calls), 6)  # A, shared B/C, and incomplete B/C packet; two judges each.
        counts = report["by_condition"]["C"]
        self.assertEqual(counts["planned_source_observations"], 15)
        self.assertEqual(counts["admitted_source_observations"], 10)
        self.assertEqual(counts["admitted_complete_judgments"], 5)
        self.assertEqual(counts["admitted_jointly_defective"], 1)
        self.assertEqual(counts["admitted_jointly_faithful"], 2)
        self.assertEqual(counts["admitted_disputed"], 1)
        self.assertEqual(counts["admitted_unresolved"], 1)
        self.assertEqual(counts["admitted_unreviewed"], 5)
        metric = counts["llm_assessed_false_assurance"]
        self.assertEqual((metric["numerator"], metric["denominator"], metric["rate"]), (1, 5, 0.2))
        self.assertNotIn("llm_assessed_false_assurance", report["by_condition"]["B"])
        # The withheld second repetition retains its defect observation without entering the rate.
        self.assertTrue(any(r["repetition"] == 2 and r["condition"] == "C" and r["jointly_defective"]
                            and r["admission"] == "withheld" for r in report["observations"]))
        for rep in (1, 2, 3):
            b = [r["judge_labels"] for r in report["observations"] if r["repetition"] == rep and r["condition"] == "B"]
            c = [r["judge_labels"] for r in report["observations"] if r["repetition"] == rep and r["condition"] == "C"]
            self.assertEqual(b, c)

    def test_study_passes_fixed_input_context_without_generator_metadata(self):
        context = {"variables": [{"name": "voltage", "type": "Real", "unit": "mV", "bounds": {"lower": "0"}}], "background": []}
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "study"
            sources = study_fixture(directory, context=context)
            packets, views, _ = packets_from_study(directory)
            self.assertTrue(all(p["fixed_context"]["variables"][0]["unit"] == "V" for p in packets))
            self.assertTrue(all("condition" not in p and "model" not in p for p in packets))
            generator = Mock(return_value=json.dumps(assessment({s["id"]: "faithful" for s in sources})))
            judge_study(directory, ["one", "two"], Path(tmp) / "judgments", generator)
        for call in generator.call_args_list:
            prompt = json.loads(call.args[1])
            self.assertIn("fixed_context", prompt)
            self.assertNotIn("HIDDEN_GENERATOR", call.args[1])
            self.assertEqual(set(prompt), {"requirements", "sysml", "fixed_context", "abstraction_policy"})
            self.assertEqual(prompt["abstraction_policy"], POLICY)
        self.assertEqual(len(views), 3)

    def test_generation_policy_alignment_is_recorded_without_unblinding_judges(self):
        cases = [(None, "legacy_generation_unclassified"),
                 (POLICY, "matched"),
                 ({"version": "OLD_GENERATION_ONLY_POLICY"}, "different_policy")]
        for generation_policy, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp) / "study"
                rows = study_fixture(directory)
                cfg = read_json(directory / "study_configuration.json")
                if generation_policy is not None:
                    cfg["abstraction_policy"] = generation_policy
                write_json(directory / "study_configuration.json", cfg)
                generator = Mock(return_value=json.dumps(assessment({s["id"]: "faithful" for s in rows})))
                out = Path(tmp) / "judgments"
                report = judge_study(directory, ["one", "two"], out, generator)
                alignment = report["policy_alignment"]
                self.assertEqual(alignment["status"], expected)
                self.assertEqual(alignment["generation_policy"], generation_policy)
                self.assertEqual(alignment["assessment_policy"], POLICY)
                self.assertEqual(read_json(out / "configuration.json")["policy_alignment"], alignment)
                self.assertEqual(read_json(out / "judgments.json")["policy_alignment"], alignment)
                for call in generator.call_args_list:
                    self.assertNotIn("OLD_GENERATION_ONLY_POLICY", call.args[1])
                    self.assertNotIn("policy_alignment", call.args[1])
                    self.assertEqual(json.loads(call.args[1])["abstraction_policy"], POLICY)

    def test_empty_malformed_and_incomplete_judge_responses_cannot_pass(self):
        valid = assessment({"R1": "faithful"})
        missing = deepcopy(valid)
        del missing["requirements"][0]["omitted_obligations"]
        contradictory = deepcopy(valid)
        contradictory["requirements"][0]["omitted_obligations"] = ["The voltage bound is absent."]
        wrong_id = deepcopy(valid)
        wrong_id["requirements"][0]["id"] = ["R1"]
        wrong_label = deepcopy(valid)
        wrong_label["requirements"][0]["label"] = []
        responses = ["", "not JSON", "{}", '{"requirements":[]}', json.dumps(missing), json.dumps(contradictory),
                     json.dumps(wrong_id), json.dumps(wrong_label)]
        for response in responses:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "judged"
                report = judge_packets([packet(expected="faithful")], ["one", "two"], output, Mock(return_value=response))
                self.assertEqual(report["summary"]["failed_judgments"], 2)
                self.assertEqual(report["summary"]["completed_requirement_judgments"], 0)
                self.assertTrue(all(j["status"] == "failed" and "assessment" not in j for j in report["results"][0]["judges"]))
                self.assertEqual((output / "sample" / "judge-1" / "response.txt").read_text(), response)
                self.assertTrue(all(s["failed_labels"] == 1 for s in report["calibration_summary"]))

    def test_calibration_keeps_failure_denominators_and_unresolved_controls(self):
        def generate(system, prompt, model, directory, call_id):
            if model == "one":
                return json.dumps(assessment({"R1": "incorrect"}))
            return "not JSON" if directory.parent.name == "defect" else json.dumps(assessment({"R1": "unresolved"}))
        with tempfile.TemporaryDirectory() as tmp:
            report = judge_packets([packet("defect", "incorrect"), packet("paraphrase", "faithful")],
                                   ["one", "two"], Path(tmp) / "judgments", generate)
        first, second = report["calibration_summary"]
        self.assertEqual((first["defects_detected"], first["false_alarms"]), (1, 1))
        self.assertEqual(first["detection_yield_planned"], 1)
        self.assertEqual(first["false_alarm_rate_completed"], 1)
        self.assertEqual(first["faithful_acceptance_yield_planned"], 0)
        self.assertEqual(second["planned_labels"], 2)
        self.assertEqual(second["completed_labels"], 1)
        self.assertEqual(second["failed_labels"], 1)
        self.assertEqual(second["defect_controls_planned"], 1)
        self.assertEqual(second["defect_controls_failed"], 1)
        self.assertEqual(second["detection_yield_planned"], 0)
        self.assertIsNone(second["sensitivity_completed"])
        self.assertEqual(second["faithful_controls_unresolved"], 1)
        self.assertEqual(second["faithful_acceptance_yield_planned"], 0)
        self.assertIsNone(second["false_alarm_rate_definitive"])
        failed = [r for r in report["calibration"] if r["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIsNone(failed[0]["observed"])
        self.assertIsNone(failed[0]["label_match"])

    def test_invalid_packets_fail_before_any_model_calls(self):
        variations = []
        for value in (None, [], 12, ""):
            changed = packet()
            changed["requirements"][0]["id"] = value
            variations.append(changed)
        for value in (None, "", "   "):
            changed = packet()
            changed["requirements"][0]["text"] = value
            variations.append(changed)
        changed = packet()
        changed["expected_labels"] = {"R1": []}
        variations.append(changed)
        changed = packet()
        changed["fixed_context"] = {"variables": [], "background": []}
        variations.append(changed)
        for data in variations:
            with self.subTest(packet=data), tempfile.TemporaryDirectory() as tmp:
                generator = Mock(side_effect=AssertionError("Validation must precede calls"))
                with self.assertRaises(ValueError):
                    judge_packets([data], ["one", "two"], Path(tmp) / "output", generator)
                generator.assert_not_called()
                self.assertFalse((Path(tmp) / "output").exists())

    def test_missing_candidates_are_unreviewed_without_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "study"
            study_fixture(directory)
            for path in directory.rglob("model.sysml"):
                path.unlink()
            generator = Mock(side_effect=AssertionError("No model to judge"))
            report = judge_study(directory, ["one", "two"], Path(tmp) / "judgments", generator)
        generator.assert_not_called()
        self.assertEqual(report["status"], "no_candidates")
        for condition in ("A", "B", "C"):
            self.assertEqual(report["by_condition"][condition]["unreviewed"], 5)
            self.assertEqual(report["by_condition"][condition]["jointly_faithful"], 0)

    def test_failed_judgment_keeps_other_judge_and_marks_observation_unreviewed(self):
        def generate(system, prompt, model, directory, call_id):
            if model == "one":
                raise RuntimeError("transport unavailable")
            return json.dumps(assessment({"R" + str(i): "faithful" for i in range(1, 6)}))
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "study"
            study_fixture(directory, identical=True)
            report = judge_study(directory, ["one", "two"], Path(tmp) / "judgments", generate)
        self.assertEqual(report["summary"]["failed_judgments"], 1)
        self.assertTrue(all(r["judge_labels"] == [None, "faithful"] for r in report["observations"]))
        self.assertTrue(all(r["assessment_status"] == "unreviewed" for r in report["observations"]))

    def test_empty_scenario_is_not_an_explanatory_example(self):
        value = assessment({"R1": "incorrect"})
        value["requirements"][0]["distinguishing_scenario"] = " "
        with self.assertRaises(ValueError):
            _assessment(value, {"R1"})

    def test_cli_judge_packets_dispatches_two_independent_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_json(directory / "packets.json", [packet()])
            generator = Mock(return_value=json.dumps(assessment({"R1": "faithful"})))
            with patch("canonical_judging._ask", generator), redirect_stdout(io.StringIO()):
                code = main(["judge", "--packets-file", str(directory / "packets.json"), "--judge-model", "one",
                             "--judge-model", "two", "--output-dir", str(directory / "judgments")])
            self.assertEqual(code, 0)
            self.assertEqual(generator.call_count, 2)
            self.assertEqual(read_json(directory / "judgments" / "judgments.json")["summary"]["failed_judgments"], 0)

    def test_cli_requires_two_judge_configs_before_calls(self):
        with tempfile.TemporaryDirectory() as tmp, patch("canonical_judging._ask") as generator, redirect_stderr(io.StringIO()):
            code = main(["judge", "--packets-file", str(Path(tmp) / "not_read.json"), "--judge-model", "one",
                         "--output-dir", str(Path(tmp) / "judgments")])
        self.assertEqual(code, 2)
        generator.assert_not_called()

    def test_existing_judge_outputs_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "judgments"
            directory.mkdir()
            marker = directory / "original.txt"
            marker.write_text("retained")
            with self.assertRaises(FileExistsError):
                judge_packets([packet()], ["one", "two"], directory, Mock())
            self.assertEqual(marker.read_text(), "retained")


if __name__ == "__main__":
    unittest.main()
