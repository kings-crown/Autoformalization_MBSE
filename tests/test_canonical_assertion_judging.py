"""Offline end-to-end assertion judging; no AWS or model calls."""
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
from canonical_assertions import build_suite
from canonical_abstractions import POLICY
from canonical_assertion_judging import evaluate_packets, evaluate_study, prepare_assertions
from bedrock_judging import BedrockTransport

MODEL = "package Candidate {\n    attribute voltage : Real;\n    require constraint { voltage <= 28; }\n}\n"
MODELS = ["judge-one", "judge-two"]


def legacy_build_suite(*args, **kwargs):
    """Freeze existing assertion fixtures at v1; new v2 behavior has separate tests."""
    return build_suite(*args, **kwargs, schema="sysml_assertions/1")


def sources(count=1):
    return [{"id": f"R{i+1}", "text": "The battery voltage shall be at most 28 V.",
             "source": {"document": "Synthetic source", "context": {"definitions": ["Voltage is the terminal voltage."],
                         "scope": "Applies during normal operation."}}} for i in range(count)]


def authored(rows):
    return [{"id": r["id"], "assertions": [{"id": f"{r['id']}-bound", "statement": "The inclusive voltage bound is 28 V.",
         "category": "boundary", "source_basis": [{"source_id": r["id"], "quote": "at most 28 V"}]}]} for r in rows]


def suite(rows=None, context=None):
    rows = rows or sources()
    return legacy_build_suite(rows, authored(rows), context, "Synthetic evaluation fixture")


def packet(pid="candidate", rows=None, model=MODEL, context=None):
    return {"id": pid, "requirements": rows or sources(), "sysml": model, "fixed_context": context}


def verdict(prompt, statuses=None):
    p = json.loads(prompt)
    lines = p["sysml_with_line_numbers"].splitlines()
    # Find a real generated constraint line; ignore the prefix when quoting it.
    line = next((i+1 for i, value in enumerate(lines) if "require constraint" in value), 1)
    quote = lines[line-1].split(": ", 1)[1]
    statuses = statuses or ["pass"] * len(p["assertions"])
    return {"requirement_id": p["target_requirement_id"], "assertions": [
        {"id": a["id"], "status": state, "rationale": "Compared the represented target with the contextual source.",
         "evidence": [{"start_line": line, "end_line": line, "quote": quote}],
         "counterexample": None if state == "pass" else "A distinguishing source boundary may be mishandled."}
        for a, state in zip(p["assertions"], statuses)]}


def callback(statuses=None):
    return Mock(side_effect=lambda system, prompt, model, directory, call_id: json.dumps(verdict(prompt, statuses)))


def study(directory, rows=None, repetitions=1, missing=None, identical=False, context=None):
    rows = rows or sources()
    directory.mkdir()
    study_rows = []
    for rep in range(1, repetitions+1):
        row = {"repetition": rep}
        for arm in ("A", "BC"):
            if arm != missing:
                path = directory / f"rep-{rep:03d}" / arm
                path.mkdir(parents=True)
                (path / "model.sysml").write_text(MODEL if identical or arm == "BC" else MODEL.replace("Candidate", "Direct"))
            row[arm] = {"status": "failed" if arm == missing else "completed",
                        "admission": "admitted_consistent_encoding" if arm == "BC" else "not_assessed"}
        study_rows.append(row)
    write_json(directory / "sources.json", rows)
    write_json(directory / "study.json", {"rows": study_rows})
    write_json(directory / "study_configuration.json", {"model": "PRIVATE_GENERATOR", "context": context})
    return directory


def config(author=False):
    result = {"schema": "bedrock_judges/1", "judges": [{"model": m, "region": "us-east-1"} for m in MODELS]}
    if author:
        result["assertion_author"] = {"model": "author-model", "region": "us-east-1"}
    return result


class AssertionJudgingTests(unittest.TestCase):
    def test_each_target_sees_complete_context_and_same_fixed_checks(self):
        rows = sources(2); s = suite(rows)
        callbacks = [callback(), callback()]
        p = packet(rows=rows)
        p["expected_assertions"] = {s["requirements"][0]["assertions"][0]["id"]: "fail"}
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_packets([p], s, MODELS, Path(tmp)/"judge", callbacks)
        for fn in callbacks:
            self.assertEqual(fn.call_count, 2)
            for index, call in enumerate(fn.call_args_list):
                prompt = json.loads(call.args[1])
                self.assertEqual(prompt["source_packet"], rows)
                self.assertEqual(prompt["target_requirement_id"], rows[index]["id"])
                self.assertEqual(prompt["assertions"], s["requirements"][index]["assertions"])
                self.assertNotIn("expected_assertions", prompt)
                self.assertNotIn("author", prompt)
                self.assertNotIn("condition", prompt)
                self.assertIn("require constraint", prompt["sysml_with_line_numbers"])
        self.assertEqual(callbacks[0].call_args_list[0].args[1], callbacks[1].call_args_list[0].args[1])
        self.assertEqual(report["joint"]["planned"], 6)
        self.assertEqual(report["joint"]["pass_rate"], 1)

    def test_numbered_model_preserves_original_newline_evidence(self):
        fn = callback()
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_packets([packet(model=MODEL.replace("\n", "\r\n"))], suite(), MODELS, Path(tmp)/"judge", [fn, fn])
        prompt = json.loads(fn.call_args.args[1])
        self.assertIn("\r\n", prompt["sysml_with_line_numbers"])
        self.assertIn("at most 12 spans", fn.call_args.args[0])
        self.assertEqual(report["joint"]["pass_rate"], 1)

    def test_fixed_denominator_and_independent_disagreement(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_packets([packet()], suite(), MODELS, Path(tmp)/"judge",
                       [callback(["pass", "fail", "unresolved"]), callback(["pass", "pass", "unresolved"])])
        row = report["results"][0]["requirements"][0]
        self.assertEqual(row["metrics"]["planned"], 6)
        self.assertEqual(row["metrics"]["pass_rate"], 3/6)
        self.assertEqual(row["joint"]["pass_rate"], 1/3)
        self.assertEqual(row["joint"]["disputed"], 1)
        self.assertEqual(row["joint"]["unresolved"], 1)
        self.assertEqual(report["status"], "completed")

    def test_provider_failure_preserves_other_judge_and_unreviewed(self):
        failed = Mock(side_effect=RuntimeError("simulated provider failure"))
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_packets([packet()], suite(), MODELS, Path(tmp)/"judge", [failed, callback()])
        self.assertEqual(failed.call_count, 1)
        self.assertEqual(report["metrics"]["unreviewed"], 3)
        self.assertEqual(report["metrics"]["pass_rate"], .5)
        self.assertEqual(report["joint"]["pass_rate"], 0)
        self.assertEqual(report["summary"]["failed_calls"], 1)
        self.assertEqual(report["status"], "incomplete")

    def test_missing_assertion_preserves_other_verdicts_without_shrinking_denominator(self):
        def partial(system, prompt, *args):
            v = verdict(prompt); v["assertions"].pop()
            return json.dumps(v)
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_packets([packet()], suite(), MODELS, Path(tmp)/"judge", [partial, callback()])
        self.assertEqual(report["metrics"]["unreviewed"], 1)
        self.assertEqual(report["joint"]["planned"], 3)
        self.assertEqual(report["joint"]["pass"], 2)
        self.assertEqual(report["summary"]["partial_calls"], 1)
        self.assertEqual(report["summary"]["failed_calls"], 0)
        self.assertEqual(report["results"][0]["requirements"][0]["judges"][0]["status"], "partial")

    def test_bad_citation_retains_valid_pass_fail_and_unresolved_peers(self):
        model = MODEL + "// Source documentation alone cannot establish a passing assertion.\n"
        rows = sources()
        authored_rows = authored(rows)
        extra = deepcopy(authored_rows[0]["assertions"][0])
        extra["id"] = "R1-additional"
        authored_rows[0]["assertions"].append(extra)
        checked = legacy_build_suite(rows, authored_rows)

        def mixed(system, prompt, *args):
            response = verdict(prompt, ["pass", "pass", "fail", "unresolved"])
            response["assertions"][1]["evidence"] = [{"start_line": 5, "end_line": 5}]
            return json.dumps(response)

        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_packets([packet(model=model)], checked, MODELS, Path(tmp)/"judge",
                                      [mixed, callback(["pass", "pass", "fail", "unresolved"])])
            record = read_json(Path(tmp)/"judge/candidate/requirement-0001/judge-1/assessment.json")
        self.assertEqual(record["status"], "partial")
        self.assertEqual([a["status"] for a in record["assessment"]["assertions"]],
                         ["pass", "fail", "unresolved"])
        self.assertEqual(record["assessment"]["validation"]["rejected"], 1)
        self.assertEqual(report["joint"], {"planned": 4, "pass": 1, "fail": 1,
            "disputed": 0, "unresolved": 1, "unreviewed": 1, "pass_rate": 0.25})
        error = report["results"][0]["requirements"][0]["assertions"][1]["judge_errors"][0]
        self.assertIn("actual code evidence", error)

    def test_comment_only_passes_do_not_become_assessed_successes(self):
        model = "doc /* The battery voltage shall be at most 28 V. */"
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_packets([packet(model=model)], suite(), MODELS, Path(tmp)/"judge", [callback(), callback()])
        self.assertEqual(report["metrics"]["unreviewed"], 6)
        self.assertEqual(report["metrics"]["pass_rate"], 0)

    def test_mismatched_source_or_context_rejected_before_output_or_calls(self):
        for changed in ("source", "context"):
            p = packet()
            if changed == "source": p["requirements"][0]["source"]["context"]["scope"] = "Changed scope"
            else: p["fixed_context"] = {"variables": [{"name": "v", "type": "Real", "unit": "V"}], "background": []}
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                fn = callback(); out = Path(tmp)/"judge"
                with self.assertRaises(ValueError): evaluate_packets([p], suite(), MODELS, out, [fn, fn])
                fn.assert_not_called(); self.assertFalse(out.exists())

    def test_empty_candidate_preserves_planned_assertions_without_inference(self):
        fn = callback()
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_packets([packet(model="")], suite(), MODELS, Path(tmp)/"judge", [fn, fn])
        fn.assert_not_called()
        self.assertEqual(report["joint"]["unreviewed"], 3)
        self.assertEqual(report["metrics"]["unreviewed"], 6)
        self.assertEqual(report["metrics"]["resolved_pass_rate"], None)
        self.assertEqual(report["status"], "incomplete")

    def test_study_BC_reuses_judgments_and_partitions_conditions(self):
        fn = callback()
        with tempfile.TemporaryDirectory() as tmp:
            run = study(Path(tmp)/"study", repetitions=2)
            report = evaluate_study(run, suite(), MODELS, Path(tmp)/"judge", [fn, fn])
            self.assertTrue((Path(tmp)/"judge"/"report.md").is_file())
            self.assertIn("Generation recorded no abstraction policy", (Path(tmp)/"judge"/"report.md").read_text())
            configuration = read_json(Path(tmp)/"judge"/"configuration.json")
            self.assertEqual(configuration["policy_alignment"]["status"], "legacy_generation_unclassified")
            self.assertEqual(report["policy_alignment"], configuration["policy_alignment"])
        self.assertEqual(fn.call_count, 4)  # two distinct artifacts, two judges
        self.assertEqual(report["by_condition"]["B"]["joint"], report["by_condition"]["C"]["joint"])
        self.assertEqual(report["by_condition"]["A"]["joint"]["planned"], 6)
        self.assertEqual(report["summary"]["unique_packets"], 2)

    def test_missing_study_artifact_does_not_disappear_from_arm_rate(self):
        fn = callback()
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_study(study(Path(tmp)/"study", missing="A"), suite(), MODELS, Path(tmp)/"judge", [fn, fn])
        self.assertEqual(fn.call_count, 2)
        self.assertEqual(report["by_condition"]["A"]["joint"]["unreviewed"], 3)
        self.assertEqual(report["by_condition"]["A"]["joint"]["pass_rate"], 0)
        self.assertEqual(report["by_condition"]["C"]["joint"]["pass_rate"], 1)

    def test_admitted_assertion_failures_remain_visible(self):
        fn = callback(["fail", "pass", "pass"])
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_study(study(Path(tmp)/"study"), suite(), MODELS, Path(tmp)/"judge", [fn, fn])
        admitted = report["by_condition"]["C"]["admitted"]
        self.assertEqual(admitted["requirements_with_jointly_failed_assertions"], 1)
        self.assertEqual(admitted["joint"]["fail"], 1)

    def test_calibration_reference_answers_do_not_enter_prompts(self):
        s = suite(); p = packet()
        p["expected_assertions"] = {s["requirements"][0]["assertions"][0]["id"]: "fail"}
        fn = callback(["fail", "pass", "pass"])
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_packets([p], s, MODELS, Path(tmp)/"judge", [fn, fn])
        for c in fn.call_args_list: self.assertNotIn("expected_assertions", json.loads(c.args[1]))
        self.assertEqual(report["calibration_summary"][0]["sensitivity_planned"], 1)

    def test_output_directory_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            fn = callback()
            with self.assertRaises(FileExistsError): evaluate_packets([packet()], suite(), MODELS, tmp, [fn, fn])
            fn.assert_not_called()

    def test_source_only_authoring_namespaces_ids_and_freezes_checks(self):
        rows = sources(2); prompts = []
        def author(system, prompt, model, directory, call_id):
            p = json.loads(prompt); prompts.append(p)
            rid = p["target_requirement_id"]
            row = authored([next(r for r in rows if r["id"] == rid)])[0]
            row["assertions"][0]["id"] = "A1"  # independent author calls can reuse local IDs
            return json.dumps(row)
        with tempfile.TemporaryDirectory() as tmp:
            result = prepare_assertions(rows, None, "author", Path(tmp)/"prepare", author)
            saved = read_json(Path(tmp)/"prepare"/"assertions.json")
        self.assertEqual(result["status"], "completed")
        ids = [a["id"] for r in saved["requirements"] for a in r["assertions"]]
        self.assertEqual(len(set(ids)), 8)
        self.assertEqual(saved["schema"], "sysml_assertions/3")
        self.assertIn("REQ_0001_A1", ids); self.assertIn("REQ_0002_A1", ids)
        for p in prompts:
            self.assertEqual(p["source_packet"], rows)
            self.assertEqual(set(p), {"target_requirement_id", "source_packet", "fixed_context", "abstraction_policy"})
            self.assertEqual(p["abstraction_policy"], POLICY)

    def test_failed_authoring_retains_response_but_emits_no_partial_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/"prepare"
            result = prepare_assertions(sources(), None, "author", out, Mock(return_value="not json"))
            self.assertFalse((out/"assertions.json").exists())
            self.assertTrue((out/"requirement-0001"/"response.txt").exists())
            self.assertEqual(result["status"], "failed")

    def test_cli_bedrock_adapter_roundtrip_is_offline_and_uses_assertions(self):
        requests = []
        class Client:
            def converse(self, **request):
                requests.append(request)
                p = request["messages"][0]["content"][0]["text"]
                return {"output": {"message": {"role": "assistant", "content": [{"text": json.dumps(verdict(p))}]}},
                        "stopReason": "end_turn", "usage": {"inputTokens": 20, "outputTokens": 30}}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for name, data in (("config.json", config()), ("suite.json", suite()), ("packets.json", [packet()])):
                write_json(base/name, data)
            with patch("bedrock_judging._default_client", lambda cfg: Client()), redirect_stdout(io.StringIO()):
                rc = main(["judge", "--packets-file", str(base/"packets.json"), "--judge-config", str(base/"config.json"),
                           "--assertions-file", str(base/"suite.json"), "--output-dir", str(base/"judged")])
            report = read_json(base/"judged"/"judgments.json")
            logs = list((base/"judged").glob("**/bedrock_calls/**/result.json"))
        self.assertEqual(rc, 0); self.assertEqual(len(requests), 2); self.assertEqual(len(logs), 2)
        self.assertEqual(report["joint"]["pass_rate"], 1)

    def test_cli_requires_suite_before_calling_bedrock(self):
        with patch("bedrock_judging._default_client") as client, redirect_stderr(io.StringIO()):
            rc = main(["judge", "--packets-file", "unused.json", "--judge-config", "unused-config.json", "--output-dir", "unused"])
        self.assertEqual(rc, 2); client.assert_not_called()

    def test_cli_preparation_reads_only_study_source_not_model(self):
        prompts = []
        class Client:
            def converse(self, **request):
                p = json.loads(request["messages"][0]["content"][0]["text"]); prompts.append(p)
                row = authored(p["source_packet"])[0]
                return {"output": {"message": {"role": "assistant", "content": [{"text": json.dumps(row)}]}},
                        "stopReason": "end_turn", "usage": {"inputTokens": 20, "outputTokens": 30}}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp); run = study(base/"study")
            write_json(base/"config.json", config(author=True))
            with patch("bedrock_judging._default_client", lambda cfg: Client()), redirect_stdout(io.StringIO()):
                rc = main(["prepare-assertions", "--study-dir", str(run), "--judge-config", str(base/"config.json"), "--output-dir", str(base/"prepared")])
        self.assertEqual(rc, 0); self.assertEqual(len(prompts), 1)
        self.assertEqual(set(prompts[0]), {"target_requirement_id", "source_packet", "fixed_context", "abstraction_policy"})
        self.assertEqual(prompts[0]["abstraction_policy"], POLICY)

    def test_cli_rejects_empty_packets_without_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            write_json(p/"config.json", config()); write_json(p/"suite.json", suite()); write_json(p/"packets.json", [])
            with patch("bedrock_judging._default_client") as client, redirect_stderr(io.StringIO()):
                rc = main(["judge", "--packets-file", str(p/"packets.json"), "--judge-config", str(p/"config.json"),
                           "--assertions-file", str(p/"suite.json"), "--output-dir", str(p/"judged")])
            self.assertEqual(rc, 2); client.assert_not_called(); self.assertFalse((p/"judged").exists())


if __name__ == "__main__":
    unittest.main()
