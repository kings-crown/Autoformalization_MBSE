"""Optional, conservative bridge from the review prototype to the existing CLI.

This module never repairs source requirements.  An LLM pipeline result remains
pending semantic review even when the encoded constraint set is satisfiable.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import tomllib
from typing import Any, Callable

import requirements_pipeline as legacy
from review_interpretation import build_pipeline_interpretations


Progress = Callable[[str, str, str], None]


def _json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _correct_trace_typing(trace_path: Path) -> list[dict[str, Any]]:
    """Correct only bare usages that name a known local part definition.

    Preserve the exact generated artifact and every changed line.  Requirement
    text, constraints, definition inheritance, and usage subsetting stay intact.
    """
    original = trace_path.read_bytes().decode("utf-8")
    # Mask comments and strings without changing positions or line numbers.
    tokens = r'/\*.*?\*/|//[^\r\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    masked = re.sub(tokens, lambda match: re.sub(r"[^\r\n]", " ", match.group(0)), original, flags=re.DOTALL)
    definitions = re.findall(r"(?m)^[ \t]*(?:individual[ \t]+)?part[ \t]+def[ \t]+([A-Za-z_][A-Za-z0-9_]*)\b", masked)
    known = {name for name in definitions if definitions.count(name) == 1}
    usage = re.compile(r"^[ \t]*part[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]+(?P<operator>:>)[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*;[ \t]*$")
    lines = original.splitlines(keepends=True)
    corrections = []
    for index, visible in enumerate(masked.splitlines()):
        match = usage.fullmatch(visible)
        if not match or match.group(3) not in known:
            continue
        before = lines[index]
        operator = match.start("operator")
        after = before[:operator] + ":" + before[operator + 2:]
        lines[index] = after
        corrections.append({
            "artifact": str(trace_path), "line": index + 1,
            "before": before.rstrip("\r\n"), "after": after.rstrip("\r\n"),
            "reason": "This bare part usage names a local part definition and requires the typing operator ':'; no requirement or constraint was changed.",
        })
    if corrections:
        backup = trace_path.with_suffix(".original.txt")
        if backup.exists():
            raise ValueError("Refusing to overwrite the original trace artifact.")
        corrected = "".join(lines)
        backup.write_bytes(original.encode("utf-8"))
        trace_path.write_bytes(corrected.encode("utf-8"))
        (trace_path.parent / "corrections.json").write_text(json.dumps({
            "original_artifact": str(backup), "corrected_artifact": str(trace_path),
            "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "corrected_sha256": hashlib.sha256(corrected.encode("utf-8")).hexdigest(),
            "corrections": corrections,
        }, indent=2) + "\n", encoding="utf-8")
    return corrections


def _selected_model(env: dict[str, str]) -> tuple[str, str]:
    for key in ("MBSE_REVIEW_PIPELINE_MODEL", "CODEX_MBSE_MODEL"):
        if env.get(key, "").strip():
            return env[key].strip(), key
    # Read only the model identifier; leave authentication and all CLI safeguards
    # under the installed Codex CLI's normal configuration handling.
    codex_root = Path(env.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        with (codex_root / "config.toml").open("rb") as handle:
            configured = tomllib.load(handle).get("model")
        if isinstance(configured, str) and configured.strip():
            return configured.strip(), "Codex user configuration"
    except (OSError, ValueError):
        pass
    return legacy.DEFAULT_CODEX_MODEL, "legacy default"


def _bounded_timeout() -> float:
    try:
        return max(5.0, min(1200.0, float(os.getenv("MBSE_REVIEW_PIPELINE_TIMEOUT", "600"))))
    except ValueError:
        return 600.0


def _semantic_gate_summary(semantic: dict[str, Any] | None) -> str | None:
    """Explain the blocking gate separately from the full-formula SAT verdict."""
    if not semantic or semantic.get("passed") is not False:
        return None
    checks = semantic.get("checks", {})
    labels = {
        "named_assertion_coverage": "requirement assertion coverage",
        "same_state": "shared-context state alignment",
        "pairwise_conflict": "pairwise constraint checks",
        "vacuity": "vacuity checks",
    }
    failed = []
    for key, label in labels.items():
        result = checks.get(key, {})
        if result.get("passed") is not False:
            continue
        count = len(result.get("solver_errors", []))
        failed.append(f"{label} ({count} solver probe error{'s' if count != 1 else ''})" if count else label)
    detail = "; ".join(failed) or "required semantic checks did not pass"
    return f"SysML generation blocked by {detail}. Inspect pipeline_model_semantic_checks.json for the recorded evidence."


def _stop_group(process: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


def _recheck(fragment: str, run_dir: Path) -> dict[str, Any]:
    """Replay a single, final consistency query without changing its assertions."""
    forms = legacy._parse_sexpr(fragment)
    allowed = {
        "set-logic", "set-option", "declare-sort", "declare-fun",
        "declare-const", "define-fun", "assert", "check-sat", "get-model",
        "get-unsat-core",
    }
    heads = [form[0] if isinstance(form, list) and form else "" for form in forms]
    if any(head not in allowed for head in heads) or heads.count("check-sat") != 1:
        return {"status": "error", "diagnostics": "Cannot independently review a fragment with unsupported commands or multiple satisfiability queries."}
    last_query = heads.index("check-sat")
    if any(head not in {"get-model", "get-unsat-core"} for head in heads[last_query + 1:]):
        return {"status": "error", "diagnostics": "Assertions occur after the solver query; no complete constraint-set verdict is available."}
    # Prevent generated SMT options from redirecting solver output to a file.
    if any(form[0] == "set-option" and len(form) > 1 and "output-channel" in str(form[1]) for form in forms):
        return {"status": "error", "diagnostics": "Solver output redirection is unsupported in review mode."}
    result = legacy.run_z3_fragment(fragment)
    result["artifact_sha256"] = hashlib.sha256(fragment.encode("utf-8")).hexdigest()
    verdict = legacy._solver_verdict(result)
    if verdict in {"sat", "unsat"}:
        query = "get-model" if verdict == "sat" else "get-unsat-core"
        option = "produce-models" if verdict == "sat" else "produce-unsat-cores"
        if query not in heads:
            evidence = f"(set-option :{option} true)\n{fragment.rstrip()}\n({query})\n"
            evidence_path = run_dir / "pipeline_review_evidence.smt2"
            evidence_path.write_text(evidence, encoding="utf-8")
            result["evidence"] = legacy.run_z3_fragment(evidence)
        else:
            result["evidence"] = dict(result)
    return result


def run_existing_pipeline(
    run_dir: Path,
    name: str,
    requirements: list[dict[str, Any]],
    progress: Progress | None = None,
    propose_behavior: bool = True,
) -> dict[str, Any]:
    """Run the actual Codex CLI pipeline, retaining partial and failed artifacts.

    ``run_dir`` should be a fresh directory allocated by the caller.  Compilation
    belongs to the caller; this adapter only emits a candidate SysML model.
    """
    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []

    def notify(stage: str, status: str, detail: str) -> None:
        if progress:
            progress(stage, status, detail)

    input_path = run_dir / "input.csv"
    prefix = run_dir / "pipeline_model"
    package = legacy._sanitize_identifier(name, prefix="PipelineModel")
    model_path = run_dir / f"{package}.sysml"
    trace_path = run_dir / f"{package}_trace.sysml"
    log_path = run_dir / "pipeline.log"
    # Do not accidentally adopt outputs from an earlier invocation.
    if any(path.exists() for path in [prefix.with_name(prefix.name + "_translate.json"), model_path, log_path]):
        raise ValueError("The existing-pipeline adapter requires a fresh run directory.")
    with input_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "text"])
        writer.writeheader()
        for req in requirements:
            writer.writerow({"id": req["id"], "text": req["text"]})
    (run_dir / "pipeline_source_requirements.json").write_text(
        json.dumps(requirements, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    notify("extraction", "complete", f"Preserved {len(requirements)} input requirements and their source references.")
    notify("interpretation", "running", "Running the existing Codex requirements pipeline.")
    env = os.environ.copy()
    env.update({
        "SMT_MAX_SEMANTIC_REPAIRS": "0", "SMT_FIX_ATTEMPTS": "1",
        "MBSE_TLR_TYPECHECK_MODE": "error", "MBSE_SOLVER": "z3",
        "PYTHONDONTWRITEBYTECODE": "1", "CODEX_STREAM": "0",
        "MBSE_REVIEW_CAPTURE_DIR": str(run_dir),
        "MBSE_REVIEW_PROPOSE_BEHAVIOR": "1" if propose_behavior else "0",
    })
    # Preserve every imported clause at the legacy SMT-prompt boundary. The
    # default 12,000-character source prefix silently omits larger uploads.
    source_for_prompt = input_path.read_text(encoding="utf-8")
    try:
        configured_limit = int(env.get("MBSE_STATEMENT_PROMPT_MAX_CHARS", "12000"))
    except ValueError:
        configured_limit = 12000
    source_limit = max(1, configured_limit, len(source_for_prompt))
    env["MBSE_STATEMENT_PROMPT_MAX_CHARS"] = str(source_limit)
    (run_dir / "prompt_coverage.json").write_text(json.dumps({
        "requirements": len(requirements), "source_characters": len(source_for_prompt),
        "statement_prompt_limit": source_limit, "source_truncated": False,
        "scope": "Full imported source is available to SMT generation; this does not establish semantic coverage of generated assertions."
    }, indent=2) + "\n", encoding="utf-8")
    model, model_source = _selected_model(env)
    env["CODEX_MBSE_MODEL"] = model
    command = [
        sys.executable, str(Path(__file__).with_name("review_llm_entry.py")),
        "--statement", str(input_path), "--output-prefix", str(prefix),
        "--sysml-output", str(model_path), "--traceability-output", str(trace_path),
        "--sysml-mode", "domain", "--llm-provider", "codex", "--model", model,
        "--skip-intent-formalization", "--semantic-strict", "--skip-sysml-compile",
    ]
    started = time.monotonic()
    returncode: int | None = None
    with log_path.open("w", encoding="utf-8") as log_handle:
        process: subprocess.Popen[Any] | None = None
        try:
            process = subprocess.Popen(
                command, cwd=Path(legacy.__file__).resolve().parent.parent,
                env=env, stdout=log_handle, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = started + _bounded_timeout()
            while process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _stop_group(process)
                    errors.append("The existing pipeline exceeded its execution deadline. Partial artifacts were retained.")
                    break
                try:
                    process.wait(timeout=min(1.0, remaining))
                except subprocess.TimeoutExpired:
                    pass
            returncode = process.returncode
            if returncode != 0 and not errors:
                errors.append(f"The existing pipeline exited with code {returncode}. Partial artifacts were retained.")
        except (OSError, subprocess.SubprocessError) as exc:
            if process and process.poll() is None:
                _stop_group(process)
            errors.append(f"The existing pipeline could not complete: {exc}")
        except BaseException:
            if process and process.poll() is None:
                _stop_group(process)
            raise
    log = log_path.read_text(encoding="utf-8", errors="replace")
    translated = _json(prefix.with_name(prefix.name + "_translate.json")) or {}
    semantic_checks = _json(prefix.with_name(prefix.name + "_semantic_checks.json"))
    semantic_blocker = _semantic_gate_summary(semantic_checks)
    semantic_details = (semantic_checks or {}).get("checks", {})
    skipped_guards = semantic_details.get("vacuity", {}).get("skipped_administrative_guards", [])
    semantic_limitations = []
    if skipped_guards:
        semantic_limitations.append(f"Operational trigger vacuity remains unchecked for {len(skipped_guards)} enable-guarded requirement(s).")
    if semantic_details.get("pairwise_conflict", {}).get("skipped"):
        semantic_limitations.append("Pairwise conflict diagnostics were skipped because of the configured size limit.")
    if semantic_blocker and semantic_blocker not in errors:
        errors.append(semantic_blocker)
    raw_tlr = translated.get("typed_requirements_form") or translated.get("typed_logical_form")
    if not isinstance(raw_tlr, dict):
        raw_tlr = _json(prefix.with_name(prefix.name + "_tlr.json"))
    tlr_origin = "pipeline_output"
    if not isinstance(raw_tlr, dict):
        raw_tlr = _json(run_dir / "initial_tlr.json")
        tlr_origin = "native_preflight" if raw_tlr else "unavailable"
    sat_path = prefix.with_name(prefix.name + "_sat.smt2")
    fragment = sat_path.read_text(encoding="utf-8") if sat_path.exists() else ""
    review_entries = build_pipeline_interpretations(requirements, raw_tlr, fragment, sat_path.name)
    tlr = {
        "schema_version": "review-pipeline-1", "engine": "pipeline", "origin": tlr_origin,
        "raw": raw_tlr, "requirements": review_entries,
        "symbols": (raw_tlr or {}).get("symbol_table", []),
        "symbol_table": (raw_tlr or {}).get("symbol_table", []),
    }
    (run_dir / "pipeline_review_interpretation.json").write_text(json.dumps(tlr, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    pending_review_count = sum(entry["status"] == "pending_review" for entry in review_entries)
    needs_interpretation_count = sum(entry["status"] == "needs_interpretation" for entry in review_entries)
    interpretation_counts = (f"{pending_review_count} candidate interpretation(s) available with engineer review pending; "
                             f"{needs_interpretation_count} requirement(s) need interpretation.")
    notify("interpretation", "failed" if not raw_tlr else "partial" if tlr_origin == "native_preflight" or needs_interpretation_count else "complete",
           ("Native preflight TLR retained; LLM interpretation did not complete. " + interpretation_counts) if tlr_origin == "native_preflight" else
           interpretation_counts if raw_tlr else "The existing pipeline did not emit a TLR artifact.")
    notify("analysis", "running", "Rechecking consistency of the emitted SMT constraint set.")
    solver: dict[str, Any] = {"status": "not_run"}
    if sat_path.exists():
        try:
            solver = _recheck(fragment, run_dir)
        except Exception as exc:
            solver = {"status": "error", "diagnostics": str(exc)}
    verdict = legacy._solver_verdict(solver)
    source_ids = [str(req["id"]) for req in requirements]
    mapped_ids = [str(entry["id"]) for entry in review_entries if entry["mapping_status"] == "unique"]
    evidence = solver.get("evidence", {})
    evidence_text = str(evidence.get("result", "")) if evidence.get("status") == "ok" else ""
    core = []
    if verdict == "unsat":
        core = [rid for rid in source_ids if re.search(r"(?<![\w])req_" + re.escape(legacy._sanitize_req_id(rid)) + r"(?![\w])", evidence_text)]
    summary = {
        "sat": "The generated SMT constraint set is consistent. Source fidelity still requires engineer review.",
        "unsat": "The generated SMT constraint set is contradictory. Review the encoding and original requirements.",
    }.get(verdict, "No conclusive consistency result is available for the generated constraint set.")
    if semantic_blocker:
        summary += " " + semantic_blocker
    if semantic_limitations:
        summary += " " + " ".join(semantic_limitations)
    diagnostics = "\n".join(filter(None, [str(solver.get("diagnostics", "")), *errors]))
    sanitation_notes = [
        str(note)
        for iteration in translated.get("smt_iterations", []) if isinstance(iteration, dict)
        for note in iteration.get("sanitation_notes", [])
    ]
    analysis = {
        "status": "unsat" if verdict == "unsat" else "partial" if verdict == "sat" else "unknown",
        "solver_status": verdict or ("not_run" if solver.get("status") == "not_run" else "unknown"),
        "summary": summary,
        "scope": "Consistency of the LLM-generated SMT encoding only; no safety proof or source-fidelity guarantee.",
        "checked_ids": mapped_ids if verdict in {"sat", "unsat"} else [],
        "unsupported_ids": [str(entry["id"]) for entry in review_entries if entry["status"] == "unsupported"],
        "pending_review_ids": [str(entry["id"]) for entry in review_entries if entry["status"] == "pending_review"],
        "needs_interpretation_ids": [str(entry["id"]) for entry in review_entries if entry["status"] == "needs_interpretation"],
        "missing_mapping_ids": [str(entry["id"]) for entry in review_entries if entry["mapping_status"] in {"missing", "invalid_context"}],
        "ambiguous_mapping_ids": [str(entry["id"]) for entry in review_entries if entry["mapping_status"] == "ambiguous"],
        "unsat_core": core, "witness": {}, "witness_raw": evidence_text if verdict == "sat" else "",
        "assumptions": ["Generated declarations, domains, bounds, and guards require review against the original requirements."],
        "diagnostics": diagnostics, "source_fidelity": "pending", "approval_blocked": True,
        "solver_result": solver, "pipeline_returncode": returncode,
        "pipeline_model": model, "pipeline_model_source": model_source,
        "pipeline_diagnostics": log[-6000:] if errors else "",
        "pipeline_semantic_checks": semantic_checks,
        "generation_blocker": semantic_blocker,
        "semantic_limitations": semantic_limitations,
        "sanitation_notes": sanitation_notes,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "artificial_unsat_note": "The legacy negative query appends assert false. It is retained as a diagnostic artifact and is not safety evidence.",
    }
    notify("analysis", "failed" if semantic_blocker else "partial" if semantic_limitations and verdict in {"sat", "unsat"} else "complete" if verdict in {"sat", "unsat"} else "failed", summary)
    model_result = None
    corrections = _correct_trace_typing(trace_path) if trace_path.exists() else []
    if model_path.exists():
        model_result = {
            "text": model_path.read_text(encoding="utf-8"), "kind": "pipeline-domain",
            "summary": "Candidate SysML v2 domain model from the existing pipeline; semantic review and compilation remain separate.",
            "path": str(model_path), "trace_path": str(trace_path) if trace_path.exists() else None,
            "corrections": corrections,
            "notes": ([f"Corrected {len(corrections)} bare trace part usages to reference their local part definitions with ':'. Original trace and exact changes are retained in artifacts."] if corrections else []),
        }
    notify("generation", "complete" if model_result else "failed",
           "Candidate SysML emitted for review." if model_result else semantic_blocker or "No SysML candidate was emitted; partial artifacts are available. Inspect pipeline.log for the cause.")
    (run_dir / "pipeline_review_analysis.json").write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")
    return {
        "tlr": tlr, "analysis": analysis, "model": model_result,
        "behavior_proposal": _json(run_dir / "llm_behavior_proposal.json"),
        "errors": errors, "log": log[-40000:],
        "artifacts": sorted(path for path in run_dir.iterdir() if path.is_file()),
    }
