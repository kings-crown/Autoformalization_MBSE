"""Independent GUI evaluation jobs using the canonical CLI evaluators.

The source packet and selected SysML are copied before enqueueing a job.
Mutation reference answers never enter the parent conversion or its repair
history. A completed job can still contain incomplete assessment evidence.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import time
from typing import Literal
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from canonical_cli import _fixed_context, read_json, write_json
from review_canonical import _owner, _owner_active, safe_artifact

EVALUATION_ID = re.compile(r"eval-[0-9a-f]{16}\Z")
MAX_EVALUATION_BYTES = 4_000_000
MAX_ACTIVE_EVALUATIONS = 6


class EvaluationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["mutation_formal", "mutation_source"]
    manifest: dict | None = None
    feedback_repairs: int = Field(default=0, ge=0, le=5, strict=True)
    abstention_repairs: int = Field(default=0, ge=0, le=5, strict=True)
    max_generations: int = Field(default=20, ge=1, le=50, strict=True)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _path(store, run_id, evaluation_id):
    if not EVALUATION_ID.fullmatch(evaluation_id):
        raise HTTPException(404, "Evaluation not found.")
    root = store.directory(run_id)
    path = root / "evaluations" / evaluation_id
    if root.is_symlink() or any(p.is_symlink() for p in (root / "evaluations", path)):
        raise HTTPException(404, "Evaluation not found.")
    return path


def _public(record):
    return {k: deepcopy(v) for k, v in record.items() if k not in {"snapshot", "inputs", "report"}}


def _save(store, run_id, record):
    """Update only evaluation metadata; conversion and review records stay fixed."""
    with store.lock:
        from review_canonical import write_json as atomic_json
        path = _path(store, run_id, record["id"]) / "job.json"
        if path.is_symlink() or path.with_name(path.name + ".tmp").is_symlink():
            raise HTTPException(409, "Evaluation metadata path is not a regular artifact.")
        atomic_json(path, record)
        run = store.get(run_id)
        evaluations = [e for e in run.get("evaluations", []) if e["id"] != record["id"]]
        evaluations.append(_public(record))
        store.update(run_id, evaluations=evaluations)


def _source_rows(run):
    rows = run.get("sources", run.get("source_packet", run.get("requirements", [])))
    if not isinstance(rows, list) or not rows:
        raise ValueError("The run has no frozen contextualized source packet.")
    return deepcopy([{k: row[k] for k in ("id", "text", "source") if k in row} for row in rows])


def _snapshot(run):
    # The request's fixed context is distinct from LLM-generated assumptions.
    context = _fixed_context(run.get("fixed_context"))
    result = run.get("canonical_result") or {}
    return {"sources": _source_rows(run), "fixed_context": context,
            "sysml": run.get("model_text") or "",
            "conversion_configuration": deepcopy(result.get("configuration", run.get("configuration", {}))),
            "development_scenarios": deepcopy(run.get("development_scenarios")),
            "boundary": "Frozen conversion input and candidate; evaluation never updates or repairs this candidate."}


def _validate(payload, snapshot):
    serialized = json.dumps(payload.model_dump(), ensure_ascii=False, allow_nan=False)
    if len(serialized.encode("utf-8")) > MAX_EVALUATION_BYTES:
        raise ValueError("Evaluation inputs exceed the 4 MB limit.")
    from mutation_stress import validate_manifest
    from mutation_sources import source_packet
    manifest = validate_manifest(payload.manifest)
    if source_packet(manifest) != snapshot["sources"]:
        raise ValueError("Mutation manifest must preserve this run's full source packet, including IDs, order, wording and context. Prepare a separate source revision for a different packet.")
    parent_context = snapshot["fixed_context"]
    from mutation_sources import same_context
    context_matches = same_context({k: parent_context[k] for k in ("variables", "background")}, manifest["context"]) if parent_context else None
    if payload.kind == "mutation_formal":
        if payload.feedback_repairs or payload.abstention_repairs:
            raise ValueError("Formal mutation comparison makes no model calls and accepts no repair budget.")
        policy = {"planned_variants": len(manifest["variants"]), "max_model_calls": 0,
                  "generation_feedback": False,
                  "scope": "Manifest reference formulas versus explicit edits; does not extract or verify the selected SysML."}
    else:
        if payload.feedback_repairs and payload.abstention_repairs:
            raise ValueError("Choose general feedback or abstention recovery, not both.")
        count = sum("text" in v for v in manifest["variants"])
        if not count:
            raise ValueError("Source mutation needs at least one variant with changed source text.")
        planned = 1 + count
        if planned > payload.max_generations:
            raise ValueError(f"Campaign needs {planned} workflow invocations; maximum is {payload.max_generations}.")
        policy = {"condition": "C", "repetitions": 1, "planned_workflow_invocations": planned,
                  "max_model_calls": planned * (1 + payload.feedback_repairs + 2 * payload.abstention_repairs),
                  "feedback_repairs": payload.feedback_repairs, "abstention_repairs": payload.abstention_repairs,
                  "max_generations": payload.max_generations, "development_scenarios": None,
                  "model": snapshot["conversion_configuration"].get("model"),
                  "generation_feedback": "Only each trial's source and declared C diagnostics; no evaluation reference answers.",
                  "scope": "Separate C source-regeneration campaign with the manifest's fixed vocabulary. Does not modify the selected candidate or replay a scenario-assisted policy."}
    policy.update(comparison_context_matches_parent=context_matches,
                  reference_status=manifest.get("reference", {}).get("status", "unspecified"),
                  context_scope="The manifest declares the comparison vocabulary/background. Matching formulas does not establish source fidelity or symbol-meaning equivalence.")
    return {"manifest": manifest}, policy


def _job(store, run_id, evaluation_id, *, source_runner=None):
    path = _path(store, run_id, evaluation_id)
    record = read_json(path / "job.json")
    inputs = read_json(path / "inputs.json")
    record.update(status="running", started_at=_now(), execution_owner=_owner())
    _save(store, run_id, record)
    tick = time.monotonic()
    output = path / "report"
    try:
        if record["kind"] == "mutation_formal":
            from mutation_stress import run_formal
            report = run_formal(inputs["manifest"], output)
            record["summary"] = report["summary"]
        else:
            from mutation_sources import run_source
            policy = record["policy"]
            report = (source_runner or run_source)(inputs["manifest"], output, engine="pipeline", repetitions=1,
                max_generations=policy["max_generations"], model=policy["model"],
                feedback_repairs=policy["feedback_repairs"], abstention_repairs=policy["abstention_repairs"],
                generation_timeout_seconds=3600)
            record["summary"] = report["summary"]
        record.update(status="completed", report_status=report.get("status"),
                      report_path=f"evaluations/{evaluation_id}/report/report.json")
    except Exception as exc:
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        record.update(completed_at=_now(), latency_seconds=time.monotonic() - tick)
        record["billing"] = {"estimated_cost_usd": 0 if record["kind"] == "mutation_formal" else None,
                             "scope": "Formal comparisons make no model calls." if record["kind"] == "mutation_formal" else "Generation transport usage remains in campaign records; unknown cost is not zero."}
        _save(store, run_id, record)


def create_evaluation_router(store, executor, *, source_runner=None):
    router = APIRouter(prefix="/api/workflow")

    # Another application instance may still own a live worker. Recover only
    # orphaned jobs; preserve their artifacts without resubmitting paid work.
    with store.lock:
        for run in store.all():
            for job in run.get("evaluations", []):
                if job.get("status") in {"queued", "running"}:
                    path = _path(store, run["id"], job["id"]) / "job.json"
                    if path.is_file() and not path.is_symlink():
                        record = read_json(path)
                        if record.get("status") not in {"queued", "running"} or _owner_active(record.get("execution_owner")):
                            continue
                        record.update(status="failed", error="The evaluation owner stopped before completion. Partial evidence is preserved.", completed_at=_now())
                        _save(store, run["id"], record)

    @router.post("/runs/{run_id}/evaluations", status_code=202)
    def submit(run_id: str, payload: EvaluationInput):
        with store.lock:
            run = store.get(run_id)
            if run["status"] not in {"completed", "failed"}:
                raise HTTPException(409, "Wait until conversion stops before freezing an assessment candidate.")
            if any(job.get("status") in {"queued", "running"} for job in run.get("evaluations", [])):
                raise HTTPException(409, "An evaluation is already queued or running for this candidate.")
            if sum(job.get("status") in {"queued", "running"}
                   for saved in store.all() for job in saved.get("evaluations", [])) >= MAX_ACTIVE_EVALUATIONS:
                raise HTTPException(429, "Six evaluations are active or queued; wait for an evaluation to finish.")
            try:
                snapshot = _snapshot(run)
                inputs, policy = _validate(payload, snapshot)
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                raise HTTPException(422, str(exc)) from exc
            eid = "eval-" + uuid.uuid4().hex[:16]
            path = _path(store, run_id, eid)
            path.mkdir(parents=True, exist_ok=False)
            write_json(path / "snapshot.json", snapshot)
            write_json(path / "inputs.json", inputs)
            record = {"schema": "workbench_evaluation/1", "id": eid, "run_id": run_id,
                      "kind": payload.kind, "status": "queued", "created_at": _now(), "policy": policy,
                      "execution_owner": _owner(),
                      "report_status": "not_run", "summary": None, "error": None}
            _save(store, run_id, record)
        try:
            executor.submit(_job, store, run_id, eid, source_runner=source_runner)
        except Exception as exc:
            record.update(status="failed", error=f"Could not start evaluation worker: {exc}", completed_at=_now())
            _save(store, run_id, record)
            raise HTTPException(503, "Could not start evaluation worker; the frozen request was retained.") from exc
        return record

    @router.get("/runs/{run_id}/evaluations/{evaluation_id}")
    def get(run_id: str, evaluation_id: str):
        run = store.get(run_id)
        if not any(e["id"] == evaluation_id for e in run.get("evaluations", [])):
            raise HTTPException(404, "Evaluation not found.")
        path = _path(store, run_id, evaluation_id)
        from review_canonical import safe_artifact
        record = read_json(safe_artifact(store.directory(run_id), f"evaluations/{evaluation_id}/job.json"))
        if record.get("report_path"):
            report = safe_artifact(store.directory(run_id), record["report_path"])
            record["report"] = read_json(report)
        progress = path / "report" / "progress.json"
        if progress.is_file() and not progress.is_symlink():
            progress = safe_artifact(store.directory(run_id), f"evaluations/{evaluation_id}/report/progress.json")
            try:
                record["progress"] = read_json(progress)
            except (ValueError, OSError):
                pass  # The canonical campaign may currently be updating progress.
        return record

    return router
