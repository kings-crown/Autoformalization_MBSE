"""Shared requirements-to-SysML workflow, independent of HTTP transport.

RunInput/create_run validate and persist requests; run_job executes the same
workflow synchronously for either the API worker or the command-line client.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field

import review_profile as profile
from review_assumptions import build_assumptions, assumption_blockers, assumption_review_hash
from review_sysml import compiler_capability, compile_sysml, generate_sysml
from review_configuration import execution_configuration, COMPILER_TIMEOUT


class WorkflowError(Exception):
    """Transport-neutral validation/state error translated by each front end."""
    def __init__(self, status_code: int, detail: object):
        self.status_code = status_code
        self.detail = detail
        super().__init__(str(detail))


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


class DesignReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewer: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=6000)
    acknowledge: bool = False
    parent_source_hash: str | None = None
    parent_evidence_hash: str | None = None


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="Requirements model", min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=profile.MAX_INPUT_BYTES)
    format: Literal["text", "csv", "json"] = "text"
    engine: Literal["local", "pipeline"] = "local"
    analysis_mode: Literal["requirements", "propose_design", "check_design"] = "requirements"
    behavior: dict | None = None
    candidate_provenance: dict = Field(default_factory=dict)
    design_review: DesignReviewInput | None = None
    parent_run_id: str | None = None
    revision_rationale: str | None = Field(default=None, max_length=4000)


def validate_analysis_selection(mode: str, engine: str, behavior: dict | None, review: dict | None) -> None:
    """Keep requirement formalization separate from explicitly reviewed design checks."""
    if mode not in {"requirements", "propose_design", "check_design"}:
        raise ValueError("Select a supported analysis mode.")
    if mode != "check_design":
        if behavior is not None or review is not None:
            raise ValueError("Behavior input and a design review require Check reviewed design mode. Requirements mode never checks a separate design.")
        if mode == "propose_design" and engine != "pipeline":
            raise ValueError("Propose design for review requires the Existing Codex pipeline engine.")
        return
    if behavior is None:
        raise ValueError("Supply the candidate behavior JSON to check a reviewed design.")
    if not review or review.get("acknowledge") is not True:
        raise ValueError("Explicitly acknowledge review of the candidate design, its source interpretation, and its assumptions before checking it.")
    if not str(review.get("reviewer", "")).strip() or not str(review.get("rationale", "")).strip():
        raise ValueError("A reviewer and engineering rationale are required before checking a design.")


def design_review_findings(behavior: dict) -> list[str]:
    findings = []
    if not behavior.get("initial") and not behavior.get("transitions"):
        findings.append("No initial-state or transition rules are declared. Inspect whether the assumptions alone describe a meaningful candidate design.")
    names = [v["name"] for v in behavior.get("variables", [])
             if v.get("type") in {"Int", "Real"} and v.get("role") != "parameter" and not v.get("bounds")]
    if names:
        findings.append("No explicit numeric bounds are declared for: " + ", ".join(names) + ". Inspect whether independent design predicates constrain these values; requirement limits remain separate checks.")
    return findings


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
    architecture_binding_review_hash: str | None = None
    correction_summary_sha256: str | None = None


class CandidateInspectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    behavior: dict
    requirements: list[dict] | None = None
    text: str | None = Field(default=None, max_length=profile.MAX_INPUT_BYTES)
    format: Literal["text", "csv", "json"] = "text"
    name: str = Field(default="Candidate review", max_length=160)
    provenance: dict = Field(default_factory=dict)
    origin: str = Field(default="engineer_supplied", max_length=100)
    edits: list[dict] = Field(default_factory=list, max_length=200)


class BindingReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_hash: str
    evidence_hash: str
    model_sha256: str
    architecture_binding_review_hash: str
    target_kind: Literal["variable", "contract"]
    target_id: str = Field(min_length=1, max_length=200)
    element_id: str | None = None
    value_element_id: str | None = None
    decision: Literal["accept", "reject", "defer"]
    reviewer: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=6000)
    acknowledge_unresolved_compatibility: bool = False


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


class _ProcessLock:
    """Reentrant thread lock with an advisory process lock at its outer edge.

    The lock file belongs to the store, so independently started API and CLI
    processes serialize read-modify-write transactions for the same directory.
    """
    def __init__(self, path: Path):
        self.path = path
        self.thread = threading.RLock()
        self.local = threading.local()

    def __enter__(self):
        self.thread.acquire()
        depth = getattr(self.local, "depth", 0)
        if depth == 0:
            descriptor = None
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self.local.descriptor = descriptor
            except BaseException:
                if descriptor is not None:
                    os.close(descriptor)
                self.thread.release()
                raise
        self.local.depth = depth + 1
        return self

    def __exit__(self, *_error):
        depth = self.local.depth - 1
        self.local.depth = depth
        try:
            if depth == 0:
                descriptor = self.local.descriptor
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
                    del self.local.descriptor
        finally:
            self.thread.release()


def process_identity(pid: int | None = None) -> dict | None:
    """Linux process identity includes birth ticks to reject a reused PID."""
    pid = os.getpid() if pid is None else pid
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] in {"Z", "X"}:
            return None
        return {"pid": pid, "start_ticks": fields[19]}
    except (OSError, IndexError):
        return None


def run_owner_active(run: dict) -> bool:
    owner = run.get("execution_owner")
    return bool(isinstance(owner, dict) and owner.get("start_ticks")
                and process_identity(owner.get("pid")) == owner)


class RunStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = _ProcessLock(self.root / ".workflow.lock")

    def directory(self, run_id: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise WorkflowError(404, "Run not found.")
        return self.root / run_id

    def get(self, run_id: str) -> dict:
        with self.lock:
            try:
                return json.loads((self.directory(run_id) / "run.json").read_text())
            except FileNotFoundError:
                raise WorkflowError(404, "Run not found.") from None

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
    if run.get("review_workflow_version", 0) >= 2 and run.get("behavior"):
        from review_bindings import binding_blockers
        blockers += binding_blockers(run)
    if run.get("analysis_mode") == "propose_design":
        blockers.append("This run only proposes a design. Review the candidate and start a separate Check reviewed design run before drawing design-compliance conclusions.")
    if run.get("analysis_mode") == "check_design" and not (run.get("design_review") or {}).get("acknowledge"):
        blockers.append("The candidate design has no explicit review acknowledgment.")
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
                          ("contract_changes", "contract_changes.json"),
                          ("design_review", "design_review.json"), ("behavior_proposal", "design_proposal.json"),
                          ("candidate_inspection", "candidate_inspection.json"),
                          ("candidate_provenance", "candidate_provenance.json"),
                          ("correction_summary", "correction_summary.json")):
        if run.get(key) is not None and filename in artifacts and artifacts[filename] != json_digest(run[key]):
            issues.append(f"{filename} differs from the recorded review snapshot.")
    for key, filename in (("execution_config", "execution_config.json"), ("formalization_quality", "formalization_quality.json")):
        if (run.get(key) is not None or filename in artifacts) and artifacts.get(filename) != json_digest(run.get(key)):
            issues.append(f"{filename} is missing or differs from the recorded snapshot.")
    mode = run.get("analysis_mode")
    if mode is not None:
        selection = {"analysis_mode": mode, "engine": run["engine"]}
        if artifacts.get("analysis_selection.json") != json_digest(selection):
            issues.append("Analysis mode differs from the recorded selection.")
        if mode != "check_design" and run.get("behavior") is not None:
            issues.append("A separate behavior model was installed without explicit design-check selection.")
        if mode == "check_design":
            from review_contracts import _hash
            review = run.get("design_review") or {}
            if (review.get("acknowledge") is not True or review.get("source_hash") != run["source_hash"]
                    or review.get("behavior_sha256") != _hash(run.get("behavior"))
                    or artifacts.get("design_review.json") != json_digest(review)):
                issues.append("Design review is missing or does not cover the exact checked source and candidate.")
    if run.get("review_workflow_version", 0) >= 2 and run.get("behavior"):
        from review_contracts import _hash
        inspection = run.get("candidate_inspection") or {}
        if (inspection.get("behavior_sha256") != _hash(run["behavior"])
                or inspection.get("provenance") != run.get("candidate_provenance")
                or (run.get("design_review") or {}).get("candidate_provenance_sha256") != _hash(run.get("candidate_provenance"))):
            issues.append("Candidate inspection/provenance differs from the reviewed behavior snapshot.")
    correction = run.get("correction_summary")
    if correction:
        from review_contracts import _hash
        if correction.get("sha256") != _hash({k: v for k, v in correction.items() if k != "sha256"}):
            issues.append("Correction summary differs from its recorded snapshot hash.")
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
        from review_quality import assess_formalization
        run["formalization_quality"] = assess_formalization(run)
        write_json(directory / "formalization_quality.json", run["formalization_quality"])
        run["artifacts"] = collect_artifacts(directory, run_id)
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
    run = store.update(run_id, status="running", execution_owner=process_identity())
    directory = store.directory(run_id)
    try:
        mode = run.get("analysis_mode", "requirements")
        validate_analysis_selection(mode, run["engine"], run.get("behavior"), run.get("design_review"))
        if mode == "check_design":
            from review_contracts import _hash
            review = run["design_review"]
            if review.get("source_hash") != run["source_hash"] or review.get("behavior_sha256") != _hash(run["behavior"]):
                raise ValueError("The recorded design review does not cover the submitted candidate and source.")
        configuration = execution_configuration(run["engine"], mode)
        write_json(directory / "execution_config.json", configuration)
        store.update(run_id, execution_config=configuration)
        write_json(directory / "toolchain.json", {
            "engine": run["engine"], "solver_backend": configuration["solver"]["backend"],
            "source_code_hashes": {name: profile.digest((ROOT / "scripts" / name).read_bytes())
                                   for name in ("review_profile.py", "review_workflow.py", "review_configuration.py", "review_quality.py", "review_cli.py", "review_server.py", "review_sysml.py", "review_model_index.py", "review_behavior.py", "review_assumptions.py", "review_llm_entry.py", "review_behavior_proposal.py", "review_pipeline_adapter.py", "review_interpretation.py", "review_contracts.py", "review_contract_sysml.py", "review_contract_changes.py", "review_candidate.py", "review_bindings.py", "review_corrections.py", "contract_rules.json", "requirements_pipeline.py")},
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
                                           propose_behavior=mode == "propose_design")
            # A proposal is inspectable data, never an implicitly installed design.
            proposal = deepcopy(result.get("behavior_proposal")) if mode == "propose_design" else None
            if proposal:
                if proposal.get("status") == "proposed":
                    from review_behavior import validate_behavior
                    from review_contracts import _hash
                    candidate = validate_behavior(proposal["candidate"], [r["id"] for r in run["requirements"]])
                    proposal.update(candidate=candidate, review_status="pending", source_hash=run["source_hash"],
                                    proposal_sha256=_hash(candidate), review_findings=design_review_findings(candidate))
                    from review_candidate import inspect_candidate
                    inspection = inspect_candidate(candidate, run["requirements"], origin="llm_proposed")
                    write_json(directory / "candidate_inspection.json", inspection)
                    write_json(directory / "candidate_provenance.json", inspection["provenance"])
                    store.update(run_id, candidate_inspection=inspection, candidate_provenance=inspection["provenance"])
                write_json(directory / "design_proposal.json", proposal)
            elif mode != "propose_design" and result.get("behavior_proposal"):
                result["log"] = result.get("log", "") + "\nAn unrequested behavior proposal was ignored; the selected mode does not permit its use."
            store.update(run_id, tlr=result.get("tlr"), analysis=result.get("analysis") or {}, model=result.get("model"),
                         errors=result.get("errors", []), behavior_proposal=proposal)
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
        if mode == "check_design":
            from review_behavior import analyze_behavior
            store.stage(run_id, "behavior", "running", "Search admissible candidate executions for violations; check feasibility and trigger reachability separately.")
            behavioral = analyze_behavior(bundle, directory)
            store.update(run_id, behavioral_analysis=behavioral)
            write_json(directory / "behavioral_analysis.json", behavioral)
            stage_status = "passed" if behavioral.get("status") == "bounded_pass" else "failed" if behavioral.get("status") in ("counterexample", "infeasible") else "partial"
            store.stage(run_id, "behavior", stage_status, behavioral.get("summary") or "Inspect each property's verdict, horizon, and assumptions.")
        else:
            proposal = current.get("behavior_proposal") or {}
            pending = mode == "propose_design" and proposal.get("status") == "proposed"
            detail = ("A design was proposed but has not been checked. Inspect its variables, dynamics, assumptions, and properties, then explicitly start a reviewed design check."
                      if pending else "Design proposal was unavailable; no design behavior was checked. " + str(proposal.get("summary", "Inspect generator diagnostics."))
                      if mode == "propose_design" else "Requirements mode: constraints are checked for consistency. No separate design was proposed or checked.")
            behavioral = {"status": "pending_review" if pending else "not_run", "analysis_mode": mode,
                          "checks": [], "summary": detail, "limitations": [detail]}
            store.update(run_id, behavioral_analysis=behavioral)
            write_json(directory / "behavioral_analysis.json", behavioral)
            store.stage(run_id, "behavior", "pending_review" if pending else "not_run", detail)
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
        parent = None
        if run.get("parent_run_id"):
            parent = store.get(run["parent_run_id"])
            for artifact in parent.get("artifacts", []):
                path = store.directory(parent["id"]) / artifact["name"]
                if path.is_symlink() or path.resolve().parent != store.directory(parent["id"]).resolve() or not path.is_file() or profile.digest(path.read_bytes()) != artifact["sha256"]:
                    raise ValueError("Parent artifacts changed after review; semantic comparison cannot use stale evidence.")
            parent_bundle = parent.get("contracts")
        changes = compare_contract_bundles(parent_bundle, bundle, directory, run.get("parent_run_id"))
        write_json(directory / "contract_changes.json", changes)
        from review_corrections import build_correction_summary
        snapshot = {**store.get(run_id), "contracts": bundle}
        corrections = build_correction_summary(parent, snapshot, changes)
        write_json(directory / "correction_summary.json", corrections)
        store.update(run_id, assumptions=assumptions, model=model, contracts=bundle, contract_changes=changes,
                     correction_summary=corrections)
        unresolved = sum(c.get("status") == "needs_interpretation" for c in bundle["contracts"])
        store.stage(run_id, "contracts", "partial" if unresolved else "passed",
                    f"{len(bundle['contracts'])} shared contract records; {unresolved} require formal interpretation. Source fidelity and semantic changes await engineer review.")

        store.stage(run_id, "compilation", "running", "Parse and validate the generated SysML v2 model with the installed pilot compiler.")
        model_before = profile.digest(model_path.read_bytes())
        compilation = compile_sysml(model_path, timeout=COMPILER_TIMEOUT)
        compilation["model_sha256"] = model_before
        compilation["diagnostic_items"] = compilation.get("diagnostics", [])
        compilation["diagnostics"] = diagnostics_text(compilation["diagnostic_items"])
        if profile.digest(model_path.read_bytes()) != model_before:
            compilation.update(status="failed", diagnostics="Model changed during compilation; the evidence is invalid.")
        trace = model.get("trace_path")
        if trace and Path(trace).is_file() and compilation["status"] == "passed":
            combined = directory / "model_with_trace.sysml"
            combined.write_text(model_path.read_text() + "\n" + Path(trace).read_text(), encoding="utf-8")
            trace_result = compile_sysml(combined, timeout=COMPILER_TIMEOUT)
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


def create_run(store: RunStore, payload: RunInput) -> dict:
    """Validate and persist a queued request without starting any worker."""
    design_review = payload.design_review.model_dump() if payload.design_review else None
    try:
        validate_analysis_selection(payload.analysis_mode, payload.engine, payload.behavior, design_review)
        requirements = profile.parse_requirements(payload.text, payload.format, payload.name.strip())
    except ValueError as exc:
        raise WorkflowError(422, str(exc)) from exc
    if payload.candidate_provenance and payload.analysis_mode != "check_design":
        raise WorkflowError(422, "Candidate provenance requires an explicitly reviewed design.")
    behavior = None
    if payload.behavior is not None:
        from review_behavior import validate_behavior
        try:
            behavior = validate_behavior(payload.behavior, [r["id"] for r in requirements])
        except (ValueError, TypeError) as exc:
            raise WorkflowError(422, str(exc)) from exc
    if not payload.name.strip():
        raise WorkflowError(422, "Give the model a name.")
    if payload.engine == "pipeline" and not shutil.which("codex"):
        raise WorkflowError(422, "The Codex CLI is not installed. Select the local profile or install the existing pipeline prerequisites.")
    with store.lock:
        if sum(r["status"] in ("queued", "running") for r in store.all()) >= 6:
            raise WorkflowError(429, "Six runs are already active or queued; wait for one to finish.")
        parent = store.get(payload.parent_run_id) if payload.parent_run_id else None
        if parent and parent["status"] in ("queued", "running"):
            raise WorkflowError(409, "Wait for the parent run to finish before revising its requirements.")
        if parent and not (payload.revision_rationale or "").strip():
            raise WorkflowError(422, "Explain the proposed source or interpretation change.")
        behavior_origin = None
        if payload.analysis_mode == "check_design":
            from review_contracts import _hash
            if parent:
                if (design_review.get("parent_source_hash") != parent["source_hash"]
                        or design_review.get("parent_evidence_hash") != parent.get("evidence_hash")
                        or not parent.get("evidence_hash")):
                    raise WorkflowError(409, "The reviewed parent source or evidence is stale or missing. Reload the parent and review the candidate again.")
                for artifact in parent.get("artifacts", []):
                    path = store.directory(parent["id"]) / artifact["name"]
                    if path.is_symlink() or not path.is_file() or path.resolve().parent != store.directory(parent["id"]).resolve() or profile.digest(path.read_bytes()) != artifact["sha256"]:
                        raise WorkflowError(409, "Parent artifacts changed after review. Review fresh evidence before checking the design.")
            elif design_review.get("parent_source_hash") or design_review.get("parent_evidence_hash"):
                raise WorkflowError(422, "Parent review hashes require a parent run.")
            design_review.update(reviewer=design_review["reviewer"].strip(), rationale=design_review["rationale"].strip(),
                                 created_at=now(), source_hash=profile.digest(payload.text), behavior_sha256=_hash(behavior),
                                 findings=design_review_findings(behavior),
                                 scope="Reviewed candidate and assumptions for this design check; no compliance result or source amendment is approved.")
            behavior_origin = "engineer_reviewed"
            parent_proposal = (parent or {}).get("behavior_proposal") or {}
            if parent_proposal.get("status") == "proposed" or (parent or {}).get("behavior_origin") == "llm_proposed_reviewed":
                behavior_origin = "llm_proposed_reviewed"
                design_review["proposal_run_id"] = parent["id"]
                design_review["proposal_sha256"] = _hash(parent_proposal["candidate"] if parent_proposal.get("candidate") else parent["behavior"])
        candidate_inspection = None
        candidate_provenance = {}
        if behavior is not None:
            from review_candidate import inspect_candidate
            try:
                candidate_inspection = inspect_candidate(behavior, requirements, payload.candidate_provenance, "engineer_supplied")
            except (ValueError, TypeError, KeyError) as exc:
                raise WorkflowError(422, str(exc)) from exc
            candidate_provenance = candidate_inspection["provenance"]
            design_review["candidate_provenance_sha256"] = _hash(candidate_provenance)
            design_review["diagnostics"] = candidate_inspection["diagnostics"]
        from review_bindings import binding_review_hash
        run_id = "run-" + uuid.uuid4().hex[:16]
        run_dir = store.directory(run_id)
        run_dir.mkdir()
        input_file = "source." + {"text": "txt", "csv": "csv", "json": "json"}[payload.format]
        (run_dir / input_file).write_text(payload.text, encoding="utf-8")
        write_json(run_dir / "requirements.json", requirements)
        write_json(run_dir / "analysis_selection.json", {"analysis_mode": payload.analysis_mode, "engine": payload.engine})
        if candidate_inspection is not None:
            write_json(run_dir / "candidate_inspection.json", candidate_inspection)
            write_json(run_dir / "candidate_provenance.json", candidate_provenance)
        if design_review is not None:
            write_json(run_dir / "design_review.json", design_review)
        if behavior is not None:
            write_json(run_dir / "behavior.json", behavior)
        run = {"id": run_id, "name": payload.name.strip(), "status": "queued", "engine": payload.engine,
               "created_at": now(), "updated_at": now(), "execution_owner": process_identity(),
               "parent_run_id": payload.parent_run_id,
               "revision_rationale": payload.revision_rationale, "source_hash": profile.digest(payload.text),
               "evidence_hash": None, "input_file": input_file, "format": payload.format,
               "analysis_mode": payload.analysis_mode, "design_review": design_review,
               "behavior": behavior, "behavior_origin": behavior_origin,
               "review_workflow_version": 2,
               "candidate_inspection": candidate_inspection, "candidate_provenance": candidate_provenance,
               "architecture_binding_reviews": [], "architecture_binding_review_hash": binding_review_hash([]),
               "correction_summary": None,
               "behavior_proposal": None, "behavioral_analysis": None,
               "contracts": None, "contract_changes": None, "contract_reviews": [], "contract_review_hash": contract_review_hash([]),
               "assumptions": [], "assumption_reviews": [], "assumption_review_hash": assumption_review_hash([]),
               "requirements": requirements, "tlr": None, "analysis": {}, "model": None, "compilation": {},
               "stages": [{"id": id, "label": label, "status": "pending", "detail": ""} for id, label in STAGES],
               "artifacts": [], "reviews": [], "errors": [], "log": "", "superseded_by": [],
               "baseline": {"status": "pending", "blockers": ["Run has not completed."]}}
        run["stages"][0].update(status="passed", detail=f"Preserved {len(requirements)} requirements, identifiers, and source references.")
        for stage in run["stages"]:
            if stage["id"] == "behavior":
                stage["label"] = {"requirements": "Design behavior (not selected)", "propose_design": "Review proposed design", "check_design": "Check design behavior"}[payload.analysis_mode]
        store.save(run)
        if parent:
            parent["superseded_by"].append(run_id)
            parent["baseline"].update(status="superseded", blockers=baseline_blockers(parent))
            store.save(parent)
    return run


def candidate_requirements(payload: CandidateInspectionInput) -> list[dict]:
    if payload.requirements is not None:
        from review_contracts import _sources
        if payload.text is not None:
            raise ValueError("Supply requirements or source text, not both.")
        if not 1 <= len(payload.requirements) <= profile.MAX_REQUIREMENTS:
            raise ValueError("Supply a supported nonempty requirement set.")
        return _sources(payload.requirements)
    if payload.text is None:
        raise ValueError("Supply source text or parsed requirements for candidate inspection.")
    return profile.parse_requirements(payload.text, payload.format, payload.name)


def check_saved_artifacts(store: RunStore, run: dict) -> None:
    from review_bindings import binding_review_hash
    if ((run.get("review_workflow_version", 0) >= 2 or run.get("architecture_binding_review_hash") is not None)
            and run.get("architecture_binding_review_hash") != binding_review_hash(run.get("architecture_binding_reviews", []))):
        raise WorkflowError(409, "Architectural binding review history differs from its recorded hash. Reload intact review evidence.")
    issues = evidence_integrity(run)
    if issues:
        raise WorkflowError(409, {"message": "Recorded model or review snapshots differ from their evidence.", "issues": issues})
    for artifact in run.get("artifacts", []):
        path = store.directory(run["id"]) / artifact["name"]
        if (path.is_symlink() or not path.is_file() or path.resolve().parent != store.directory(run["id"]).resolve()
                or profile.digest(path.read_bytes()) != artifact["sha256"]):
            raise WorkflowError(409, "An artifact changed after analysis. Review fresh evidence before binding the model.")


def inspect_bindings(store: RunStore, run_id: str) -> dict:
    from review_bindings import binding_choices, binding_status, binding_review_hash
    run = store.get(run_id)
    check_saved_artifacts(store, run)
    records = run.get("architecture_binding_reviews", [])
    return {"choices": binding_choices(run), "records": records,
            "review_hash": run.get("architecture_binding_review_hash", binding_review_hash(records)),
            "status": binding_status(run)}


def record_assumption_review(store: RunStore, run_id: str, assumption_id: str, payload: AssumptionReviewInput) -> dict:
    if not payload.reviewer.strip() or not payload.rationale.strip():
        raise WorkflowError(422, "A reviewer and engineering rationale are required.")
    with store.lock:
        run = store.get(run_id)
        if run["status"] not in ("completed", "failed"):
            raise WorkflowError(409, "Wait for the run to finish before reviewing its assumptions.")
        if payload.source_hash != run["source_hash"] or payload.evidence_hash != run["evidence_hash"]:
            raise WorkflowError(409, "The reviewed source or evidence is stale. Reload the run.")
        if payload.assumption_review_hash != run.get("assumption_review_hash"):
            raise WorkflowError(409, "Assumption decisions changed. Reload the current review state.")
        assumption = next((a for a in run.get("assumptions", []) if a["id"] == assumption_id), None)
        if assumption is None:
            raise WorkflowError(404, "Assumption not found in this run.")
        for artifact in run["artifacts"]:
            path = store.directory(run_id) / artifact["name"]
            if path.is_symlink() or not path.is_file() or profile.digest(path.read_bytes()) != artifact["sha256"]:
                raise WorkflowError(409, "An artifact changed after analysis. Create a new run before reviewing it.")
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


def record_contract_review(store: RunStore, run_id: str, contract_id: str, payload: ContractReviewInput) -> dict:
    if not payload.reviewer.strip() or not payload.rationale.strip():
        raise WorkflowError(422, "A reviewer and engineering rationale are required.")
    with store.lock:
        run = store.get(run_id)
        if run["status"] not in ("completed", "failed"):
            raise WorkflowError(409, "Wait for the run to finish before reviewing contracts.")
        if payload.source_hash != run["source_hash"] or payload.evidence_hash != run["evidence_hash"]:
            raise WorkflowError(409, "The reviewed source or evidence is stale. Reload the run.")
        if payload.contract_review_hash != run.get("contract_review_hash"):
            raise WorkflowError(409, "Contract decisions changed. Reload the current review state.")
        contract = next((c for c in (run.get("contracts") or {}).get("contracts", []) if c["id"] == contract_id), None)
        if contract is None:
            raise WorkflowError(404, "Contract not found in this run.")
        for artifact in run["artifacts"]:
            path = store.directory(run_id) / artifact["name"]
            if path.is_symlink() or not path.is_file() or path.resolve().parent != store.directory(run_id).resolve() or profile.digest(path.read_bytes()) != artifact["sha256"]:
                raise WorkflowError(409, "An artifact changed after analysis. Create a new run before reviewing it.")
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


def record_binding_review(store: RunStore, run_id: str, payload: BindingReviewInput) -> dict:
    from review_bindings import validate_binding, binding_review_hash
    with store.lock:
        run = store.get(run_id)
        if run["status"] not in ("completed", "failed"):
            raise WorkflowError(409, "Wait for analysis to finish before reviewing architectural bindings.")
        expected = run.get("architecture_binding_review_hash", binding_review_hash(run.get("architecture_binding_reviews", [])))
        if (payload.source_hash != run["source_hash"] or payload.evidence_hash != run.get("evidence_hash")
                or payload.architecture_binding_review_hash != expected
                or payload.model_sha256 != profile.digest((run.get("model") or {}).get("text", ""))):
            raise WorkflowError(409, "The binding source, evidence, model, or review state is stale. Reload this run.")
        check_saved_artifacts(store, run)
        try:
            record = validate_binding(run, payload.model_dump())
        except (ValueError, TypeError, KeyError) as exc:
            raise WorkflowError(422, str(exc)) from exc
        record.update(id="binding-review-" + uuid.uuid4().hex[:12], created_at=now(), run_id=run_id)
        run.setdefault("architecture_binding_reviews", []).append(record)
        run["architecture_binding_review_hash"] = binding_review_hash(run["architecture_binding_reviews"])
        run["baseline"] = {"status": "superseded" if run.get("superseded_by") else "pending",
                           "blockers": baseline_blockers(run),
                           "scope": "Architectural binding decisions changed; record final acceptance again."}
        store.save(run)
        return record


def record_review(store: RunStore, run_id: str, payload: ReviewInput) -> dict:
    if not payload.reviewer.strip() or not payload.rationale.strip() or not payload.scope.strip():
        raise WorkflowError(422, "Reviewer, rationale, and review scope must be nonempty.")
    with store.lock:
        run = store.get(run_id)
        if run["status"] not in ("completed", "failed"):
            raise WorkflowError(409, "Wait for analysis to finish before recording a review.")
        if payload.source_hash != run["source_hash"] or payload.evidence_hash != run["evidence_hash"]:
            raise WorkflowError(409, "The reviewed source or evidence revision is stale. Reload the run.")
        check_saved_artifacts(store, run)
        if run.get("assumptions") and payload.assumption_review_hash != run.get("assumption_review_hash"):
            raise WorkflowError(409, "Assumption decisions changed or were not acknowledged. Reload their current review state.")
        if run.get("contracts") and payload.contract_review_hash != run.get("contract_review_hash"):
            raise WorkflowError(409, "Contract decisions changed or were not acknowledged. Reload their current review state.")
        if payload.decision == "approve" and run.get("parent_run_id") and not payload.acknowledge_contract_changes:
            raise WorkflowError(409, "Explicitly acknowledge the proposed semantic changes, including removed contracts and changed assumptions.")
        if payload.decision == "approve" and run.get("review_workflow_version", 0) >= 2:
            if run.get("behavior") and payload.architecture_binding_review_hash != run.get("architecture_binding_review_hash"):
                raise WorkflowError(409, "Architectural binding decisions changed or were not acknowledged. Reload their current state.")
            if run.get("parent_run_id") and payload.correction_summary_sha256 != (run.get("correction_summary") or {}).get("sha256"):
                raise WorkflowError(409, "Inspect and acknowledge the exact correction summary before accepting this revision.")
        blockers = baseline_blockers(run)
        if payload.decision == "approve" and (blockers or not payload.acknowledge):
            raise WorkflowError(409, {"message": "This prototype model is not ready for acceptance.",
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
                  "candidate_inspection_snapshot": deepcopy(run.get("candidate_inspection")),
                  "architecture_binding_decisions": deepcopy(run.get("architecture_binding_reviews", [])),
                  "correction_summary_snapshot": deepcopy(run.get("correction_summary")),
                  "authority": "Self-declared local reviewer; no organizational identity verification.",
                  "source_change_authorized": False, "acceptance_kind": "prototype model and interpreted scope only"}
        run["reviews"].append(record)
        run["baseline"] = {"status": "approved" if payload.decision == "approve" else "superseded" if run["superseded_by"] else "pending",
                           "blockers": blockers, "scope": "Local prototype model acceptance; no authoritative source amendment."}
        store.save(run)
        return record
