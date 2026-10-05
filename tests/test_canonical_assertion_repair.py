"""Frozen-candidate judgment correction: offline callbacks, no provider calls."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from bedrock_judging import validate_config
from canonical_assertions import build_suite
from canonical_assertion_judging import evaluate_packets, _study_summaries, _write_report
from canonical_assertion_repair import prepare_repair_plan, repair_assessment
from canonical_assertion_rescore import rescore_assessment
from canonical_cli import read_json, write_json


SOURCES = [{"id": "R1", "text": "The voltage shall be at most 28 V.",
            "source": {"context": {"scope": "Normal operation."}}}]
MODEL = ("package Battery {\n"
         "  doc /* Voltage at most 28 V. */\n"
         "  attribute voltage : Real;\n"
         "  require constraint { voltage <= 28; }\n"
         "}\n")
MODELS = ["first-judge", "second-judge"]
CONFIG = validate_config({"schema": "bedrock_judges/1", "judges": [
    {"model": name, "region": "us-east-1", "structured_output": True,
     "prices_per_million": {"input_tokens": .15, "output_tokens": .6}}
    for name in MODELS]})


def suite():
    return build_suite(SOURCES, [{"id": "R1", "assertions": [{"id": "BOUND",
        "statement": "The inclusive upper bound is 28 V.", "category": "boundary",
        "evidence_requirement": "model", "source_basis": [{"source_id": "R1", "quote": "at most 28 V"}]}]}])


def response(prompt, bad=(), statuses=None, *, quote=False):
    fields = json.loads(prompt)
    verdicts = []
    for i, assertion in enumerate(fields["assertions"]):
        status = (statuses or {}).get(assertion["id"], "pass")
        line = 2 if assertion["id"] in bad else 4
        span = {"start_line": line, "end_line": line}
        if quote:
            span["quote"] = MODEL.splitlines()[line - 1]
        verdicts.append({"id": assertion["id"], "status": status,
            "rationale": "Mapped the actual constraint to the specific source obligation.",
            "evidence": [span] if status == "pass" else [],
            "counterexample": None if status == "pass" else "The required bound is not established."})
    return {"requirement_id": fields["target_requirement_id"], "assertions": verdicts}


def fixture(root, bad_first=("BOUND",), bad_second=(), statuses=None, empty=False, two_packets=False):
    callbacks = [Mock(side_effect=lambda system, prompt, *args: json.dumps(response(prompt, bad_first, statuses, quote=True))),
                 Mock(side_effect=lambda system, prompt, *args: json.dumps(response(prompt, bad_second, statuses, quote=True)))]
    packets = [{"id": "candidate", "requirements": SOURCES, "fixed_context": None, "sysml": "" if empty else MODEL}]
    if two_packets:
        packets.append({**packets[0], "id": "candidate-two"})
    old = root / "old"
    report = evaluate_packets(packets, suite(), MODELS, old, callbacks, CONFIG)
    return old, report


def correction(statuses=None, *, bad=(), receipt=False, leading_brace=False):
    def callback(system, prompt, model, directory, call_id):
        text = json.dumps(response(prompt, bad=bad, statuses=statuses))
        if leading_brace:
            text = "{\n" + text
        if receipt:
            folder = Path(directory) / "bedrock_calls" / ("judge-1" if model == MODELS[0] else "judge-2") / call_id
            folder.mkdir(parents=True)
            write_json(folder / "result.json", {"status": "ok", "stop_reason": "end_turn", "text": text,
                       "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                                 "cache_read_tokens": 0, "cache_write_tokens": 0},
                       "cost": {"usd": .012}, "seconds": .1})
        return text
    return Mock(side_effect=callback)


class JudgmentRepairTests(unittest.TestCase):
    def test_plan_read_only_selects_invalid_slots_not_valid_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, original = fixture(root, statuses={"ASSERT_0001_COVERAGE": "fail", "ASSERT_0001_FIDELITY": "unresolved"})
            before = {str(p.relative_to(old)): p.read_bytes() for p in old.rglob("*") if p.is_file()}
            plan = prepare_repair_plan(old)
            after = {str(p.relative_to(old)): p.read_bytes() for p in old.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(plan["eligible_cells"], 1)
            self.assertEqual(plan["eligible_assertion_judgments"], 1)
            self.assertEqual(plan["cells"][0]["assertion_ids"], ["BOUND"])

    def test_selective_calls_preserve_originals_and_accepted_verdicts_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, original = fixture(root)
            snapshot = {str(p.relative_to(old)): p.read_bytes() for p in old.rglob("*") if p.is_file()}
            first, second = correction(), correction()
            report = repair_assessment(old, root / "new", [first, second], CONFIG)
            first.assert_called_once(); second.assert_not_called()
            self.assertEqual(snapshot, {str(p.relative_to(old)): p.read_bytes() for p in old.rglob("*") if p.is_file()})
            old_j = original["results"][0]["requirements"][0]["judges"][0]
            new_j = report["results"][0]["requirements"][0]["judges"][0]
            original_accepted = {v["id"]: v for v in old_j["assessment"]["assertions"]}
            for verdict in new_j["assessment"]["assertions"]:
                if verdict["id"] in original_accepted:
                    self.assertEqual(verdict, original_accepted[verdict["id"]])
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["joint"]["planned"], 4)
            self.assertEqual(report["metrics"]["planned"], 8)
            self.assertEqual(report["repair"]["new_calls"], 1)
            self.assertEqual(report["repair"]["unknown_cost_calls"], 1)
            self.assertIsNone(report["repair"]["added_cost_usd"])

    def test_prompt_has_frozen_context_subset_and_only_same_judge_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _ = fixture(root)
            fn = correction()
            repair_assessment(old, root / "new", [fn, correction()], CONFIG)
            system, prompt, model, directory, call_id = fn.call_args.args
            fields = json.loads(prompt)
            self.assertEqual(fields["source_packet"], SOURCES)
            self.assertEqual(fields["fixed_context"], None)
            self.assertEqual([a["id"] for a in fields["assertions"]], ["BOUND"])
            self.assertEqual(model, MODELS[0]); self.assertEqual(call_id, "judgment")
            self.assertEqual(directory.name, "001")
            self.assertIn("unrelated code", system)
            self.assertIn("fail or unresolved", system)
            self.assertEqual(len(fields["judgment_correction"]["diagnostics"]), 1)
            for key in ("condition", "judge_models", "joint", "score", "admission", "solver", "counterpart"):
                self.assertNotIn(key, fields)
            self.assertNotIn(MODELS[1], prompt)

    def test_valid_fail_and_unresolved_stop_without_favorable_retry(self):
        for status in ("fail", "unresolved"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); old, _ = fixture(root)
                fn = correction({"BOUND": status})
                report = repair_assessment(old, root / "new", [fn, correction()], CONFIG)
                fn.assert_called_once()
                self.assertEqual(report["status"], "completed")
                self.assertEqual(report["results"][0]["requirements"][0]["assertions"][0]["judge_statuses"], [status, "pass"])

    def test_budget_exhaustion_retains_invalid_as_unreviewed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _ = fixture(root)
            fn = correction(bad=("BOUND",))
            report = repair_assessment(old, root / "new", [fn, correction()], CONFIG)
            self.assertEqual(fn.call_count, 2)
            self.assertEqual(report["status"], "incomplete")
            self.assertEqual(report["metrics"]["unreviewed"], 1)
            self.assertEqual(report["joint"]["planned"], 4)
            self.assertEqual(report["repair"]["remaining_unreviewed_judgments"], 1)
            self.assertIn("actual code evidence", report["results"][0]["requirements"][0]["assertions"][0]["judge_errors"][0])

    def test_second_attempt_contains_only_still_invalid_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _ = fixture(root, bad_first=("BOUND", "ASSERT_0001_COVERAGE"))
            prompts = []
            def cb(system, prompt, *args):
                prompts.append(json.loads(prompt))
                return json.dumps(response(prompt, bad=("ASSERT_0001_COVERAGE",) if len(prompts) == 1 else ()))
            report = repair_assessment(old, root / "new", [cb, correction()], CONFIG)
            self.assertEqual([a["id"] for a in prompts[0]["assertions"]], ["BOUND", "ASSERT_0001_COVERAGE"])
            self.assertEqual([a["id"] for a in prompts[1]["assertions"]], ["ASSERT_0001_COVERAGE"])
            self.assertEqual(report["status"], "completed")
            origins = read_json(root / "new/candidate/requirement-0001/judge-1/composite.json")["verdict_origins"]
            self.assertEqual(origins["BOUND"]["attempt"], 1)
            self.assertEqual(origins["ASSERT_0001_COVERAGE"]["attempt"], 2)

    def test_unrequested_change_cannot_replace_valid_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, original = fixture(root, statuses={"ASSERT_0001_COVERAGE": "fail"})
            def cb(system, prompt, *args):
                raw = response(prompt)
                raw["assertions"].append({**raw["assertions"][0], "id": "ASSERT_0001_COVERAGE"})
                return json.dumps(raw)
            report = repair_assessment(old, root / "new", [cb, correction()], CONFIG)
            row = report["results"][0]["requirements"][0]
            self.assertEqual(row["assertions"][1]["judge_statuses"], ["fail", "fail"])
            self.assertEqual(report["repair"]["new_calls"], 1)
            attempt = read_json(root / "new/candidate/requirement-0001/judge-1/attempts/001/assessment.json")
            self.assertEqual(len(attempt["assessment"]["validation"]["response_errors"]), 1)

    def test_transport_failure_is_recorded_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _ = fixture(root)
            fn = Mock(side_effect=RuntimeError("No provider completion"))
            report = repair_assessment(old, root / "new", [fn, correction()], CONFIG, max_attempts=1)
            fn.assert_called_once()
            self.assertEqual(report["status"], "incomplete")
            self.assertIn("No provider completion", report["repair"]["attempts"][0]["error"])
            self.assertIsNone(report["repair"]["added_cost_usd"])

    def test_duplicate_or_wrong_target_reply_does_not_supply_a_judgment(self):
        for kind in ("duplicate", "wrong_target", "wrong_id"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); old, _ = fixture(root)
                def cb(system, prompt, *args):
                    raw = response(prompt)
                    if kind == "duplicate": raw["assertions"] *= 2
                    elif kind == "wrong_target": raw["requirement_id"] = "R99"
                    else: raw["assertions"][0]["id"] = "NOT_FROZEN"
                    return json.dumps(raw)
                report = repair_assessment(old, root / "new", [cb, correction()], CONFIG, max_attempts=1)
                self.assertEqual(report["metrics"]["unreviewed"], 1)

    def test_missing_candidate_never_calls_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _ = fixture(root, empty=True)
            first, second = correction(), correction()
            report = repair_assessment(old, root / "new", [first, second], CONFIG)
            first.assert_not_called(); second.assert_not_called()
            self.assertEqual(report["metrics"]["unreviewed"], 8)
            self.assertEqual(report["repair"]["new_calls"], 0)
            self.assertEqual(report["repair"]["added_cost_usd"], 0)

    def test_costs_are_new_unique_receipts_and_composite_can_rescore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _ = fixture(root, bad_second=("BOUND",))
            first, second = correction(receipt=True), correction(receipt=True)
            report = repair_assessment(old, root / "new", [first, second], CONFIG)
            meta = report["repair"]
            self.assertEqual(meta["new_calls"], 2)
            self.assertEqual(meta["provider_records"], 2)
            self.assertAlmostEqual(meta["added_cost_usd"], .024)
            self.assertEqual(meta["usage"]["input_tokens"]["total"], 200)
            self.assertEqual(len(list((root / "new").glob("*/requirement-*/judge-*/bedrock_calls"))), 0)
            new = rescore_assessment(root / "new", root / "rescore")
            self.assertEqual(new["joint"], report["joint"])
            self.assertEqual(new["rescore"]["new_calls"], 0)
            self.assertIn("NOT a single provider", read_json(root / "new/candidate/requirement-0001/judge-1/composite.json")["role"])

    def test_bounded_brace_recovery_requires_matching_provider_receipt(self):
        for receipt in (True, False):
            with self.subTest(receipt=receipt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); old, _ = fixture(root)
                fn = correction(receipt=receipt, leading_brace=True)
                report = repair_assessment(old, root / "new", [fn, correction()], CONFIG, max_attempts=1)
                self.assertEqual(report["status"], "completed" if receipt else "incomplete")
                parsing = read_json(root / "new/candidate/requirement-0001/judge-1/attempts/001/parse_recovery.json")
                self.assertEqual(parsing["status"], "recovered" if receipt else "rejected")

    def test_recovered_input_follows_original_receipt_without_copying_billing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, original = fixture(root)
            call = old / "candidate/requirement-0001/judge-1"
            text = "{\n" + (call / "response.txt").read_text()
            (call / "response.txt").write_text(text); (call / "response.json").write_text(text)
            receipt = call / "bedrock_calls/judge-1/judgment/result.json"
            receipt.parent.mkdir(parents=True)
            write_json(receipt, {"status": "ok", "stop_reason": "end_turn", "text": text})
            rescore_assessment(old, root / "recovered", recover_leading_brace=True)
            plan = prepare_repair_plan(root / "recovered")
            self.assertEqual(plan["eligible_cells"], 1)
            report = repair_assessment(root / "recovered", root / "new", [correction(), correction()], CONFIG)
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["repair"]["provider_records"], 0)

    def test_same_packet_condition_views_do_not_multiply_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, report = fixture(root)
            views = [{"condition": condition, "artifact_arm": condition, "repetition": 1,
                      "packet_id": "candidate", "admission": "not_assessed"} for condition in ("A", "B", "C")]
            _study_summaries(report, views); _write_report(old, report)
            fn = correction()
            result = repair_assessment(old, root / "new", [fn, correction()], CONFIG)
            fn.assert_called_once()
            self.assertEqual(set(result["by_condition"]), {"A", "B", "C"})
            self.assertTrue(all(v["joint"]["planned"] == 4 for v in result["by_condition"].values()))

    def test_frozen_configuration_and_prompt_changes_fail_before_calls_or_output(self):
        for change in ("model", "temperature", "prices", "prompt", "evidence", "accepted", "mode"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); old, report = fixture(root)
                configuration = deepcopy(CONFIG)
                if change == "model": configuration["judges"][0]["model"] = "different"
                elif change == "temperature": configuration["judges"][0]["temperature"] = .2
                elif change == "prices": configuration["judges"][0]["prices_per_million"]["input_tokens"] = 99
                elif change == "prompt":
                    path = old / "candidate/requirement-0001/judge-1/prompt.json"
                    p = read_json(path); p["source_packet"][0]["text"] = "Changed source"; write_json(path, p)
                elif change == "evidence":
                    report["evidence_policy"] = "other"; write_json(old / "judgments.json", report)
                elif change == "mode":
                    report["assertion_evidence_requirements"]["BOUND"] = "documentation"; write_json(old / "judgments.json", report)
                else:
                    report["results"][0]["requirements"][0]["judges"][0]["assessment"]["assertions"][0]["rationale"] = "Changed"
                    write_json(old / "judgments.json", report)
                fn = correction()
                with self.assertRaises(ValueError): repair_assessment(old, root / "new", [fn, fn], configuration)
                fn.assert_not_called(); self.assertFalse((root / "new").exists())

    def test_overlap_existing_output_and_budget_reset_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _ = fixture(root)
            fn = correction()
            for path in (old, old / "nested", root):
                with self.assertRaises(ValueError): repair_assessment(old, path, [fn, fn], CONFIG)
            (root / "exists").mkdir()
            with self.assertRaises(FileExistsError): repair_assessment(old, root / "exists", [fn, fn], CONFIG)
            repair_assessment(old, root / "new", [fn, fn], CONFIG)
            with self.assertRaisesRegex(ValueError, "reset"):
                repair_assessment(root / "new", root / "again", [fn, fn], CONFIG)

    def test_invalid_budgets_and_worker_counts_fail_before_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _ = fixture(root); fn = correction()
            for value in (0, 3, True, 1.5):
                with self.assertRaises(ValueError): repair_assessment(old, root / "new", [fn, fn], CONFIG, max_attempts=value)
            for value in (0, 3, True):
                with self.assertRaises(ValueError): repair_assessment(old, root / "new", [fn, fn], CONFIG, workers=value)
            fn.assert_not_called()

    def test_explicit_continuation_preserves_prior_accepted_and_costs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _ = fixture(root)
            first = repair_assessment(old, root / "first", [correction(bad=("BOUND",), receipt=True), correction()], CONFIG)
            before = {str(p.relative_to(root / "first")): p.read_bytes() for p in (root / "first").rglob("*") if p.is_file()}
            with self.assertRaisesRegex(ValueError, "reset"):
                prepare_repair_plan(root / "first")
            plan = prepare_repair_plan(root / "first", continue_budget=True)
            self.assertEqual(plan["cells"][0]["prior_attempts"], 2)
            self.assertEqual(plan["cells"][0]["remaining_cumulative_attempts"], 2)
            fn = correction({"BOUND": "fail"}, receipt=True)
            final = repair_assessment(root / "first", root / "continued", [fn, correction()], CONFIG, continue_budget=True)
            fn.assert_called_once()
            self.assertEqual(before, {str(p.relative_to(root / "first")): p.read_bytes() for p in (root / "first").rglob("*") if p.is_file()})
            meta = final["repair"]
            self.assertEqual(meta["new_calls"], 1); self.assertEqual(meta["prior_new_calls"], 2)
            self.assertEqual(meta["cumulative_new_calls"], 3)
            self.assertAlmostEqual(meta["added_cost_usd"], .012)
            self.assertAlmostEqual(meta["prior_added_cost_usd"], .024)
            self.assertAlmostEqual(meta["cumulative_added_cost_usd"], .036)
            self.assertEqual(meta["attempts"][0]["cumulative_attempt"], 3)
            prior = first["results"][0]["requirements"][0]["judges"][0]["assessment"]["assertions"]
            final_by_id = {a["id"]: a for a in final["results"][0]["requirements"][0]["judges"][0]["assessment"]["assertions"]}
            for accepted in prior:
                self.assertEqual(accepted, final_by_id[accepted["id"]])
            prompt = json.loads(fn.call_args.args[1])
            diagnostics = prompt["judgment_correction"]["diagnostics"][0]
            self.assertEqual(diagnostics["previous_rejected_verdicts"][0]["evidence"], [{"start_line": 2, "end_line": 2}])
            self.assertEqual(final["status"], "completed")

    def test_hard_cumulative_four_attempt_cap_cannot_be_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _ = fixture(root)
            bad = correction(bad=("BOUND",))
            repair_assessment(old, root / "first", [bad, correction()], CONFIG)
            final = repair_assessment(root / "first", root / "second", [bad, correction()], CONFIG, continue_budget=True)
            self.assertEqual(bad.call_count, 4)
            self.assertEqual(final["repair"]["cumulative_attempt_counts"][0]["attempts"], 4)
            self.assertIsNone(final["repair"]["cumulative_added_cost_usd"])
            self.assertEqual(final["metrics"]["unreviewed"], 1)
            with self.assertRaisesRegex(ValueError, "exhausted"):
                repair_assessment(root / "second", root / "third", [bad, correction()], CONFIG, continue_budget=True)
            self.assertEqual(bad.call_count, 4)
            self.assertFalse((root / "third").exists())

    def test_continuation_respects_remaining_cumulative_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _ = fixture(root)
            bad = correction(bad=("BOUND",))
            repair_assessment(old, root / "first", [bad, correction()], CONFIG, max_attempts=1)
            repair_assessment(root / "first", root / "second", [bad, correction()], CONFIG, continue_budget=True)
            final = repair_assessment(root / "second", root / "third", [bad, correction()], CONFIG, continue_budget=True)
            self.assertEqual(bad.call_count, 4)
            self.assertEqual(final["repair"]["new_calls"], 1)
            self.assertEqual(read_json(root / "third/repair_plan.json")["maximum_new_calls"], 1)

    def test_continuation_denies_missing_or_inconsistent_history(self):
        for defect in ("no_history", "missing_ledger", "running", "attempt_record", "frozen_input", "omitted_attempt"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); old, _ = fixture(root)
                source = old
                if defect != "no_history":
                    repair_assessment(old, root / "first", [correction(bad=("BOUND",)), correction()], CONFIG)
                    source = root / "first"
                    if defect == "missing_ledger": (source / "repair.json").unlink()
                    elif defect == "running":
                        meta = read_json(source / "repair.json"); meta["status"] = "running"; write_json(source / "repair.json", meta)
                    elif defect == "attempt_record":
                        path = source / "candidate/requirement-0001/judge-1/attempts/001/attempt.json"
                        entry = read_json(path); entry["attempt"] = 0; write_json(path, entry)
                    elif defect == "frozen_input": (source / "rubric.txt").write_text("Changed rubric")
                    else:
                        meta = read_json(source / "repair.json"); meta["attempts"].pop()
                        report = read_json(source / "judgments.json"); report["repair"] = meta
                        write_json(source / "repair.json", meta); write_json(source / "judgments.json", report)
                fn = correction()
                with self.assertRaises((ValueError, OSError)):
                    repair_assessment(source, root / "new", [fn, fn], CONFIG, continue_budget=True)
                fn.assert_not_called(); self.assertFalse((root / "new").exists())

    def test_navigation_is_only_original_lexical_line_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old, _ = fixture(root)
            fn = correction()
            repair_assessment(old, root / "new", [fn, correction()], CONFIG)
            navigation = json.loads(fn.call_args.args[1])["judgment_correction"]["original_model_navigation"]
            self.assertEqual(navigation["model_line_count"], 5)
            self.assertEqual(navigation["code_bearing_original_line_numbers"], [1, 3, 4])
            self.assertNotIn(2, navigation["code_bearing_original_line_numbers"])
            self.assertNotIn(5, navigation["code_bearing_original_line_numbers"])
            self.assertIn("NOT evidence endorsement", navigation["interpretation"])
            diag = json.loads(fn.call_args.args[1])["judgment_correction"]["diagnostics"][0]["previous_citation_diagnostics"][0][0]
            self.assertEqual(diag["cited_line_count"], 1)
            self.assertEqual(diag["code_bearing_original_line_numbers"], [])

    def test_navigation_flags_overlong_and_out_of_range_spans_without_invention(self):
        from canonical_assertion_repair import _span_diagnostics
        model = "doc /* a */\n" * 100 + "require constraint { true; }\n"
        verdict = {"evidence": [{"start_line": 1, "end_line": 101}, {"start_line": 99, "end_line": 200}]}
        spans = _span_diagnostics(verdict, model)
        self.assertEqual(spans[0]["cited_line_count"], 101)
        self.assertTrue(spans[0]["exceeds_maximum_span_lines"])
        self.assertTrue(spans[0]["within_original_model"])
        self.assertFalse(spans[1]["within_original_model"])
        self.assertEqual(spans[0]["code_bearing_original_line_numbers"], [101])
        self.assertEqual(spans[1]["code_bearing_original_line_numbers"], [101])


if __name__ == "__main__":
    unittest.main()
