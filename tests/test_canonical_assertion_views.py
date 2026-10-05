"""Replay/repair preserve study denominators across multiple source targets."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from bedrock_judging import validate_config
from canonical_assertions import build_suite
from canonical_assertion_judging import evaluate_packets, _study_summaries, _write_report
from canonical_assertion_repair import prepare_repair_plan, repair_assessment
from canonical_assertion_rescore import _views, rescore_assessment


SOURCES = [
    {"id": "R1", "text": "Voltage shall be at most 28 V."},
    {"id": "R2", "text": "Mass shall be at most 2 kg."},
]
MODEL = ("package Limits {\n"
         "  doc /* Voltage and mass limits. */\n"
         "  attribute voltage : Real;\n"
         "  require constraint { voltage <= 28; }\n"
         "  attribute mass : Real;\n"
         "  require constraint { mass <= 2; }\n"
         "}\n")
MODELS = ["judge-one", "judge-two"]
CONFIG = validate_config({"schema": "bedrock_judges/1", "judges": [
    {"model": model, "region": "us-east-1"} for model in MODELS]})


def suite():
    return build_suite(SOURCES, [{"id": row["id"], "assertions": [{
        "id": row["id"] + "_BOUND", "statement": row["text"],
        "category": "boundary", "evidence_requirement": "model",
        "source_basis": [{"source_id": row["id"], "quote": row["text"]}],
    }]} for row in SOURCES])


def callback(*, invalid=False, resolve_as_failure=False):
    def respond(system, prompt, model, directory, call_id):
        fields = json.loads(prompt)
        rid = fields["target_requirement_id"]
        assertions = []
        for definition in fields["assertions"]:
            aid = definition["id"]
            # Preserve a known failure in another target, alongside a missing
            # decision that the bounded correction campaign may complete.
            status = "fail" if aid == "R2_BOUND" else "pass"
            if resolve_as_failure and aid == "R1_BOUND":
                status = "fail"
            line = 4 if rid == "R1" else 6
            if invalid and aid == "R1_BOUND" and model == MODELS[0]:
                line = 2  # A comment cannot satisfy this model-evidence claim.
            assertions.append({"id": aid, "status": status,
                "rationale": "The source obligation and cited candidate were assessed.",
                "evidence": [{"start_line": line, "end_line": line}] if status == "pass" else [],
                "counterexample": None if status == "pass" else "Required behavior is missing."})
        return json.dumps({"requirement_id": rid, "assertions": assertions})
    return Mock(side_effect=respond)


def fixture(root, packet_ids, *, invalid=False):
    callbacks = [callback(invalid=invalid), callback()]
    packets = [{"id": pid, "requirements": deepcopy(SOURCES),
                "fixed_context": None, "sysml": MODEL} for pid in packet_ids]
    directory = root / "original"
    report = evaluate_packets(packets, suite(), MODELS, directory, callbacks, CONFIG)
    # Identical outputs can belong to distinct repetitions and conditions.
    # These are legitimate attributed observations, not extra provider calls.
    views = []
    for repetition in (1, 2):
        for condition in ("A", "B", "C"):
            pid = condition if condition in packet_ids else packet_ids[-1]
            views.append({"condition": condition, "repetition": repetition,
                "packet_id": pid, "artifact_arm": "BC" if pid == "BC" else condition,
                "admission": "admitted_consistent_encoding" if condition == "C" else "not_assessed"})
    _study_summaries(report, views)
    _write_report(directory, report)
    return directory, report, views, callbacks


def snapshot(directory):
    return {str(p.relative_to(directory)): p.read_bytes()
            for p in directory.rglob("*") if p.is_file()}


class AssertionViewRegressionTests(unittest.TestCase):
    def assert_denominators(self, report, packet_count):
        self.assertEqual(len(report["observations"]), 12)  # 2 targets x 2 reps x 3 arms
        self.assertEqual(report["joint"]["planned"], 8 * packet_count)
        self.assertEqual(report["source_alignment"]["planned"], 2 * packet_count)
        for arm, group in report["by_condition"].items():
            self.assertEqual(group["planned_requirement_observations"], 4, arm)
            self.assertEqual(group["joint"]["planned"], 16, arm)
            self.assertEqual(group["metrics"]["planned"], 32, arm)
            self.assertEqual(group["source_alignment"]["planned"], 4, arm)
            self.assertEqual(group["by_category"]["fidelity"]["planned"], 4, arm)
            self.assertEqual(group["by_evidence_requirement"]["model"]["planned"], 16, arm)
        admitted = report["by_condition"]["C"]["admitted"]
        self.assertEqual(admitted["planned_requirement_observations"], 4)
        self.assertEqual(admitted["joint"]["planned"], 16)

    def test_rescore_preserves_multitarget_shared_distinct_and_repeated_attributions(self):
        for packets in (("A", "BC"), ("A", "B", "C")):
            with self.subTest(packets=packets), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                old, original, views, callbacks = fixture(root, packets)
                original_bytes = snapshot(old)
                for fn in callbacks:
                    self.assertEqual(fn.call_count, 2 * len(packets))
                    fn.reset_mock()
                # New per-target metrics must never become view identity.
                annotated = deepcopy(original)
                for row in annotated["observations"]:
                    row["future_requirement_metric"] = {"id": row["requirement_id"]}
                with patch("bedrock_judging.BedrockTransport", side_effect=AssertionError("No inference")):
                    rescored = rescore_assessment(old, root / "rescored")
                    repeated = rescore_assessment(root / "rescored", root / "rescored_again")
                self.assertEqual(snapshot(old), original_bytes)
                for report in (rescored, repeated):
                    self.assert_denominators(report, len(packets))
                    self.assertEqual(report["by_condition"], original["by_condition"])
                    self.assertEqual(report["rescore"]["new_calls"], 0)
                    self.assertEqual(report["rescore"]["added_cost_usd"], 0)
                for fn in callbacks:
                    fn.assert_not_called()
                self.assertEqual(_views(annotated, set(packets)), views)

    def test_rescore_recovers_inflated_saved_views_without_changing_verdicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, original, views, _ = fixture(root, ("A", "B", "C"))
            correct_groups = deepcopy(original["by_condition"])
            original_results = deepcopy(original["results"])
            # Emulate the historical expansion: one recovered packet view per
            # requirement, followed by expansion over the whole packet again.
            _study_summaries(original, [view for view in views for _ in SOURCES])
            _write_report(old, original)
            self.assertEqual(original["by_condition"]["C"]["joint"]["planned"], 32)
            before = snapshot(old)
            fixed = rescore_assessment(old, root / "rescored")
            self.assert_denominators(fixed, 3)
            self.assertEqual(fixed["by_condition"], correct_groups)
            self.assertEqual(snapshot(old), before)
            for old_packet, new_packet in zip(original_results, fixed["results"]):
                for old_row, new_row in zip(old_packet["requirements"], new_packet["requirements"]):
                    self.assertEqual(old_row["assertions"], new_row["assertions"])

    def test_repair_and_continuation_keep_denominators_and_valid_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, original, _, _ = fixture(root, ("shared",), invalid=True)
            original_bytes = snapshot(old)
            plan = prepare_repair_plan(old)
            self.assertEqual(plan["eligible_cells"], 1)
            self.assertEqual(plan["eligible_assertion_judgments"], 1)
            first, unused = callback(invalid=True), callback()
            partial = repair_assessment(old, root / "partial", [first, unused], CONFIG, max_attempts=2)
            self.assertEqual(first.call_count, 2)
            unused.assert_not_called()
            self.assert_denominators(partial, 1)
            self.assertEqual(partial["by_condition"], original["by_condition"])
            partial_bytes = snapshot(root / "partial")
            continued_plan = prepare_repair_plan(root / "partial", continue_budget=True)
            self.assertEqual(continued_plan["eligible_cells"], 1)
            self.assertEqual(continued_plan["cells"][0]["prior_attempts"], 2)
            final_callback = callback(resolve_as_failure=True)
            final = repair_assessment(root / "partial", root / "continued",
                [final_callback, unused], CONFIG, max_attempts=2, continue_budget=True)
            final_callback.assert_called_once()
            unused.assert_not_called()
            self.assertEqual(final["repair"]["cumulative_new_calls"], 3)
            self.assert_denominators(final, 1)
            self.assertEqual(final["joint"]["unreviewed"], 0)
            for arm in ("A", "B", "C"):
                self.assertEqual(final["by_condition"][arm]["joint"]["fail"], 2)
                self.assertEqual(final["by_condition"][arm]["joint"]["disputed"], 2)
                self.assertEqual(final["by_condition"][arm]["source_alignment"]["fail"], 2)
                self.assertEqual(final["by_condition"][arm]["source_alignment"]["disputed"], 2)
            self.assertEqual(snapshot(old), original_bytes)
            self.assertEqual(snapshot(root / "partial"), partial_bytes)
            final_rows = {row["requirement_id"]: row for row in final["results"][0]["requirements"]}
            for row in original["results"][0]["requirements"]:
                for slot, judge in enumerate(row["judges"]):
                    accepted = {v["id"]: v for v in final_rows[row["requirement_id"]]["judges"][slot]
                                ["assessment"]["assertions"]}
                    for verdict in judge["assessment"]["assertions"]:
                        self.assertEqual(accepted[verdict["id"]], verdict)


if __name__ == "__main__":
    unittest.main()
