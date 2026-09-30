"""Offline API checks for frozen, independent GUI assessment and mutation jobs."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
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
        self.source_calls = []
        self.fixture = json.loads((ROOT / "examples/mutations/scalar_source.json").read_text())
        self.rows = source_packet(self.fixture)
        self.context = None
        self.run_id = "run-" + "a" * 16
        self.seed()
        def source_runner(manifest, output, **options):
            self.source_calls.append((deepcopy(manifest), options))
            Path(output).mkdir()
            report = {"status": "completed", "summary": {"fixture": True}}
            (Path(output) / "report.json").write_text(json.dumps(report))
            return report
        self.app = FastAPI()
        self.app.include_router(create_evaluation_router(self.store, self.executor, source_runner=source_runner))
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def seed(self, rows=None, **changes):
        run = {"id": self.run_id, "name": "Completed source-model candidate", "status": "completed",
            "created_at": now(), "sources": deepcopy(rows or self.rows), "fixed_context": self.context,
            "development_scenarios": None, "model_text": MODEL,
            "canonical_result": {"configuration": {"model": "test-generator", "feedback_repair_budget": 2}},
            "analysis": {"status": "passed"}, "admission": "admitted_consistent_encoding", "evaluations": [], "reviews": []}
        run.update(changes)
        self.store.save(run)
        return run

    def post(self, **changes):
        payload = {"kind": "mutation_source", "manifest": deepcopy(self.fixture), "max_generations": 7}
        payload.update(changes)
        return self.client.post(f"/api/workflow/runs/{self.run_id}/evaluations", json=payload)

    def fetch(self, eid):
        response = self.client.get(f"/api/workflow/runs/{self.run_id}/evaluations/{eid}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_unavailable_kind_and_extra_assessment_fields_are_rejected(self):
        self.assertEqual(self.post(kind="judge").status_code, 422)
        self.assertEqual(self.post(assertions={}).status_code, 422)
        self.assertEqual(self.post(judge_config={}).status_code, 422)
        self.assertEqual(self.executor.tasks, [])
        self.assertEqual(self.store.get(self.run_id)["evaluations"], [])

    def test_source_job_retains_frozen_inputs_and_parent_evidence(self):
        initial = self.store.get(self.run_id)
        response = self.post()
        self.assertEqual(response.status_code, 202, response.text)
        eid = response.json()["id"]
        directory = self.store.directory(self.run_id) / "evaluations" / eid
        frozen = json.loads((directory / "snapshot.json").read_text())
        self.assertEqual(frozen["sources"], self.rows)
        self.assertEqual(frozen["sysml"], MODEL)
        self.assertEqual(self.source_calls, [])
        self.executor.drain()
        job = self.fetch(eid)
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(self.source_calls[0][0], json.loads((directory / "inputs.json").read_text())["manifest"])
        final = self.store.get(self.run_id)
        for key in ("sources", "model_text", "canonical_result", "admission", "reviews"):
            self.assertEqual(final[key], initial[key])

    def test_only_terminal_run_and_one_evaluation_at_a_time(self):
        self.store.update(self.run_id, status="running")
        self.assertEqual(self.post().status_code, 409)
        self.store.update(self.run_id, status="completed")
        self.assertEqual(self.post().status_code, 202)
        self.assertEqual(self.post().status_code, 409)
        self.assertEqual(len(self.executor.tasks), 1)

    def manifest(self):
        value = json.loads((ROOT / "examples/mutations/scalar_source.json").read_text())
        self.seed(source_packet(value), fixed_context=None)
        return value

    @unittest.skipUnless(shutil.which("z3"), "Z3 executable unavailable")
    def test_formal_mutations_reuse_real_comparator_without_changing_model(self):
        manifest = self.manifest()
        response = self.post(kind="mutation_formal", manifest=manifest)
        self.assertEqual(response.status_code, 202, response.text)
        self.executor.drain()
        job = self.fetch(response.json()["id"])
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["policy"]["max_model_calls"], 0)
        self.assertEqual(job["summary"]["mutant"]["detected"], 4)
        self.assertEqual(job["summary"]["control"]["false_alarms"], 0)
        self.assertEqual(self.store.get(self.run_id)["model_text"], MODEL)

    def test_source_campaign_has_explicit_separate_policy_and_budget(self):
        manifest = self.manifest()
        self.store.update(self.run_id, development_scenarios={"role": "development"})
        response = self.post(kind="mutation_source", manifest=manifest,
                             max_generations=7, feedback_repairs=2)
        self.assertEqual(response.status_code, 202, response.text)
        policy = response.json()["policy"]
        self.assertEqual(policy["planned_workflow_invocations"], 7)
        self.assertEqual(policy["max_model_calls"], 21)
        self.assertIsNone(policy["development_scenarios"])
        self.executor.drain()
        self.assertEqual(len(self.source_calls), 1)
        self.assertEqual(self.source_calls[0][1]["model"], "test-generator")
        self.assertEqual(self.source_calls[0][1]["feedback_repairs"], 2)
        self.assertNotIn("development_scenarios", self.source_calls[0][1])
        self.assertIsNone(self.fetch(response.json()["id"])["billing"]["estimated_cost_usd"])

    def test_mutation_context_drift_and_excess_budget_are_not_silently_accepted(self):
        manifest = self.manifest()
        altered = deepcopy(manifest)
        altered["requirements"][0]["source"]["document"] = "Changed source"
        self.assertEqual(self.post(kind="mutation_formal", manifest=altered).status_code, 422)
        self.assertEqual(self.post(kind="mutation_source", manifest=manifest, max_generations=1).status_code, 422)
        self.assertEqual(self.post(kind="mutation_source", manifest=manifest,
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
        self.assertEqual(len(self.source_calls), 1)

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
        freed_run, record = accepted[0]
        record.update(status="failed", error="Synthetic terminal outcome")
        _save(self.store, freed_run, record)
        self.assertEqual(self.post().status_code, 202)
        self.assertEqual(len(self.executor.tasks), 7)


if __name__ == "__main__":
    unittest.main()
