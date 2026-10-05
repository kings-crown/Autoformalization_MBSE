"""Offline API checks for frozen, independent GUI assessment and mutation jobs."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from bedrock_judging import BedrockTransport
from canonical_assertions import build_suite
from mutation_sources import source_packet
from review_canonical import CanonicalRunStore, now
from review_evaluation import _save, create_evaluation_router


MODEL = "package Candidate {\n    doc /* Source obligation. */\n    attribute voltage : Real;\n    require constraint { voltage <= 28; }\n}\n"


class DeferredExecutor:
    def __init__(self):
        self.tasks = []

    def submit(self, fn, *args, **kwargs):
        self.tasks.append((fn, args, kwargs))

    def drain(self):
        while self.tasks:
            fn, args, kwargs = self.tasks.pop(0)
            fn(*args, **kwargs)


class EvaluationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CanonicalRunStore(Path(self.temp.name))
        self.executor = DeferredExecutor()
        self.prompts = []
        self.source_calls = []
        self.rows = [{"id": "R1", "text": "The battery voltage shall be at most 28 V.",
                      "source": {"document": "Synthetic", "context": {"definitions": ["Voltage is the terminal voltage."]}}}]
        self.context = {"variables": [{"name": "voltage", "type": "Real", "unit": "V"}], "background": []}
        self.suite = build_suite(self.rows, [{"id": "R1", "assertions": [{"id": "bound",
            "statement": "Preserve the inclusive limit of 28 V.", "category": "boundary",
            "source_basis": [{"source_id": "R1", "quote": "at most 28 V"}]}]}], self.context,
            schema="sysml_assertions/1")  # Preserve historical three-assertion API fixtures.
        self.config = {"schema": "bedrock_judges/1", "judges": [{"model": name, "region": "us-east-1",
            "prices_per_million": {"input_tokens": 1, "output_tokens": 2}} for name in ("judge-one", "judge-two")]}
        self.run_id = "run-" + "a" * 16
        self.seed()
        outer = self
        class Client:
            def __init__(self, config):
                self.config = config
            def converse(self, **request):
                fields = json.loads(request["messages"][0]["content"][0]["text"])
                outer.prompts.append(fields)
                verdicts = []
                for index, assertion in enumerate(fields["assertions"]):
                    # One invalid comment-only pass; valid peers must survive.
                    line = 2 if self.config["model"] == "judge-one" and index == 0 else 4
                    state = getattr(outer, "fidelity_verdict", "pass") if assertion["category"] == "fidelity" else "pass"
                    verdicts.append({"id": assertion["id"], "status": state, "rationale": "Inspect the cited expression.",
                        "evidence": [{"start_line": line, "end_line": line}],
                        "counterexample": "Synthetic source/model discrepancy for protocol testing." if state == "fail" else None})
                text = json.dumps({"requirement_id": fields["target_requirement_id"], "assertions": verdicts})
                return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}},
                        "stopReason": "end_turn", "usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120}}
        def transport(config):
            return BedrockTransport(config, client_factory=Client)
        def source_runner(manifest, output, **options):
            self.source_calls.append((deepcopy(manifest), options))
            Path(output).mkdir()
            report = {"status": "completed", "summary": {"fixture": True}}
            (Path(output) / "report.json").write_text(json.dumps(report))
            return report
        self.app = FastAPI()
        self.app.include_router(create_evaluation_router(self.store, self.executor, transport_factory=transport, source_runner=source_runner))
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def seed(self, rows=None, **changes):
        run = {"id": self.run_id, "name": "Condition C solver success secret label", "status": "completed",
            "created_at": now(), "sources": deepcopy(rows or self.rows), "fixed_context": self.context,
            "development_scenarios": None, "model_text": MODEL,
            "canonical_result": {"configuration": {"model": "test-generator", "feedback_repair_budget": 2}},
            "analysis": {"status": "passed"}, "admission": "admitted_consistent_encoding", "evaluations": [], "reviews": []}
        run.update(changes)
        self.store.save(run)
        return run

    def post(self, **changes):
        payload = {"kind": "judge", "assertions": self.suite, "judge_config": self.config}
        payload.update(changes)
        return self.client.post(f"/api/workflow/runs/{self.run_id}/evaluations", json=payload)

    def fetch(self, eid):
        response = self.client.get(f"/api/workflow/runs/{self.run_id}/evaluations/{eid}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_frozen_blinded_partial_judgments_and_costs(self):
        initial = self.store.get(self.run_id)
        response = self.post()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.prompts, [])
        eid = response.json()["id"]
        self.store.update(self.run_id, model_text="externally changed after freezing")
        self.executor.drain()
        job = self.fetch(eid)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["report_status"], "incomplete")
        self.assertEqual(job["summary"]["joint"]["planned"], 3)
        self.assertEqual(job["summary"]["joint"]["pass"], 2)
        self.assertEqual(job["summary"]["joint"]["unreviewed"], 1)
        self.assertEqual(job["summary"]["partial_calls"], 1)
        self.assertEqual(job["billing"]["total_tokens"], 240)
        self.assertAlmostEqual(job["billing"]["estimated_cost_usd"], .00028)
        self.assertEqual(job["progress"]["finished_calls"], 2)
        for prompt in self.prompts:
            self.assertEqual(set(prompt), {"target_requirement_id", "source_packet", "fixed_context", "assertions", "abstraction_policy", "sysml_with_line_numbers"})
            self.assertEqual(prompt["source_packet"], self.rows)
            self.assertIn("voltage <= 28", prompt["sysml_with_line_numbers"])
            self.assertNotIn("externally changed", prompt["sysml_with_line_numbers"])
        final = self.store.get(self.run_id)
        self.assertEqual(final["canonical_result"], initial["canonical_result"])
        self.assertEqual(final["admission"], initial["admission"])
        self.assertEqual(final["reviews"], [])
        self.assertTrue(any("bedrock_calls" in a["path"] for a in self.store.artifacts(self.run_id)))

    def test_bad_source_context_and_prices_rejected_before_enqueue(self):
        altered = deepcopy(self.suite)
        altered["sources"][0]["source"]["context"] = {}
        self.assertEqual(self.post(assertions=altered).status_code, 422)
        self.store.update(self.run_id, fixed_context=None)
        self.assertEqual(self.post().status_code, 422)
        self.store.update(self.run_id, fixed_context=self.context)
        prices = deepcopy(self.config)
        prices["judges"][0].pop("prices_per_million")
        self.assertEqual(self.post(judge_config=prices).status_code, 422)
        self.assertEqual(self.post(feedback_repairs=1).status_code, 422)
        self.assertEqual(self.executor.tasks, [])
        self.assertEqual(self.store.get(self.run_id)["evaluations"], [])

    def test_v2_fidelity_is_reported_and_raw_verdicts_survive_gui_api(self):
        # Mock verdicts test API routing and retention, not empirical detection.
        authored = [{"id": row["id"], "assertions": [deepcopy(row["assertions"][0])]}
                    for row in self.suite["requirements"]]
        suite = build_suite(self.rows, authored, self.context)
        self.fidelity_verdict = "fail"
        self.store.update(self.run_id, canonical_result={
            "source_review": {"status": "passed", "private_note": "HIDDEN_SOURCE_REVIEW"}})
        response = self.post(assertions=suite)
        self.assertEqual(response.status_code, 202, response.text)
        self.executor.drain()
        job = self.fetch(response.json()["id"])
        self.assertEqual(job["summary"]["joint"]["planned"], 4)
        self.assertEqual(job["summary"]["joint"]["pass"], 2)
        self.assertEqual(job["summary"]["joint"]["unreviewed"], 1)
        self.assertEqual(job["summary"]["by_category"]["fidelity"]["fail"], 1)
        alignment = job["summary"]["source_alignment"]
        self.assertEqual(alignment, job["report"]["source_alignment"])
        self.assertEqual(alignment["pass"], 0)
        self.assertEqual(alignment["fail"], 1)
        self.assertGreater(alignment["pending_model_assertions"], 0)
        self.assertEqual(job["summary"]["fidelity"], job["report"]["fidelity"])
        self.assertEqual(job["summary"]["by_evidence_requirement"], job["report"]["by_evidence_requirement"])
        self.assertEqual(job["report"]["assertion_suite_schema"], "sysml_assertions/3")
        self.assertEqual(job["report"]["fidelity"]["joint"]["fail"], 1)
        raw_paths = list((self.store.directory(self.run_id) / "evaluations" /
                          response.json()["id"] / "report").glob("*/requirement-*/judge-*/response.json"))
        self.assertEqual(len(raw_paths), 2)
        for path in raw_paths:
            raw = json.loads(path.read_text())
            fidelity = next(item for item in raw["assertions"] if item["id"] == "ASSERT_0001_FIDELITY")
            self.assertEqual(fidelity["status"], "fail")
            self.assertTrue(fidelity["counterexample"])
        for prompt in self.prompts:
            self.assertNotIn("HIDDEN_SOURCE_REVIEW", json.dumps(prompt))
            self.assertNotIn("source_review", prompt)
            self.assertNotIn("admission", prompt)

    def test_only_terminal_run_and_one_evaluation_at_a_time(self):
        self.store.update(self.run_id, status="running")
        self.assertEqual(self.post().status_code, 409)
        self.store.update(self.run_id, status="completed")
        self.assertEqual(self.post().status_code, 202)
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(len(self.executor.tasks), 1)

    def test_empty_candidate_retains_denominator_without_paid_calls(self):
        self.store.update(self.run_id, model_text="")
        response = self.post()
        self.executor.drain()
        job = self.fetch(response.json()["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["summary"]["joint"]["unreviewed"], 3)
        self.assertEqual(job["billing"]["provider_calls"], 0)
        self.assertEqual(self.prompts, [])

    def manifest(self):
        value = json.loads((ROOT / "examples/mutations/scalar_source.json").read_text())
        self.seed(source_packet(value), fixed_context=None)
        return value

    @unittest.skipUnless(shutil.which("z3"), "Z3 executable unavailable")
    def test_formal_mutations_reuse_real_comparator_without_changing_model(self):
        manifest = self.manifest()
        response = self.post(kind="mutation_formal", assertions=None, judge_config=None, manifest=manifest)
        self.assertEqual(response.status_code, 202, response.text)
        self.executor.drain()
        job = self.fetch(response.json()["id"])
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["policy"]["max_model_calls"], 0)
        self.assertEqual(job["summary"]["mutant"]["detected"], 4)
        self.assertEqual(job["summary"]["control"]["false_alarms"], 0)
        self.assertEqual(self.store.get(self.run_id)["model_text"], MODEL)
        self.assertEqual(self.prompts, [])

    def test_source_campaign_has_explicit_separate_policy_and_budget(self):
        manifest = self.manifest()
        self.store.update(self.run_id, development_scenarios={"role": "development"})
        response = self.post(kind="mutation_source", assertions=None, judge_config=None, manifest=manifest,
                             max_generations=7, feedback_repairs=2)
        self.assertEqual(response.status_code, 202, response.text)
        policy = response.json()["policy"]
        self.assertEqual(policy["planned_workflow_invocations"], 7)
        self.assertEqual(policy["max_model_calls"], 21)
        self.assertEqual(policy["max_generation_and_repair_calls"], 21)
        self.assertEqual(policy["max_source_review_calls"], 0)
        self.assertEqual(policy["max_obligation_preparation_calls"], 0)
        self.assertFalse(policy["obligation_inventory_required"])
        self.assertFalse(policy["source_review_required"])
        self.assertEqual(policy["source_review_mode"], "embedded_in_feedback")
        self.assertEqual(policy["format_repairs"], 0)
        self.assertFalse(policy["deprecated_abstention_alias_used"])
        self.assertIsNone(policy["development_scenarios"])
        self.executor.drain()
        self.assertEqual(len(self.source_calls), 1)
        self.assertEqual(self.source_calls[0][1]["model"], "test-generator")
        self.assertEqual(self.source_calls[0][1]["feedback_repairs"], 2)
        self.assertNotIn("development_scenarios", self.source_calls[0][1])
        self.assertIsNone(self.fetch(response.json()["id"])["billing"]["estimated_cost_usd"])

    def test_source_campaign_legacy_alias_uses_the_same_single_feedback_budget(self):
        for budget, generation_per_workflow in ((0, 1), (2, 3), (5, 6)):
            with self.subTest(abstention_repairs=budget):
                self.run_id = f"run-{budget:016x}"
                manifest = self.manifest()
                response = self.post(kind="mutation_source", assertions=None, judge_config=None,
                    manifest=manifest, max_generations=7, abstention_repairs=budget)
                self.assertEqual(response.status_code, 202, response.text)
                policy = response.json()["policy"]
                self.assertEqual(policy["max_generation_and_repair_calls_per_workflow"], generation_per_workflow)
                self.assertEqual(policy["max_source_review_calls_per_workflow"], 0)
                self.assertEqual(policy["max_generation_and_repair_calls"], 7 * generation_per_workflow)
                self.assertEqual(policy["max_source_review_calls"], 0)
                self.assertEqual(policy["max_obligation_preparation_calls_per_workflow"], 0)
                self.assertEqual(policy["max_obligation_preparation_calls"], 0)
                self.assertEqual(policy["max_model_calls"], 7 * generation_per_workflow)
                self.assertEqual(policy["feedback_repairs"], budget)
                self.assertEqual(policy["deprecated_abstention_alias_used"], bool(budget))
                self.assertEqual(policy["format_repairs"], 0)
                self.executor.drain()
                self.assertEqual(self.source_calls[-1][1]["feedback_repairs"], budget)
                self.assertEqual(self.source_calls[-1][1]["abstention_repairs"], 0)

    def test_mutation_context_drift_and_excess_budget_are_not_silently_accepted(self):
        manifest = self.manifest()
        altered = deepcopy(manifest)
        altered["requirements"][0]["source"]["document"] = "Changed source"
        self.assertEqual(self.post(kind="mutation_formal", assertions=None, judge_config=None, manifest=altered).status_code, 422)
        self.assertEqual(self.post(kind="mutation_source", assertions=None, judge_config=None, manifest=manifest, max_generations=1).status_code, 422)
        self.assertEqual(self.post(kind="mutation_source", assertions=None, judge_config=None, manifest=manifest,
                                  feedback_repairs=1, abstention_repairs=1).status_code, 422)
        self.assertEqual(self.executor.tasks, [])

    def test_missing_or_pathlike_job_ids_do_not_expose_files(self):
        response = self.client.get(f"/api/workflow/runs/{self.run_id}/evaluations/eval-{'b'*16}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get(f"/api/workflow/runs/{self.run_id}/evaluations/not-an-id").status_code, 404)

    def test_worker_shutdown_preserves_failed_job(self):
        def fail(*args, **kwargs):
            raise RuntimeError("executor stopped")
        self.executor.submit = fail
        self.assertEqual(self.post().status_code, 503)
        jobs = self.store.get(self.run_id)["evaluations"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "failed")
        self.assertEqual(self.prompts, [])

    def test_malformed_billing_retains_report_and_known_peer_cost(self):
        eid = self.post().json()["id"]
        original_factory = self.executor.tasks[0][2]["transport_factory"]

        def corrupt_one_record(config):
            transport = original_factory(config)
            class Transport:
                def for_judge(self, index):
                    callback = transport.for_judge(index)
                    def invoke(*args):
                        result = callback(*args)
                        if index == 0:
                            path = next(Path(args[3]).glob("bedrock_calls/*/*/result.json"))
                            path.write_text("{")
                        return result
                    return invoke
            return Transport()

        self.executor.tasks[0][2]["transport_factory"] = corrupt_one_record
        self.executor.drain()
        job = self.fetch(eid)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["report_status"], "incomplete")
        self.assertEqual(job["report"]["joint"]["pass"], 2)
        self.assertEqual(job["billing"]["status"], "incomplete")
        self.assertEqual(job["billing"]["provider_calls"], 2)
        self.assertEqual(job["billing"]["unknown_cost_calls"], 1)
        self.assertAlmostEqual(job["billing"]["known_cost_usd"], .00014)
        self.assertIsNone(job["billing"]["estimated_cost_usd"])
        self.assertIsNone(job["billing"]["total_tokens"])
        self.assertEqual(len(job["billing"]["errors"]), 1)
        self.assertIn("JSONDecodeError", job["billing"]["errors"][0]["error"])
        self.assertEqual(self.store.get(self.run_id)["evaluations"][0]["status"], "completed")

    def test_billing_io_failure_cannot_discard_completed_assessment(self):
        eid = self.post().json()["id"]
        with patch("review_evaluation._billing", side_effect=OSError("unreadable billing directory")):
            self.executor.drain()
        job = self.fetch(eid)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["report"]["joint"]["planned"], 3)
        self.assertEqual(job["billing"]["status"], "unavailable")
        self.assertIsNone(job["billing"]["estimated_cost_usd"])
        self.assertIn("unreadable billing directory", job["billing"]["error"])

    def test_second_router_preserves_jobs_owned_by_live_process(self):
        eid = self.post().json()["id"]
        for status in ("queued", "running"):
            record = self.fetch(eid)
            record["status"] = status
            _save(self.store, self.run_id, record)
            create_evaluation_router(self.store, self.executor)
            self.assertEqual(self.fetch(eid)["status"], status)
            self.assertEqual(self.store.get(self.run_id)["evaluations"][0]["status"], status)
            self.assertEqual(len(self.executor.tasks), 1)
        self.executor.drain()
        self.assertEqual(self.fetch(eid)["status"], "completed")
        self.assertEqual(len(self.prompts), 2)

    def test_recovery_marks_only_orphaned_jobs_and_preserves_artifacts(self):
        eid = self.post().json()["id"]
        directory = self.store.directory(self.run_id) / "evaluations" / eid
        snapshot = (directory / "snapshot.json").read_bytes()
        partial = directory / "partial.txt"
        partial.write_text("Retained partial evidence")
        record = self.fetch(eid)
        record["status"] = "running"
        record["execution_owner"]["start_ticks"] = "expired-process-identity"
        _save(self.store, self.run_id, record)
        self.executor.tasks.clear()  # The recorded owning process has stopped.
        create_evaluation_router(self.store, self.executor)
        recovered = self.fetch(eid)
        self.assertEqual(recovered["status"], "failed")
        self.assertIn("owner stopped", recovered["error"])
        self.assertEqual((directory / "snapshot.json").read_bytes(), snapshot)
        self.assertEqual(partial.read_text(), "Retained partial evidence")
        self.assertEqual(self.executor.tasks, [])
        self.assertEqual(self.prompts, [])

    def test_job_and_temporary_metadata_symlinks_are_rejected(self):
        eid = self.post().json()["id"]
        directory = self.store.directory(self.run_id) / "evaluations" / eid
        record = self.fetch(eid)
        original_job = (directory / "job.json").read_bytes()
        target = Path(self.temp.name) / "outside-metadata.json"
        target.write_text("Preserve this file")
        original_run = self.store.get(self.run_id)
        for name in ("job.json.tmp", "job.json"):
            with self.subTest(name=name):
                path = directory / name
                if path.exists():
                    path.unlink()
                path.symlink_to(target)
                try:
                    with self.assertRaises(HTTPException) as raised:
                        _save(self.store, self.run_id, record)
                    self.assertEqual(raised.exception.status_code, 409)
                    self.assertEqual(target.read_text(), "Preserve this file")
                    self.assertEqual(self.store.get(self.run_id), original_run)
                finally:
                    path.unlink()
                    if name == "job.json":
                        path.write_bytes(original_job)

    def test_six_evaluation_limit_applies_across_candidates_and_releases_capacity(self):
        accepted = []
        for index in range(7):
            self.run_id = f"run-{index:016x}"
            self.seed()
            response = self.post()
            if index < 6:
                self.assertEqual(response.status_code, 202, response.text)
                accepted.append((self.run_id, response.json()))
            else:
                self.assertEqual(response.status_code, 429, response.text)
                self.assertEqual(self.store.get(self.run_id)["evaluations"], [])
                self.assertFalse((self.store.directory(self.run_id) / "evaluations").exists())
        self.assertEqual(len(self.executor.tasks), 6)
        self.assertEqual(self.prompts, [])
        freed_run, record = accepted[0]
        record.update(status="failed", error="Synthetic terminal outcome")
        _save(self.store, freed_run, record)
        self.assertEqual(self.post().status_code, 202)
        self.assertEqual(len(self.executor.tasks), 7)


if __name__ == "__main__":
    unittest.main()
