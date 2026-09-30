#!/usr/bin/env python3
"""Persistent localhost HTTP interface to the shared requirements review workflow.

Run: python scripts/review_server.py
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

import review_profile as profile
# Re-export existing core entry points for callers importing review_server.
from review_workflow import (
    process_identity, run_owner_active,
    WorkflowError, now, write_json, DesignReviewInput,
    RunInput, validate_analysis_selection, design_review_findings, ReviewInput,
    CandidateInspectionInput, BindingReviewInput, AssumptionReviewInput, ContractReviewInput,
    contract_review_hash, contract_blockers, RunStore, baseline_blockers,
    interpretation_presentation, collect_artifacts, json_digest, evidence_integrity,
    finish_run, diagnostics_text, run_job, create_run,
    candidate_requirements, check_saved_artifacts, inspect_bindings, record_assumption_review,
    record_contract_review, record_binding_review, record_review, ROOT,
    RUN_ID, STAGES, build_assumptions, assumption_blockers,
    assumption_review_hash, compiler_capability, compile_sysml, generate_sysml,
)


def create_app(data_dir: Path | None = None) -> FastAPI:
    store = RunStore(data_dir or Path(os.getenv("MBSE_REVIEW_DATA_DIR", str(ROOT / "out" / "review_workbench"))))
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="requirements-review")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        for run in store.all():
            if run["status"] in ("queued", "running") and not run_owner_active(run):
                store.update(run["id"], errors=run.get("errors", []) + ["The service stopped before this run completed. Create a new run to retry."])
                finish_run(store, run["id"], "failed")
        yield
        executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="MBSE model review prototype", lifespan=lifespan, docs_url=None, redoc_url=None)
    @app.exception_handler(WorkflowError)
    async def workflow_error(_request: Request, error: WorkflowError):
        return JSONResponse({"detail": error.detail}, status_code=error.status_code)

    app.state.store = store
    app.state.executor = executor
    # New requirements runs use the exact canonical CLI core. The earlier
    # design-review UI and API remain available for existing saved artifacts.
    from review_canonical import create_router as canonical_router
    from review_evaluation import create_evaluation_router
    workflow = canonical_router(store.root / "canonical", executor)
    app.state.workflow_store = workflow.workflow_store
    app.include_router(workflow)
    app.include_router(create_evaluation_router(workflow.workflow_store, executor))
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (origin and urlsplit(origin).netloc != request.headers.get("host")):
                return JSONResponse({"detail": "Use the local review application to submit changes."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get("/")
    def index():
        return FileResponse(ROOT / "prototypes/review-workbench/canonical.html", media_type="text/html")

    @app.get("/legacy")
    def legacy_index():
        return FileResponse(ROOT / "prototypes/review-workbench/index.html", media_type="text/html")

    @app.get("/api/config")
    def config():
        codex = bool(shutil.which("codex"))
        from review_configuration import solver_capability
        sample_path = ROOT / "examples/synthetic/behavior_response.json"
        behavior_sample = json.loads(sample_path.read_text()) if sample_path.is_file() else None
        return {"engines": [
            {"id": "local", "label": "Local constraint profile", "available": True,
             "description": "Exact scalar/timing grammar, existing solver runner, and actual SysML compilation. Unrecognized clauses remain visible and unverified."},
            {"id": "pipeline", "label": "Existing Codex pipeline", "available": codex,
             "description": "Uses the configured Codex account to generate requirements and SysML artifacts. A separate design proposal requires Propose design for review mode. Sends requirements to that provider; interpretations require engineering review."}],
            "analysis_modes": [
                {"id": "requirements", "label": "Requirements model", "default": True},
                {"id": "propose_design", "label": "Propose design for review", "requires_engine": "pipeline"},
                {"id": "check_design", "label": "Check reviewed design", "requires_review": True}],
            "capabilities": {"solver": solver_capability(),
                             "compiler": compiler_capability()},
            "sample": profile.SAMPLE, "behavior_sample": behavior_sample,
            "contract_sample": json.loads((ROOT / "examples/synthetic/contracts_voltage.json").read_text()), "limits": {"max_requirements": profile.MAX_REQUIREMENTS, "max_bytes": profile.MAX_INPUT_BYTES},
            "acceptance_scope": "Local prototype model and interpretation only; no authenticated organizational approval or source amendment."}


    @app.post("/api/behavior/inspect")
    def inspect_behavior(payload: CandidateInspectionInput):
        from review_candidate import inspect_candidate
        try:
            if payload.edits:
                raise ValueError("Use the edit endpoint to apply expression edits.")
            return inspect_candidate(payload.behavior, candidate_requirements(payload), payload.provenance, payload.origin)
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/behavior/edit")
    def edit_behavior(payload: CandidateInspectionInput):
        from review_candidate import edit_candidate
        try:
            return edit_candidate(payload.behavior, candidate_requirements(payload), payload.edits, payload.provenance, payload.origin)
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/runs")
    def list_runs():
        return {"runs": store.all()}

    @app.post("/api/runs", status_code=202)
    def submit(payload: RunInput):
        run = create_run(store, payload)
        executor.submit(run_job, store, run["id"])
        return run

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        run = store.get(run_id)
        view = interpretation_presentation(run, store.directory(run_id))
        if view is not None:
            run["interpretation_view"] = view
        return run

    @app.get("/api/runs/{run_id}/artifacts/{name}")
    def download(run_id: str, name: str):
        run = store.get(run_id)
        artifact = next((a for a in run["artifacts"] if a["name"] == name), None)
        if artifact is None:
            raise HTTPException(404, "Artifact not found.")
        path = store.directory(run_id) / name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != store.directory(run_id).resolve():
            raise HTTPException(404, "Artifact not found.")
        if profile.digest(path.read_bytes()) != artifact["sha256"]:
            raise HTTPException(409, "Artifact changed after analysis; its review evidence is stale.")
        return FileResponse(path, filename=name, media_type="application/octet-stream")

    @app.get("/api/runs/{run_id}/packet")
    def packet(run_id: str):
        run = store.get(run_id)
        files = {}
        for artifact in run["artifacts"]:
            path = store.directory(run_id) / artifact["name"]
            if path.is_symlink() or not path.is_file() or profile.digest(path.read_bytes()) != artifact["sha256"]:
                raise HTTPException(409, "An artifact changed after analysis; create a new run before exporting evidence.")
            files[artifact["name"]] = {"sha256": artifact["sha256"], "text": path.read_text(encoding="utf-8", errors="replace")}
        response = JSONResponse({"schema": "mbse-review-packet/1", "exported_at": now(), "run": run, "files": files,
                                 "acceptance_scope": "Local prototype; no authenticated organizational authorization."})
        response.headers["Content-Disposition"] = f'attachment; filename="{run_id}-review-packet.json"'
        return response

    @app.post("/api/runs/{run_id}/assumptions/{assumption_id}/reviews", status_code=201)
    def review_assumption(run_id: str, assumption_id: str, payload: AssumptionReviewInput):
        return record_assumption_review(store, run_id, assumption_id, payload)

    @app.post("/api/runs/{run_id}/contracts/{contract_id}/reviews", status_code=201)
    def review_contract(run_id: str, contract_id: str, payload: ContractReviewInput):
        return record_contract_review(store, run_id, contract_id, payload)


    @app.get("/api/runs/{run_id}/bindings")
    def get_bindings(run_id: str):
        return inspect_bindings(store, run_id)

    @app.post("/api/runs/{run_id}/bindings/reviews", status_code=201)
    def review_binding(run_id: str, payload: BindingReviewInput):
        return record_binding_review(store, run_id, payload)

    @app.post("/api/runs/{run_id}/reviews", status_code=201)
    def review(run_id: str, payload: ReviewInput):
        return record_review(store, run_id, payload)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.data_dir), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
