"""Independent GUI evaluation jobs using the canonical CLI evaluators.

The source packet and selected SysML are copied before enqueueing a job. Judges
and mutation reference answers never enter the parent conversion or its repair
history. A completed job can still contain incomplete assessment evidence.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import threading
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
    kind: Literal["judge", "mutation_formal", "mutation_source"]
    assertions: dict | None = None
    judge_config: dict | None = None
    manifest: dict | None = None
    judge_workers: int = Field(default=2, ge=1, le=2, strict=True)
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
    if payload.kind == "judge":
        from canonical_assertions import validate_suite
        from bedrock_judging import validate_config
        if payload.manifest is not None or payload.feedback_repairs or payload.abstention_repairs:
            raise ValueError("Independent judging accepts assertions and judge configuration, without repair or mutation inputs.")
        suite = validate_suite(payload.assertions, snapshot["sources"], snapshot["fixed_context"])
        config = validate_config(payload.judge_config)
        for judge in config["judges"]:
            if not {"input_tokens", "output_tokens"} <= set(judge["prices_per_million"]):
                raise ValueError("Supply input_tokens and output_tokens prices_per_million for both judges so cost can be recorded.")
        return {"assertions": suite, "judge_config": config}, {
            "planned_calls": 2 * len(suite["requirements"]), "max_model_calls": 2 * len(suite["requirements"]),
            "planned_assertions": sum(len(r["assertions"]) for r in suite["requirements"]),
            "generation_feedback": False, "judge_workers": payload.judge_workers,
            "scope": "LLM-assessed source preservation in the frozen SysML; not formal proof or engineer approval."}
    from mutation_stress import validate_manifest
    from mutation_sources import source_packet
    if payload.assertions is not None or payload.judge_config is not None:
        raise ValueError("Mutation jobs accept a manifest, not judge assertions or judge configuration.")
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
            raise ValueError("abstention_repairs is a deprecated alias for feedback_repairs; supply only one budget.")
        count = sum("text" in v for v in manifest["variants"])
        if not count:
            raise ValueError("Source mutation needs at least one variant with changed source text.")
        planned = 1 + count
        if planned > payload.max_generations:
            raise ValueError(f"Campaign needs {planned} workflow invocations; maximum is {payload.max_generations}.")
        feedback_repairs = payload.feedback_repairs or payload.abstention_repairs
        generation_bound = 1 + feedback_repairs
        review_bound = preparation_bound = 0
        policy = {"condition": "C", "repetitions": 1, "planned_workflow_invocations": planned,
                  "max_generation_and_repair_calls_per_workflow": generation_bound,
                  "max_source_review_calls_per_workflow": review_bound,
                  "max_generation_and_repair_calls": planned * generation_bound,
                  "max_source_review_calls": planned * review_bound,
                  "max_obligation_preparation_calls_per_workflow": preparation_bound,
                  "max_obligation_preparation_calls": planned * preparation_bound,
                  "max_model_calls": planned * (generation_bound + review_bound + preparation_bound),
                  "source_review_required": False,
                  "source_review_mode": "embedded_in_feedback",
                  "obligation_inventory_required": False,
                  "generation_budget_scope": "max_generations bounds complete workflows; each workflow uses one initial generation and up to feedback_repairs source-grounded proposals. No separate inventory or source-review calls; initial format correction is disabled. Actual calls may stop earlier.",
                  "feedback_repairs": feedback_repairs, "abstention_repairs": 0,
                  "deprecated_abstention_alias_used": bool(payload.abstention_repairs), "format_repairs": 0,
                  "max_generations": payload.max_generations, "development_scenarios": None,
                  "model": snapshot["conversion_configuration"].get("model"),
                  "generation_feedback": "Only each trial's source and declared C diagnostics; no evaluation reference answers.",
                  "scope": "Separate C source-regeneration campaign with the manifest's fixed vocabulary. Does not modify the selected candidate or replay a scenario-assisted policy."}
    policy.update(comparison_context_matches_parent=context_matches,
                  reference_status=manifest.get("reference", {}).get("status", "unspecified"),
                  context_scope="The manifest declares the comparison vocabulary/background. Matching formulas does not establish source fidelity or symbol-meaning equivalence.")
    return {"manifest": manifest}, policy


def _billing(output):
    output = Path(output)
    calls, errors = [], []
    for path in sorted(output.glob("**/bedrock_calls/*/*/result.json")):
        relative = path.relative_to(output).as_posix()
        try:
            value = read_json(safe_artifact(output, relative))
            if (not isinstance(value, dict) or not isinstance(value.get("usage", {}), dict)
                    or not isinstance(value.get("cost", {}), dict)):
                raise ValueError("Call result, usage and cost must be objects.")
        except (ValueError, OSError, HTTPException) as exc:
            errors.append({"path": relative, "error": f"{type(exc).__name__}: {exc}"})
            # A damaged call record must not disappear from the subtotal or
            # turn its unknown charge into zero. Keep usable peer records.
            value = {"status": "unavailable", "usage": {}, "cost": {"usd": None}}
        calls.append(value)
    def total(key):
        values = [c.get("usage", {}).get(key) for c in calls]
        return sum(values) if all(type(v) is int and v >= 0 for v in values) else None
    amounts = [c.get("cost", {}).get("usd") for c in calls]
    known = [v for v in amounts if isinstance(v, (int, float)) and not isinstance(v, bool)
             and math.isfinite(v) and v >= 0]
    return {"status": "incomplete" if errors or len(known) != len(calls) else "complete", "errors": errors,
            "provider_calls": len(calls), "successful_provider_calls": sum(c.get("status") == "ok" for c in calls),
            "input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"),
            "total_tokens": total("total_tokens"), "known_cost_usd": sum(known),
            "estimated_cost_usd": sum(known) if len(known) == len(calls) else None,
            "unknown_cost_calls": len(calls) - len(known),
            "scope": "Judge call subtotal using supplied prices; generation and preparation excluded."}


def _job(store, run_id, evaluation_id, *, transport_factory=None, source_runner=None):
    path = _path(store, run_id, evaluation_id)
    record = read_json(path / "job.json")
    snapshot = read_json(path / "snapshot.json")
    inputs = read_json(path / "inputs.json")
    record.update(status="running", started_at=_now(), execution_owner=_owner())
    _save(store, run_id, record)
    tick = time.monotonic()
    output = path / "report"
    try:
        if record["kind"] == "judge":
            from bedrock_judging import BedrockTransport
            from canonical_assertion_judging import evaluate_packets
            transport = (transport_factory or BedrockTransport)(inputs["judge_config"])
            progress_lock = threading.Lock()
            record["progress"] = {"started_calls": 0, "finished_calls": 0, "planned_calls": record["policy"]["planned_calls"]}
            def monitored(callback):
                def call(*args):
                    with progress_lock:
                        record["progress"]["started_calls"] += 1
                        _save(store, run_id, record)
                    try:
                        return callback(*args)
                    finally:
                        with progress_lock:
                            record["progress"]["finished_calls"] += 1
                            _save(store, run_id, record)
                return call
            packet = {"id": "candidate-0001", "requirements": snapshot["sources"],
                      "sysml": snapshot["sysml"], "fixed_context": snapshot["fixed_context"]}
            config = inputs["judge_config"]
            report = evaluate_packets([packet], inputs["assertions"], [j["model"] for j in config["judges"]], output,
                [monitored(transport.for_judge(i)) for i in range(2)], configuration=config,
                workers=record["policy"]["judge_workers"])
            requirement_rows = [r for p in report["results"] for r in p["requirements"]]
            record["summary"] = {**report["summary"], "joint": report["joint"], "by_category": report["by_category"],
                "source_alignment": deepcopy(report.get("source_alignment")),
                "fidelity": deepcopy(report.get("fidelity")),
                "by_evidence_requirement": deepcopy(report.get("by_evidence_requirement")),
                "macro_requirement_joint_pass_rate": (sum(r["joint"]["pass_rate"] for r in requirement_rows) / len(requirement_rows)
                                                       if requirement_rows else None)}
        elif record["kind"] == "mutation_formal":
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
                      report_path=f"evaluations/{evaluation_id}/report/" + ("judgments.json" if record["kind"] == "judge" else "report.json"))
    except Exception as exc:
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        record.update(completed_at=_now(), latency_seconds=time.monotonic() - tick)
        if record["kind"] == "judge":
            try:
                record["billing"] = _billing(output)
            except Exception as exc:
                # Billing is supporting evidence: failure to read it must not
                # discard a completed assessment or leave the job running.
                record["billing"] = {"status": "unavailable", "estimated_cost_usd": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "scope": "Judge billing could not be read; retained call artifacts may contain partial usage."}
        else:
            record["billing"] = {"estimated_cost_usd": 0 if record["kind"] == "mutation_formal" else None,
                                 "scope": "Formal comparisons make no model calls." if record["kind"] == "mutation_formal" else "Generation transport usage remains in campaign records; unknown cost is not zero."}
        _save(store, run_id, record)


def create_evaluation_router(store, executor, *, transport_factory=None, source_runner=None):
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
            executor.submit(_job, store, run_id, eid, transport_factory=transport_factory, source_runner=source_runner)
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
        if progress.is_file() and not progress.is_symlink() and record["kind"] != "judge":
            progress = safe_artifact(store.directory(run_id), f"evaluations/{evaluation_id}/report/progress.json")
            try:
                record["progress"] = read_json(progress)
            except (ValueError, OSError):
                pass  # The canonical campaign may currently be updating progress.
        return record

    return router
