#!/usr/bin/env python3
"""Persistent localhost requirements-to-SysML review application.

Run: python scripts/review_server.py
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Literal
from urllib.parse import urlsplit
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

import review_profile as profile
from review_assumptions import build_assumptions, assumption_blockers, assumption_review_hash
from review_sysml import compiler_capability, compile_sysml, generate_sysml

ROOT = Path(__file__).resolve().parent.parent
RUN_ID = re.compile(r"run-[0-9a-f]{16}\Z")
STAGES = [("extraction", "Read requirements"), ("interpretation", "Build typed meaning"),
          ("analysis", "Check constraints"), ("generation", "Generate SysML v2"),
          ("contracts", "Connect shared contracts"),
          ("behavior", "Check temporal behavior"),
          ("compilation", "Compile model")]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="Requirements model", min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=profile.MAX_INPUT_BYTES)
    format: Literal["text", "csv", "json"] = "text"
    engine: Literal["local", "pipeline"] = "local"
    behavior: dict | None = None
    parent_run_id: str | None = None
    revision_rationale: str | None = Field(default=None, max_length=4000)


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_hash: str
    evidence_hash: str
    reviewer: str = Field(min_length=1, max_length=200)
    decision: Literal["recommend", "defer", "approve"]
    rationale: str = Field(min_length=1, max_length=6000)
    scope: str = Field(default="Generated model and interpretation for this run", max_length=2000)
    acknowledge: bool = False
    assumption_review_hash: str | None = None
    contract_review_hash: str | None = None
    acknowledge_contract_changes: bool = False


class AssumptionReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_hash: str
    evidence_hash: str
    assumption_review_hash: str
    reviewer: str = Field(min_length=1, max_length=200)
    decision: Literal["accept", "reject", "defer"]
    rationale: str = Field(min_length=1, max_length=6000)


class ContractReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_hash: str
    evidence_hash: str
    contract_review_hash: str
    reviewer: str = Field(min_length=1, max_length=200)
    decision: Literal["accept", "reject", "defer"]
    rationale: str = Field(min_length=1, max_length=6000)


def contract_review_hash(reviews: list[dict]) -> str:
    return profile.digest(json.dumps(reviews, sort_keys=True, ensure_ascii=False))


def contract_blockers(run: dict) -> list[str]:
    bundle = run.get("contracts")
    if not bundle:
        return ["This run has no shared-contract snapshot; source-to-contract review remains unavailable."]
    latest = {r["contract_id"]: r for r in run.get("contract_reviews", [])
              if r.get("source_hash") == run.get("source_hash") and r.get("evidence_hash") == run.get("evidence_hash")}
    entries = bundle.get("contracts", [])
    pending = [c["id"] for c in entries if latest.get(c["id"], {}).get("decision") != "accept"]
    unresolved = [c["id"] for c in entries if c.get("status") == "needs_interpretation"]
    out = []
    if pending:
        out.append(f"{len(pending)} contract interpretations remain pending, rejected, or deferred; inspect source, rule, model, evidence and proposed changes.")
    if unresolved:
        out.append(f"{len(unresolved)} requirements have no executable shared contract; review decisions cannot supply missing formalization.")
    return out


class RunStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def directory(self, run_id: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise HTTPException(404, "Run not found.")
        return self.root / run_id

    def get(self, run_id: str) -> dict:
        with self.lock:
            try:
                return json.loads((self.directory(run_id) / "run.json").read_text())
            except FileNotFoundError:
                raise HTTPException(404, "Run not found.") from None

    def save(self, run: dict) -> None:
        with self.lock:
            directory = self.directory(run["id"])
            directory.mkdir(exist_ok=True)
            write_json(directory / "run.json", run)

    def update(self, run_id: str, **changes) -> dict:
        with self.lock:
            run = self.get(run_id)
            run.update(changes)
            run["updated_at"] = now()
            self.save(run)
            return run

    def all(self) -> list[dict]:
        result = []
        with self.lock:
            for path in self.root.glob("run-*/run.json"):
                try:
                    result.append(json.loads(path.read_text()))
                except (OSError, ValueError):
                    continue
        return sorted(result, key=lambda r: r["created_at"], reverse=True)

    def stage(self, run_id: str, stage_id: str, status: str, detail: str) -> None:
        with self.lock:
            run = self.get(run_id)
            status = {"complete": "passed", "error": "failed"}.get(status, status)
            for stage in run["stages"]:
                if stage["id"] == stage_id:
                    stage.update(status=status, detail=detail)
            run["log"] += f"[{now()}] {stage_id}: {status} — {detail}\n"
            self.save(run)


def baseline_blockers(run: dict) -> list[str]:
    blockers = assumption_blockers(run) + contract_blockers(run)
    if "assumptions" not in run:
        blockers.append("This historical run predates the explicit assumption ledger; create a new run for acceptance.")
    if run.get("behavior") and (run.get("behavioral_analysis") or {}).get("status") != "bounded_pass":
        blockers.append("Behavioral obligations have unresolved, violated, vacuous, or unbounded results; inspect the scope of each check.")
    if run["status"] != "completed":
        blockers.append("Run has not completed successfully.")
    if not run.get("model"):
        blockers.append("No generated model is available.")
    if (run.get("analysis") or {}).get("status") != "sat":
        blockers.append("Complete supported-scope satisfiability evidence is unavailable.")
    entries = (run.get("tlr") or {}).get("requirements", [])
    if len(entries) != len(run.get("requirements", [])) or not entries or any(r.get("status") != "supported" for r in entries):
        blockers.append("One or more requirement interpretations are unresolved or await engineering review.")
    if (run.get("compilation") or {}).get("status") != "passed":
        blockers.append("SysML compilation has not passed.")
    if run.get("engine") == "pipeline":
        blockers.append("The existing LLM pipeline's source-to-model semantic alignment has not been established.")
    if run.get("superseded_by"):
        blockers.append("A newer candidate revision exists; review that revision for current acceptance.")
    if run.get("errors"):
        blockers.append("The run has unresolved execution errors.")
    return blockers


def interpretation_presentation(run: dict, directory: Path) -> dict | None:
    """Derive an inspectable view without rewriting a saved run or its evidence."""
    if run.get("engine") != "pipeline":
        return None
    from review_interpretation import build_pipeline_interpretations
    artifact_name = "pipeline_model_sat.smt2"
    artifact = next((item for item in run.get("artifacts", []) if item.get("name") == artifact_name), None)
    fragment = ""
    diagnostics = []
    if artifact:
        path = directory / artifact_name
        try:
            if path.is_symlink() or path.resolve().parent != directory.resolve():
                raise ValueError("Candidate SMT artifact is outside this run directory.")
            content = path.read_bytes()
            if profile.digest(content) != artifact.get("sha256"):
                raise ValueError("Candidate SMT artifact changed after analysis; interpretation evidence is stale.")
            fragment = content.decode("utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            diagnostics.append(str(exc))
    raw = (run.get("tlr") or {}).get("raw")
    rows = build_pipeline_interpretations(run.get("requirements", []), raw, fragment, artifact_name)
    return {
        "schema": "review_interpretation_view/1", "requirements": rows,
        "pending_review_ids": [row["id"] for row in rows if row["status"] == "pending_review"],
        "needs_interpretation_ids": [row["id"] for row in rows if row["status"] == "needs_interpretation"],
        "diagnostics": diagnostics, "source_evidence_hash": run.get("evidence_hash"),
        "scope": "Presentation of recorded candidate encodings and pending interpretation. This view does not alter the saved run, approve meaning, or establish source-to-model fidelity.",
    }


def collect_artifacts(run_dir: Path, run_id: str) -> list[dict]:
    artifacts = []
    for path in sorted(run_dir.iterdir()):
        if not path.is_file() or path.is_symlink() or path.name == "run.json" or path.suffix == ".tmp":
            continue
        artifacts.append({"name": path.name, "label": path.name,
                          "url": f"/api/runs/{run_id}/artifacts/{path.name}",
                          "sha256": profile.digest(path.read_bytes()), "bytes": path.stat().st_size})
    return artifacts



def json_digest(value: object) -> str:
    return profile.digest(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def evidence_integrity(run: dict) -> list[str]:
    """Check producer snapshots against the final, downloadable artifact ledger."""
    artifacts = {a["name"]: a["sha256"] for a in run["artifacts"]}
    issues = []
    if artifacts.get(run["input_file"]) != run["source_hash"]:
        issues.append("Source file differs from the input version used by this run.")
    model, compilation, analysis = run.get("model") or {}, run.get("compilation") or {}, run.get("analysis") or {}
    if model:
        model_name = Path(model.get("path") or "model.sysml").name
        final_hash = artifacts.get(model_name)
        if final_hash != profile.digest(model["text"]):
            issues.append("Model artifact differs from the generated model snapshot.")
        if compilation.get("status") == "passed" and final_hash != compilation.get("model_sha256"):
            issues.append("Compilation does not cover the final model artifact.")
        trace_check = compilation.get("trace_compilation")
        if trace_check and trace_check.get("status") == "passed" and artifacts.get("model_with_trace.sysml") != trace_check.get("model_sha256"):
            issues.append("Trace compilation does not cover the final combined artifact.")
    if run["engine"] == "local" and run.get("tlr"):
        if artifacts.get("interpretation.json") != json_digest(run["tlr"]):
            issues.append("Interpretation artifact differs from the parsed typed-meaning snapshot.")
        if analysis.get("smt_sha256") and artifacts.get("constraints.smt2") != analysis["smt_sha256"]:
            issues.append("Solver evidence does not cover the final SMT artifact.")
        if artifacts.get("analysis.json") and artifacts["analysis.json"] != json_digest(analysis):
            issues.append("Analysis artifact differs from its recorded result.")
    for key, filename in (("assumptions", "assumptions.json"), ("behavior", "behavior.json"),
                          ("behavioral_analysis", "behavioral_analysis.json"), ("contracts", "contracts.json"),
                          ("contract_changes", "contract_changes.json")):
        if run.get(key) is not None and filename in artifacts and artifacts[filename] != json_digest(run[key]):
            issues.append(f"{filename} differs from the recorded review snapshot.")
    bundle = run.get("contracts")
    if bundle:
        from review_contracts import behavior_from_contracts
        try:
            canonical = behavior_from_contracts(bundle)
            if bundle.get("source_hash") != run["source_hash"] or canonical != run.get("behavior"):
                issues.append("Shared contract source or behavior differs from the analyzed run.")
        except (ValueError, KeyError, TypeError) as exc:
            issues.append(f"Shared contract snapshot is invalid: {exc}")
        for row in bundle.get("contracts", []):
            reference = row.get("sysml", {})
            if reference.get("binding") == "exact_text_index" and reference.get("text_sha256") != profile.digest(model.get("text", "")):
                issues.append("Contract model reference is not bound to the compiled model text.")
            for check in row.get("checks", []):
                for link in check.get("artifacts", []):
                    if link.get("sha256") and artifacts.get(link.get("artifact")) != link["sha256"]:
                        issues.append("Contract evidence link differs from the exact artifact ledger.")
    inspection = model.get("inspection")
    if inspection and (artifacts.get("model_inspection.json") != json_digest(inspection)
                       or inspection.get("text_sha256") != profile.digest(model["text"])):
        issues.append("Model inspection is not bound to the generated model text.")
    return issues


def finish_run(store: RunStore, run_id: str, status: str) -> None:
    with store.lock:
        run = store.get(run_id)
        run["status"] = status
        run["completed_at"] = now()
        for stage in run["stages"]:
            if stage["status"] in ("pending", "running"):
                stage.update(status="not_run", detail="No completed evidence was produced for this stage.")
        directory = store.directory(run_id)
        if not (directory / "assumptions.json").exists():
            run["assumptions"] = build_assumptions(run.get("tlr"), run["requirements"], run["engine"], directory,
                                                   run.get("behavior"), run.get("behavioral_analysis"), run.get("behavior_origin", "engineer_supplied"))
            write_json(directory / "assumptions.json", run["assumptions"])
        if not run.get("contracts"):
            from review_contracts import build_contract_bundle
            run["contracts"] = build_contract_bundle(run["requirements"], run["source_hash"], run.get("behavior"),
                                                      tlr=run.get("tlr"), model=run.get("model"), assumptions=run.get("assumptions"))
            write_json(directory / "contracts.json", run["contracts"])
        run.setdefault("contract_reviews", [])
        run.setdefault("contract_review_hash", contract_review_hash([]))
        run["artifacts"] = collect_artifacts(directory, run_id)
        integrity_issues = evidence_integrity(run)
        run["integrity"] = {"status": "failed" if integrity_issues else "passed", "issues": integrity_issues}
        if integrity_issues:
            run["errors"].extend(integrity_issues)
            run["status"] = "failed"
        run["evidence_hash"] = profile.digest(json.dumps({"source": run["source_hash"], "artifacts": run["artifacts"],
                                                          "analysis": run.get("analysis"), "compilation": run.get("compilation")}, sort_keys=True))
        run["baseline"] = {"status": "pending", "blockers": baseline_blockers(run),
                           "scope": "Local prototype model acceptance only; source authorization and organizational sign-off are separate."}
        store.save(run)


def diagnostics_text(items) -> str:
    if isinstance(items, str):
        return items
    return "\n".join(f"{item.get('severity', 'info')}: {item.get('message', '')}" +
                     (f" (line {item['line']})" if item.get("line") else "") for item in items or [])


def run_job(store: RunStore, run_id: str) -> None:
    run = store.update(run_id, status="running")
    directory = store.directory(run_id)
    try:
        write_json(directory / "toolchain.json", {
            "engine": run["engine"], "solver_backend": os.getenv("MBSE_SOLVER", "z3"),
            "source_code_hashes": {name: profile.digest((ROOT / "scripts" / name).read_bytes())
                                   for name in ("review_profile.py", "review_server.py", "review_sysml.py", "review_model_index.py", "review_behavior.py", "review_assumptions.py", "review_llm_entry.py", "review_behavior_proposal.py", "review_pipeline_adapter.py", "review_interpretation.py", "review_contracts.py", "review_contract_sysml.py", "review_contract_changes.py", "contract_rules.json", "requirements_pipeline.py")},
            "scope": "Prototype artifacts; source and engineering assumptions require human review."
        })
        shutil.copyfile(ROOT / "scripts" / "contract_rules.json", directory / "contract_rules.json")
        if run["engine"] == "local":
            store.stage(run_id, "interpretation", "running", "Match the explicit grammar; retain unsupported clauses unchanged.")
            tlr = profile.interpret(run["requirements"])
            contract = profile.existing_contract(tlr, run["source_hash"], directory / run["input_file"])
            write_json(directory / "interpretation.json", tlr)
            write_json(directory / "requirements_tlr.json", contract)
            supported = sum(r["status"] == "supported" for r in tlr["requirements"])
            store.update(run_id, tlr=tlr)
            store.stage(run_id, "interpretation", "passed" if supported == len(run["requirements"]) else "partial",
                        f"{supported}/{len(run['requirements'])} requirements in the supported grammar. Meaning awaits engineer review.")
            store.stage(run_id, "analysis", "running", "Invoke the existing solver runner on exact shared typed bounds.")
            analysis = profile.analyze(tlr, directory)
            store.update(run_id, analysis=analysis)
            write_json(directory / "analysis.json", analysis)
            analysis_status = {"sat": "passed", "unsat": "failed", "partial": "partial", "unknown": "partial", "not_run": "not_run"}
            store.stage(run_id, "analysis", analysis_status[analysis["status"]], analysis["summary"])
            store.stage(run_id, "generation", "running", "Generate requirement usages, typed quantities, and proposed satisfaction links from the same interpretation.")
            model = generate_sysml(run["name"], run["requirements"], tlr)
            model_path = directory / "model.sysml"
            model_path.write_text(model["text"], encoding="utf-8")
            model["path"] = str(model_path)
            model["summary_details"] = model["summary"]
            model["summary"] = f"{len(run['requirements'])} source requirements retained; {supported} quantitative clauses bound to typed model attributes. Constraint model for review; executable controller behavior is outside this profile."
            store.update(run_id, model=model)
            store.stage(run_id, "generation", "passed", "Generated the model; unsupported requirements remain visible as documentation-only requirements.")
        else:
            from review_pipeline_adapter import run_existing_pipeline
            result = run_existing_pipeline(directory, run["name"], run["requirements"],
                                           lambda stage, status, detail: store.stage(run_id, stage, status, detail),
                                           propose_behavior=run.get("behavior") is None)
            store.update(run_id, tlr=result.get("tlr"), analysis=result.get("analysis") or {}, model=result.get("model"),
                         errors=result.get("errors", []), behavior_proposal=result.get("behavior_proposal"))
            proposal = result.get("behavior_proposal") or {}
            if not run.get("behavior") and proposal.get("status") == "proposed":
                from review_behavior import validate_behavior
                candidate = validate_behavior(proposal["candidate"], [r["id"] for r in run["requirements"]])
                write_json(directory / "behavior.json", candidate)
                store.update(run_id, behavior=candidate, behavior_origin="llm_proposed")
            current = store.get(run_id)
            store.update(run_id, log=current["log"] + "\n" + result.get("log", ""))
            model = result.get("model")
            if not model:
                finish_run(store, run_id, "failed")
                return
            model_path = Path(model["path"])

        current = store.get(run_id)
        behavior = current.get("behavior")
        from review_contracts import build_contract_bundle, bind_contract_evidence
        store.stage(run_id, "contracts", "running", "Connect source passages, deterministic pattern rules, independent behavior contracts and model elements.")
        assumptions = build_assumptions(current.get("tlr"), run["requirements"], run["engine"], directory,
                                        behavior, None, current.get("behavior_origin", "engineer_supplied"))
        bundle = build_contract_bundle(run["requirements"], run["source_hash"], behavior, tlr=current.get("tlr"),
                                       model=model, assumptions=assumptions)
        if behavior:
            from review_behavior import analyze_behavior
            store.stage(run_id, "behavior", "running", "Search admissible candidate executions for violations; check feasibility and trigger reachability separately.")
            behavioral = analyze_behavior(bundle, directory)
            store.update(run_id, behavioral_analysis=behavioral)
            write_json(directory / "behavioral_analysis.json", behavioral)
            stage_status = "passed" if behavioral.get("status") == "bounded_pass" else "failed" if behavioral.get("status") in ("counterexample", "infeasible") else "partial"
            store.stage(run_id, "behavior", stage_status, behavioral.get("summary") or "Inspect each property's verdict, horizon, and assumptions.")
        else:
            behavioral = {"status": "not_run", "checks": [], "limitations": ["No separate candidate transition model was supplied; scalar consistency does not establish liveness or robustness."]}
            store.update(run_id, behavioral_analysis=behavioral)
            write_json(directory / "behavioral_analysis.json", behavioral)
            store.stage(run_id, "behavior", "not_run", behavioral["limitations"][0])
        assumptions = build_assumptions(current.get("tlr"), run["requirements"], run["engine"], directory, behavior, behavioral, current.get("behavior_origin", "engineer_supplied"))
        # The domain candidate is preserved; the same contract representation adds a
        # finite projection, without claiming that domain parts implement its behavior.
        from requirements_pipeline import attach_review_contracts
        original_text = model["text"]
        (directory / "domain_model_before_contracts.sysml").write_text(original_text, encoding="utf-8")
        projection = attach_review_contracts(original_text, bundle)
        model["text"] = projection["text"]
        for row in bundle["contracts"]:
            if row["id"] in projection.get("contract_map", {}):
                row["sysml"] = {**row.get("sysml", {}), **projection["contract_map"][row["id"]]}
        model["contract_projection"] = {k: v for k, v in projection.items() if k != "text"}
        model["elements"] = model.get("elements", []) + projection.get("elements", [])
        model_path.write_text(model["text"], encoding="utf-8")
        from review_model_index import build_model_inspection
        model["inspection"] = build_model_inspection(model, run["requirements"], current.get("tlr") or {}, assumptions, behavioral)
        # The binder receives exact files; the original solver result snapshot stays immutable.
        binding_analysis = deepcopy(behavioral)
        binding_analysis["behavior_sha256"] = (behavioral.get("contract_input") or {}).get("behavior_sha256")
        binding_analysis["context_sha256"] = (behavioral.get("contract_input") or {}).get("context_sha256")
        binding_analysis["artifact_hashes"] = {path.name: profile.digest(path.read_bytes()) for path in directory.iterdir() if path.is_file() and not path.is_symlink()}
        binding_analysis["scalar_analysis"] = current.get("analysis") or {}
        bundle = bind_contract_evidence(bundle, assumptions, binding_analysis, model["inspection"])
        for assumption in assumptions:
            assumption["contract_ids"] = [c["id"] for c in bundle["contracts"] if assumption["id"] in c.get("assumption_ids", [])]
        write_json(directory / "assumptions.json", assumptions)
        write_json(directory / "model_inspection.json", model["inspection"])
        write_json(directory / "contracts.json", bundle)
        from review_contract_changes import compare_contract_bundles
        parent_bundle = None
        if run.get("parent_run_id"):
            parent = store.get(run["parent_run_id"])
            for artifact in parent.get("artifacts", []):
                path = store.directory(parent["id"]) / artifact["name"]
                if path.is_symlink() or path.resolve().parent != store.directory(parent["id"]).resolve() or not path.is_file() or profile.digest(path.read_bytes()) != artifact["sha256"]:
                    raise ValueError("Parent artifacts changed after review; semantic comparison cannot use stale evidence.")
            parent_bundle = parent.get("contracts")
        changes = compare_contract_bundles(parent_bundle, bundle, directory, run.get("parent_run_id"))
        write_json(directory / "contract_changes.json", changes)
        store.update(run_id, assumptions=assumptions, model=model, contracts=bundle, contract_changes=changes)
        unresolved = sum(c.get("status") == "needs_interpretation" for c in bundle["contracts"])
        store.stage(run_id, "contracts", "partial" if unresolved else "passed",
                    f"{len(bundle['contracts'])} shared contract records; {unresolved} require formal interpretation. Source fidelity and semantic changes await engineer review.")

        store.stage(run_id, "compilation", "running", "Parse and validate the generated SysML v2 model with the installed pilot compiler.")
        model_before = profile.digest(model_path.read_bytes())
        compilation = compile_sysml(model_path, timeout=90)
        compilation["model_sha256"] = model_before
        compilation["diagnostic_items"] = compilation.get("diagnostics", [])
        compilation["diagnostics"] = diagnostics_text(compilation["diagnostic_items"])
        if profile.digest(model_path.read_bytes()) != model_before:
            compilation.update(status="failed", diagnostics="Model changed during compilation; the evidence is invalid.")
        trace = model.get("trace_path")
        if trace and Path(trace).is_file() and compilation["status"] == "passed":
            combined = directory / "model_with_trace.sysml"
            combined.write_text(model_path.read_text() + "\n" + Path(trace).read_text(), encoding="utf-8")
            trace_result = compile_sysml(combined, timeout=90)
            compilation["trace_compilation"] = trace_result
            if trace_result["status"] != "passed":
                compilation["status"] = trace_result["status"]
                compilation["diagnostics"] += "\nCombined domain/trace validation: " + diagnostics_text(trace_result.get("diagnostics", []))
        write_json(directory / "compilation.json", compilation)
        store.update(run_id, compilation=compilation)
        detail = "SysML syntax, references, and well-formedness passed. This is separate from requirement feasibility and intent." if compilation["status"] == "passed" else compilation["diagnostics"] or "Compiler did not establish a successful result."
        store.stage(run_id, "compilation", compilation["status"], detail)
        run = store.get(run_id)
        finish_run(store, run_id, "failed" if compilation["status"] == "failed" or run["errors"] else "completed")
    except Exception as exc:
        current = store.get(run_id)
        errors = current.get("errors", []) + [f"{type(exc).__name__}: {exc}"]
        store.update(run_id, errors=errors)
        for stage in current["stages"]:
            if stage["status"] == "running":
                store.stage(run_id, stage["id"], "failed", errors[-1])
        finish_run(store, run_id, "failed")


def create_app(data_dir: Path | None = None) -> FastAPI:
    store = RunStore(data_dir or Path(os.getenv("MBSE_REVIEW_DATA_DIR", str(ROOT / "out" / "review_workbench"))))
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="requirements-review")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        for run in store.all():
            if run["status"] in ("queued", "running"):
                store.update(run["id"], errors=run.get("errors", []) + ["The service stopped before this run completed. Create a new run to retry."])
                finish_run(store, run["id"], "failed")
        yield
        executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="MBSE model review prototype", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.executor = executor
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
        return FileResponse(ROOT / "prototypes/review-workbench/index.html", media_type="text/html")

    @app.get("/api/config")
    def config():
        codex = bool(shutil.which("codex"))
        solver_path = os.getenv("Z3_PATH", "z3")
        sample_path = ROOT / "examples/synthetic/behavior_response.json"
        behavior_sample = json.loads(sample_path.read_text()) if sample_path.is_file() else None
        return {"engines": [
            {"id": "local", "label": "Local constraint profile", "available": True,
             "description": "Exact scalar/timing grammar, existing solver runner, and actual SysML compilation. Unrecognized clauses remain visible and unverified."},
            {"id": "pipeline", "label": "Existing Codex pipeline", "available": codex,
             "description": "Uses the configured Codex account to generate SysML and propose a typed behavior model when none is supplied. Captures assumptions and generator responses. Sends requirements to that provider; proposals require engineering review."}],
            "capabilities": {"solver": {"available": bool(shutil.which(solver_path)), "detail": "Existing solver seam (default Z3); exact verdict and diagnostics recorded per run."},
                             "compiler": compiler_capability()},
            "sample": profile.SAMPLE, "behavior_sample": behavior_sample,
            "contract_sample": json.loads((ROOT / "examples/synthetic/contracts_voltage.json").read_text()), "limits": {"max_requirements": profile.MAX_REQUIREMENTS, "max_bytes": profile.MAX_INPUT_BYTES},
            "acceptance_scope": "Local prototype model and interpretation only; no authenticated organizational approval or source amendment."}

    @app.get("/api/runs")
    def list_runs():
        return {"runs": store.all()}

    @app.post("/api/runs", status_code=202)
    def submit(payload: RunInput):
        try:
            requirements = profile.parse_requirements(payload.text, payload.format, payload.name.strip())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        behavior = None
        if payload.behavior is not None:
            from review_behavior import validate_behavior
            try:
                behavior = validate_behavior(payload.behavior, [r["id"] for r in requirements])
            except (ValueError, TypeError) as exc:
                raise HTTPException(422, str(exc)) from exc
        if not payload.name.strip():
            raise HTTPException(422, "Give the model a name.")
        if payload.engine == "pipeline" and not shutil.which("codex"):
            raise HTTPException(422, "The Codex CLI is not installed. Select the local profile or install the existing pipeline prerequisites.")
        with store.lock:
            if sum(r["status"] in ("queued", "running") for r in store.all()) >= 6:
                raise HTTPException(429, "Six runs are already active or queued; wait for one to finish.")
            parent = store.get(payload.parent_run_id) if payload.parent_run_id else None
            if parent and parent["status"] in ("queued", "running"):
                raise HTTPException(409, "Wait for the parent run to finish before revising its requirements.")
            if parent and not (payload.revision_rationale or "").strip():
                raise HTTPException(422, "Explain the proposed source or interpretation change.")
            run_id = "run-" + uuid.uuid4().hex[:16]
            run_dir = store.directory(run_id)
            run_dir.mkdir()
            input_file = "source." + {"text": "txt", "csv": "csv", "json": "json"}[payload.format]
            (run_dir / input_file).write_text(payload.text, encoding="utf-8")
            write_json(run_dir / "requirements.json", requirements)
            if behavior is not None:
                write_json(run_dir / "behavior.json", behavior)
            run = {"id": run_id, "name": payload.name.strip(), "status": "queued", "engine": payload.engine,
                   "created_at": now(), "updated_at": now(), "parent_run_id": payload.parent_run_id,
                   "revision_rationale": payload.revision_rationale, "source_hash": profile.digest(payload.text),
                   "evidence_hash": None, "input_file": input_file, "format": payload.format,
                   "behavior": behavior, "behavior_origin": "engineer_supplied" if behavior is not None else None,
                   "behavior_proposal": None, "behavioral_analysis": None,
                   "contracts": None, "contract_changes": None, "contract_reviews": [], "contract_review_hash": contract_review_hash([]),
                   "assumptions": [], "assumption_reviews": [], "assumption_review_hash": assumption_review_hash([]),
                   "requirements": requirements, "tlr": None, "analysis": {}, "model": None, "compilation": {},
                   "stages": [{"id": id, "label": label, "status": "pending", "detail": ""} for id, label in STAGES],
                   "artifacts": [], "reviews": [], "errors": [], "log": "", "superseded_by": [],
                   "baseline": {"status": "pending", "blockers": ["Run has not completed."]}}
            run["stages"][0].update(status="passed", detail=f"Preserved {len(requirements)} requirements, identifiers, and source references.")
            store.save(run)
            if parent:
                parent["superseded_by"].append(run_id)
                parent["baseline"].update(status="superseded", blockers=baseline_blockers(parent))
                store.save(parent)
            executor.submit(run_job, store, run_id)
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
        if not payload.reviewer.strip() or not payload.rationale.strip():
            raise HTTPException(422, "A reviewer and engineering rationale are required.")
        with store.lock:
            run = store.get(run_id)
            if run["status"] not in ("completed", "failed"):
                raise HTTPException(409, "Wait for the run to finish before reviewing its assumptions.")
            if payload.source_hash != run["source_hash"] or payload.evidence_hash != run["evidence_hash"]:
                raise HTTPException(409, "The reviewed source or evidence is stale. Reload the run.")
            if payload.assumption_review_hash != run.get("assumption_review_hash"):
                raise HTTPException(409, "Assumption decisions changed. Reload the current review state.")
            assumption = next((a for a in run.get("assumptions", []) if a["id"] == assumption_id), None)
            if assumption is None:
                raise HTTPException(404, "Assumption not found in this run.")
            for artifact in run["artifacts"]:
                path = store.directory(run_id) / artifact["name"]
                if path.is_symlink() or not path.is_file() or profile.digest(path.read_bytes()) != artifact["sha256"]:
                    raise HTTPException(409, "An artifact changed after analysis. Create a new run before reviewing it.")
            record = {"id": "assumption-review-" + uuid.uuid4().hex[:12], "created_at": now(),
                      "run_id": run_id, "assumption_id": assumption_id, **payload.model_dump(),
                      "reviewer": payload.reviewer.strip(), "rationale": payload.rationale.strip(),
                      "assumption_snapshot": deepcopy(assumption),
                      "effect": "Review decision only; the recorded assumption and solver constraints remain unchanged."}
            run.setdefault("assumption_reviews", []).append(record)
            run["assumption_review_hash"] = assumption_review_hash(run["assumption_reviews"])
            run["baseline"] = {"status": "superseded" if run["superseded_by"] else "pending",
                               "blockers": baseline_blockers(run),
                               "scope": "Assumption decisions changed; final prototype acceptance must be recorded again."}
            store.save(run)
            return record

    @app.post("/api/runs/{run_id}/contracts/{contract_id}/reviews", status_code=201)
    def review_contract(run_id: str, contract_id: str, payload: ContractReviewInput):
        if not payload.reviewer.strip() or not payload.rationale.strip():
            raise HTTPException(422, "A reviewer and engineering rationale are required.")
        with store.lock:
            run = store.get(run_id)
            if run["status"] not in ("completed", "failed"):
                raise HTTPException(409, "Wait for the run to finish before reviewing contracts.")
            if payload.source_hash != run["source_hash"] or payload.evidence_hash != run["evidence_hash"]:
                raise HTTPException(409, "The reviewed source or evidence is stale. Reload the run.")
            if payload.contract_review_hash != run.get("contract_review_hash"):
                raise HTTPException(409, "Contract decisions changed. Reload the current review state.")
            contract = next((c for c in (run.get("contracts") or {}).get("contracts", []) if c["id"] == contract_id), None)
            if contract is None:
                raise HTTPException(404, "Contract not found in this run.")
            for artifact in run["artifacts"]:
                path = store.directory(run_id) / artifact["name"]
                if path.is_symlink() or not path.is_file() or path.resolve().parent != store.directory(run_id).resolve() or profile.digest(path.read_bytes()) != artifact["sha256"]:
                    raise HTTPException(409, "An artifact changed after analysis. Create a new run before reviewing it.")
            record = {"id": "contract-review-" + uuid.uuid4().hex[:12], "created_at": now(), "run_id": run_id,
                      "contract_id": contract_id, **payload.model_dump(), "reviewer": payload.reviewer.strip(),
                      "rationale": payload.rationale.strip(), "contract_snapshot": deepcopy(contract),
                      "change_snapshot": [deepcopy(c) for c in (run.get("contract_changes") or {}).get("changes", []) if c["contract_id"] == contract_id],
                      "authority": "Self-declared local reviewer; no organizational identity verification.",
                      "effect": "Review of this candidate interpretation and recorded change only; no source amendment, solver result, or artifact is altered."}
            run.setdefault("contract_reviews", []).append(record)
            run["contract_review_hash"] = contract_review_hash(run["contract_reviews"])
            run["baseline"] = {"status": "superseded" if run["superseded_by"] else "pending", "blockers": baseline_blockers(run),
                               "scope": "Contract decisions changed; final prototype acceptance must be recorded again."}
            store.save(run)
            return record

    @app.post("/api/runs/{run_id}/reviews", status_code=201)
    def review(run_id: str, payload: ReviewInput):
        if not payload.reviewer.strip() or not payload.rationale.strip() or not payload.scope.strip():
            raise HTTPException(422, "Reviewer, rationale, and review scope must be nonempty.")
        with store.lock:
            run = store.get(run_id)
            if run["status"] not in ("completed", "failed"):
                raise HTTPException(409, "Wait for analysis to finish before recording a review.")
            if payload.source_hash != run["source_hash"] or payload.evidence_hash != run["evidence_hash"]:
                raise HTTPException(409, "The reviewed source or evidence revision is stale. Reload the run.")
            for artifact in run["artifacts"]:
                path = store.directory(run_id) / artifact["name"]
                if path.is_symlink() or not path.is_file() or profile.digest(path.read_bytes()) != artifact["sha256"]:
                    raise HTTPException(409, "An artifact changed after analysis. Create a new run before reviewing it.")
            if run.get("assumptions") and payload.assumption_review_hash != run.get("assumption_review_hash"):
                raise HTTPException(409, "Assumption decisions changed or were not acknowledged. Reload their current review state.")
            if run.get("contracts") and payload.contract_review_hash != run.get("contract_review_hash"):
                raise HTTPException(409, "Contract decisions changed or were not acknowledged. Reload their current review state.")
            if payload.decision == "approve" and run.get("parent_run_id") and not payload.acknowledge_contract_changes:
                raise HTTPException(409, "Explicitly acknowledge the proposed semantic changes, including removed contracts and changed assumptions.")
            blockers = baseline_blockers(run)
            if payload.decision == "approve" and (blockers or not payload.acknowledge):
                raise HTTPException(409, {"message": "This prototype model is not ready for acceptance.",
                                          "blockers": blockers + ([] if payload.acknowledge else ["Reviewer must acknowledge the interpretation, assumptions, and scope."])})
            record = {"id": "review-" + uuid.uuid4().hex[:12], "created_at": now(), "run_id": run_id,
                      **payload.model_dump(), "reviewer": payload.reviewer.strip(), "rationale": payload.rationale.strip(),
                      "artifact_hashes": {a["name"]: a["sha256"] for a in run["artifacts"]},
                      "analysis_snapshot": deepcopy(run["analysis"]), "compilation_snapshot": deepcopy(run["compilation"]),
                      "assumptions": deepcopy(run.get("assumptions", [])),
                      "assumption_decisions": deepcopy(run.get("assumption_reviews", [])),
                      "behavioral_analysis_snapshot": deepcopy(run.get("behavioral_analysis")),
                      "contracts_snapshot": deepcopy(run.get("contracts")), "contract_decisions": deepcopy(run.get("contract_reviews", [])),
                      "contract_changes_snapshot": deepcopy(run.get("contract_changes")),
                      "authority": "Self-declared local reviewer; no organizational identity verification.",
                      "source_change_authorized": False, "acceptance_kind": "prototype model and interpreted scope only"}
            run["reviews"].append(record)
            run["baseline"] = {"status": "approved" if payload.decision == "approve" else "superseded" if run["superseded_by"] else "pending",
                               "blockers": blockers, "scope": "Local prototype model acceptance; no authoritative source amendment."}
            store.save(run)
            return record

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
