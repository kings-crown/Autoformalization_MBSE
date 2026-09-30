#!/usr/bin/env python3
"""LLM requirements -> executable TLR -> shared SMT/SysML, with A/B/C study support."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import shutil
from pathlib import Path
import sys
import time
from canonical_abstractions import POLICY_VERSION, POLICY, POLICY_TEXT, PROFILE, representation_summary
from canonical_repair import RECOVERY_POLICY_VERSION

ROOT = Path(__file__).resolve().parents[1]
TLR_INSTRUCTIONS = '''Return a single JSON object, schema "mbse_tlr/1". Required keys:
abstraction_policy: "mbse_abstraction/1".
variables: [{name, type: "Bool"|"Int"|"Real", unit?: string, bounds?: {lower?: exact_decimal_string, upper?: exact_decimal_string}, description?: string}].
assumptions: [{id, text, predicate: Boolean_AST}].
requirements: [{id: exact_source_ID, status: "supported"|"unsupported"|"unresolved", formula: Boolean_AST when supported, reason: explanatory_string when not supported}].
Every supported requirement also needs abstraction: {kind: "state_constraint"|"capability"|"event_relation", meaning: nonempty_text, scope: nonempty_text, limitations: [explicit_limitations]}. A capability abstraction additionally requires subject, operation and symbol strings; its formula must be {"var": symbol} for the Boolean availability attribute. Use a nonempty description for every referenced variable. Capability and event_relation limitations cannot be empty. Event relations are guarded implications over distinct defined symbols. Unsupported rows need reason_code "profile_limit" or "resource_limit"; unresolved rows need "source_ambiguity" or "missing_context". Do not put abstraction/formula on unsupported/unresolved rows.
AST forms: true/false; {"var":"declared_name"}; {"value":"exact_decimal_string","unit":"V"}; {"op":"operator","args":[AST,...]}.
Operators: and/or (2..16 Boolean args), not (1), implies (2), ite (condition,then,else), =/!= and </<=/>/>= (2 compatible operands), + (2), - (1 or 2), * (2 with a fixed dimensionless literal factor). No raw SMT/code, floats, quantifiers, next-state references or nonlinear products. Comparison/addition units must agree. Numeric units include 1,s,ms,min,V,mV,A,mA,W,kW,J,kJ,Wh,kWh,m,cm,mm,km,kg,g,%,K. Use at most 24 variables and 40 background assumptions; retain unsupported source clauses explicitly if outside the profile. Empty variables are allowed when all clauses are unsupported.
Every source ID must appear exactly once. Reuse the same variable for the same physical quantity across clauses. Preserve conditions, modalities, endpoints, units and exceptions. Never replace a requirement with an unexplained Boolean like R1_holds or a truth constant. Keep source obligations in requirements, not in background assumptions or variable bounds. Disclose genuine background assumptions in assumptions; do not invent premises to make a result satisfiable. With supplied fixed vocabulary/background, use precisely that context and introduce no further symbols or assumptions. If fixed_context contains symbol_meanings, copy each meaning verbatim into the corresponding variable description; definitions are fixed inputs, not formulas. Missing context is unresolved, not permission to invent meaning.
Example expression for an obligation requiring motion disabled when maintenance is enabled:
{"op":"implies","args":[{"var":"maintenance"},{"op":"not","args":[{"var":"motion_enabled"}]}]}.
Do not return SysML, solver outcomes, approvals, hashes, or provenance certificates.
'''
SYSML_INSTRUCTIONS = '''Return only SysML v2 textual syntax for the supplied source requirements. Use one package with ScalarValues, a shared subject part definition with Boolean/Integer/Real attributes, and one requirement usage per source ID with actual require constraints for supported obligations. Retain exact source text in documentation. Numeric attributes denote canonical magnitudes; document their units. Preserve conditions, inclusive/exclusive bounds, quantity identities and genuine environmental assumptions. Unsupported temporal, probabilistic or otherwise unrepresentable clauses must remain explicitly documented as unsupported; do not invent Boolean placeholders or extra architecture. Use the supplied fixed vocabulary/background if present. Do not output TLR, SMT, explanations, approvals, hashes or invented verification results.'''

TLR_INSTRUCTIONS += "\n" + POLICY_TEXT
SYSML_INSTRUCTIONS += "\nDocument each supported requirement's abstraction kind, meaning, scope and limitations; for a capability also its subject, operation and bound Boolean symbol. Define every referenced attribute's meaning. When fixed_context supplies symbol_meanings, preserve those definitions in the corresponding attribute documentation. Use the same abstraction kinds and limits as the shared policy, without emitting JSON or TLR.\n" + POLICY_TEXT


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def sources_from_file(path, fmt=None):
    from review_profile import parse_requirements
    path = Path(path)
    fmt = fmt or {".json": "json", ".csv": "csv", ".txt": "text"}.get(path.suffix.lower())
    if fmt is None:
        raise ValueError("Use CSV, JSON or TXT, or --format")
    # Strict JSON loading rejects duplicate keys before the shared importer can
    # normalize input. The importer still enforces IDs, text and size limits.
    original = read_json(path) if fmt == "json" else None
    rows = parse_requirements(path.read_text(encoding="utf-8"), fmt, path.name)
    original_rows = original.get("requirements") if isinstance(original, dict) else original
    sources = []
    for index, row in enumerate(rows):
        source = {key: value for key, value in row["source"].items() if key != "provenance_status"}
        text = row["text"]
        if fmt == "json":
            supplied = original_rows[index]
            if isinstance(supplied, str):
                text = supplied
            else:
                text = next(supplied[key] for key in ("text", "requirement", "statement", "description")
                            if supplied.get(key))
                if isinstance(supplied.get("source"), dict):
                    # Supplied citations/context are authoritative input data;
                    # do not drop extra fields or replace locations on import.
                    source = deepcopy(supplied["source"])
        sources.append({"id": row["id"], "text": text, "source": source})
    return sources


def _fixed_context(raw):
    if raw is None:
        return None
    from mutation_core import validate_context
    if (not isinstance(raw, dict) or not {"variables", "background"} <= set(raw)
            or set(raw) - {"variables", "background", "symbol_meanings"}):
        raise ValueError("Context must contain variables and background, with optional symbol_meanings")
    result = validate_context(raw["variables"], raw["background"])
    if "symbol_meanings" in raw:
        meanings = raw["symbol_meanings"]
        if (not isinstance(meanings, dict) or set(meanings) != {v["name"] for v in result["variables"]}
                or any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in meanings.values())):
            raise ValueError("Fixed symbol_meanings must define every declared variable exactly once")
        result["symbol_meanings"] = deepcopy(meanings)
    return result


def _context_equal(left, right):
    return (sorted(left["variables"], key=lambda v: v["name"]) == sorted(right["variables"], key=lambda v: v["name"])
            and sorted(left["background"], key=lambda a: a["id"]) == sorted(right["background"], key=lambda a: a["id"]))


def _model(requested=None):
    if requested:
        return requested
    from review_pipeline_adapter import _selected_model
    return _selected_model(os.environ)[0]


def _ask(system, prompt, model, directory, call_id):
    from requirements_pipeline import _run_codex_exec
    record = {"model": model, "system_prompt": system, "user_prompt": prompt,
              "status": "running", "input_tokens": None, "output_tokens": None, "estimated_cost": None,
              "usage_note": "Unavailable unless reported by the configured transport; not treated as zero."}
    started = time.monotonic()
    write_json(directory / (call_id + ".json"), record)
    try:
        composed = "System instructions:\n" + system.strip() + "\n\nUser request:\n" + prompt.strip() + "\n"
        response = _run_codex_exec(composed, model)
        record.update(status="completed", response=response)
        return response
    except Exception as exc:
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        record["latency_seconds"] = time.monotonic() - started
        write_json(directory / (call_id + ".json"), record)


def _strip_fence(text):
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return text


def _prompt(sources, context, name):
    return json.dumps({"model_name": name, "representation_profile": PROFILE,
                       "abstraction_policy": POLICY,
                       "requirements": [{k: r[k] for k in ("id", "text", "source") if k in r} for r in sources],
                       "fixed_context": context}, ensure_ascii=False, indent=2)


def _compile(path, enabled):
    if not enabled:
        return {"status": "not_run", "reason": "Explicit --skip-compile; no admission claim"}
    from review_sysml import compile_sysml
    return compile_sysml(path)


def _repair_budget(value, label="Abstention repair budget"):
    if type(value) is not int or not 0 <= value <= 5:
        raise ValueError(f"{label} must be an integer from 0 to 5")
    return value


def _normalize_candidate(raw, sources, context, require_abstractions):
    from canonical_tlr import tlr_context, validate_tlr
    # validate_tlr restores authoritative citations. Reject attempted substitutions
    # before normalization so a proposal cannot conceal an input change.
    source_by_id = {row["id"]: row for row in sources}
    if isinstance(raw, dict) and isinstance(raw.get("requirements"), list):
        for row in raw["requirements"]:
            if isinstance(row, dict) and row.get("id") in source_by_id:
                original = source_by_id[row["id"]]
                for key in ("text", "source"):
                    if key in row and row[key] != original.get(key):
                        raise ValueError(f"Requirement {row['id']}: generated {key} differs from prepared source")
    normalized = validate_tlr(raw, sources, require_abstractions=require_abstractions)
    if context is not None and not _context_equal(tlr_context(normalized), context):
        raise ValueError("Generated vocabulary/background differs from the fixed study context")
    if context is not None and "symbol_meanings" in context:
        meanings = {v["name"]: v.get("description") for v in normalized["variables"]}
        if meanings != context["symbol_meanings"]:
            raise ValueError("Generated symbol descriptions differ from fixed symbol_meanings")
    return normalized


def _materialize_candidate(directory, candidate, tlr, condition, compile_model, solver, timeout):
    from canonical_sysml_screen import requirement_content
    result = {"model_file": "model.sysml", "tlr": tlr, "admission": "not_assessed"}
    if tlr is not None:
        write_json(directory / "tlr.json", tlr)
        result["representation"] = representation_summary(tlr)
        write_json(directory / "representation.json", result["representation"])
    path = directory / "model.sysml"
    path.write_text(candidate, encoding="utf-8")
    result["candidate_content"] = requirement_content(candidate)
    write_json(directory / "candidate_content.json", result["candidate_content"])
    result["compilation"] = _compile(path, compile_model)
    write_json(directory / "compilation.json", result["compilation"])
    if condition in {"C", "BC"}:
        from canonical_audits import audit_tlr
        result["analysis"] = audit_tlr(tlr, directory / "audit", timeout, solver)
        allowed = result["analysis"].get("admitted", False) and result["compilation"].get("status") == "passed"
        result["admission"] = "admitted_consistent_encoding" if allowed else "withheld"
    else:
        result["analysis"] = {"status": "not_run", "reason": "Condition does not execute solver checks"}
    write_json(directory / "analysis.json", result["analysis"])
    return result


_CANDIDATE_FILES = ("candidate_tlr.json", "tlr.json", "representation.json", "model.sysml",
                    "candidate_content.json", "compilation.json", "analysis.json", "audit",
                    "development_scenarios")


def _copy_candidate(source, destination):
    for name in _CANDIDATE_FILES:
        src, dst = source / name, destination / name
        if not src.exists():
            continue
        if src.is_dir():
            # Only this run's generated evidence is replaced. Attempt histories
            # and source packets are never overwritten by candidate selection.
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def _recover_abstentions(initial, sources, context, directory, budget, ask, model, name,
                         condition, compile_model, solver, timeout):
    from canonical_repair import (DIAGNOSIS_INSTRUCTIONS, REPAIR_INSTRUCTIONS,
                                  diagnosis_prompt, repair_prompt, validate_diagnosis,
                                  validate_proposal, validate_repair)
    from canonical_tlr import render_sysml
    def abstentions(value):
        return [row["id"] for row in value["tlr"]["requirements"] if row["status"] != "supported"]
    initial_ids = abstentions(initial)
    ledger = {"schema": "abstention_recovery/1", "policy": RECOVERY_POLICY_VERSION,
        "budget": budget, "max_additional_transport_invocations": 2 * budget,
        "diagnosis_calls": 0, "repair_attempts": 0, "accepted_repairs": 0,
        "initial_abstained_ids": initial_ids, "recovered_ids": [], "retained_ids": initial_ids,
        "overridden_diagnosis_ids": [], "diagnosis_advisory": True,
        "selected_attempt": "initial", "steps": [], "status": "running", "stop_reason": None,
        "feedback": "Prepared source/context, abstraction policy, current TLR, and internal validation errors only.",
        "source_fidelity": "unassessed; diagnosis and recovery are LLM-reviewed, not engineer-approved"}
    selected, selected_dir = initial, directory
    def save():
        ledger["retained_ids"] = abstentions(selected)
        ledger["recovered_ids"] = [rid for rid in initial_ids if rid not in ledger["retained_ids"]]
        write_json(directory / "repair.json", ledger)
    if not initial_ids:
        ledger["stop_reason"] = "no_abstentions"
    elif not budget:
        ledger["stop_reason"] = "disabled_by_policy"
    elif initial["tlr"].get("abstraction_policy") != POLICY_VERSION:
        ledger["stop_reason"] = "legacy_profile_requires_explicit_conversion"
    elif compile_model and initial["compilation"].get("status") != "passed":
        ledger["stop_reason"] = "initial_compilation_failed"
    else:
        selected_dir = directory / "attempts" / "000"
        selected_dir.mkdir(parents=True)
        _copy_candidate(directory, selected_dir)
        write_json(selected_dir / "attempt.json", {"status": "initial", "accepted": True})
        ledger["selected_attempt"] = "attempts/000"
        failure = None
        save()
        for index in range(1, budget + 1):
            attempt_dir = directory / "attempts" / f"{index:03d}"
            attempt_dir.mkdir()
            step = {"index": index, "directory": str(attempt_dir.relative_to(directory)),
                    "diagnosis_status": "running", "proposal_status": "not_requested",
                    "eligible_ids": abstentions(selected), "diagnosis_recommended_ids": [],
                    "overridden_diagnosis_ids": [],
                    "compilation": {"status": "not_run"}, "analysis": {"status": "not_run"}}
            ledger["steps"].append(step)
            save()
            diagnosis = None
            try:
                prompt = diagnosis_prompt(sources, context, selected["tlr"], previous_failure=failure)
                ledger["diagnosis_calls"] += 1
                save()
                response = ask(DIAGNOSIS_INSTRUCTIONS, prompt, model, attempt_dir, "diagnosis_call")
                (attempt_dir / "diagnosis_response.txt").write_text(response, encoding="utf-8")
                (attempt_dir / "diagnosis_candidate.json").write_text(_strip_fence(response), encoding="utf-8")
                diagnosis = validate_diagnosis(read_json(attempt_dir / "diagnosis_candidate.json"), sources, selected["tlr"])
                write_json(attempt_dir / "diagnosis.json", diagnosis)
                step["diagnosis_status"] = "completed"
                step["diagnosis_recommended_ids"] = [row["id"] for row in diagnosis["requirements"]
                                                     if row["decision"] == "repair"]
            except Exception as exc:
                # Invalid advice is retained as evidence, never as authority or
                # a reason to suppress this round's bounded proposal attempt.
                diagnosis = None
                step.update(diagnosis_status="failed", diagnosis_error=f"{type(exc).__name__}: {exc}")
            step["proposal_status"] = "running"
            save()
            artifact_phase = False
            try:
                prompt = repair_prompt(sources, context, selected["tlr"], diagnosis, previous_failure=failure)
                ledger["repair_attempts"] += 1
                save()
                instructions = (REPAIR_INSTRUCTIONS +
                    "\nThe following instructions apply only to the nested 'tlr' member, "
                    "not to the response envelope:\n<tlr_schema_instructions>\n" + TLR_INSTRUCTIONS +
                    "\n</tlr_schema_instructions>\nReturn the outer abstention_proposal/1 envelope.")
                response = ask(instructions, prompt, model, attempt_dir, "repair_call")
                (attempt_dir / "repair_response.txt").write_text(response, encoding="utf-8")
                (attempt_dir / "proposal_candidate.json").write_text(_strip_fence(response), encoding="utf-8")
                raw = read_json(attempt_dir / "proposal_candidate.json")
                if isinstance(raw, dict) and "tlr" in raw:
                    write_json(attempt_dir / "candidate_tlr.json", raw["tlr"])
                proposal = validate_proposal(raw, sources, selected["tlr"])
                write_json(attempt_dir / "proposal.json", proposal)
                write_json(attempt_dir / "proposal_reviews.json", proposal["reviews"])
                proposed = _normalize_candidate(proposal["tlr"], sources, context, True)
                changes = validate_repair(selected["tlr"], proposed, diagnosis, fixed_context=context)
                step["changes"] = changes
                write_json(attempt_dir / "tlr.json", proposed)
                if not changes["progress"]:
                    step["proposal_status"] = "no_progress_after_proposal"
                    ledger["stop_reason"] = "no_progress_after_proposal"
                    write_json(attempt_dir / "attempt.json", step)
                    break
                artifact_phase = True
                artifacts = _materialize_candidate(attempt_dir, render_sysml(proposed, name), proposed,
                                                   condition, compile_model, solver, timeout)
                step.update(compilation=artifacts["compilation"], analysis=artifacts["analysis"])
                if compile_model and artifacts["compilation"].get("status") != "passed":
                    step["proposal_status"] = "rejected"
                    step["error"] = "Generated SysML did not compile; investigate the renderer before further semantic changes."
                    ledger["stop_reason"] = "proposal_compilation_failed"
                    write_json(attempt_dir / "attempt.json", step)
                    break
                # Acceptance depends on structural recovery and optional compilation,
                # never on SAT, judge scores, or a held-out mutation comparison.
                selected, selected_dir = artifacts, attempt_dir
                ledger["accepted_repairs"] += 1
                ledger["selected_attempt"] = str(attempt_dir.relative_to(directory))
                step["proposal_status"] = "accepted"
                if diagnosis is not None:
                    step["overridden_diagnosis_ids"] = [rid for rid in changes["recovered_ids"]
                                                        if rid not in step["diagnosis_recommended_ids"]]
                    ledger["overridden_diagnosis_ids"].extend(step["overridden_diagnosis_ids"])
                failure = None
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                step.update(proposal_status="rejected", error=error)
                # Keep complete errors on disk, but bounded prompt feedback must
                # not itself disable the next round's diagnosis/proposal calls.
                failure = error if len(error) <= 12000 else error[:11900] + " [truncated; full error in previous attempt.json]"
                if artifact_phase:
                    # Tool/renderer failures are recorded, never fed back as an
                    # invitation to change the source interpretation.
                    ledger["stop_reason"] = "candidate_check_failed"
                    write_json(attempt_dir / "attempt.json", step)
                    break
            write_json(attempt_dir / "attempt.json", step)
            if not abstentions(selected):
                ledger["stop_reason"] = "all_supported"
                break
            save()
        if selected_dir != directory:
            _copy_candidate(selected_dir, directory)
    ledger["status"] = "completed"
    if ledger["stop_reason"] is None:
        ledger["stop_reason"] = "budget_exhausted"
    save()
    return selected, ledger



def _refine_with_feedback(initial, sources, context, directory, budget, ask, model, name,
                          condition, compile_model, solver, timeout, development_scenarios=None):
    """Matched source review / Z3-informed review with no evaluator feedback."""
    from canonical_feedback import (FEEDBACK_POLICY_VERSION, FEEDBACK_INSTRUCTIONS,
                                    feedback_prompt, solver_feedback, validate_feedback_proposal,
                                    SCENARIO_POLICY_VERSION, SCENARIO_INSTRUCTIONS)
    from canonical_tlr import render_sysml
    mode = "solver" if condition == "C" else "source"
    selected, selected_dir = initial, directory
    ledger = {"schema": "semantic_feedback_repair/1", "policy": FEEDBACK_POLICY_VERSION,
        "feedback_mode": mode, "budget": budget, "max_additional_transport_invocations": budget,
        "repair_attempts": 0, "accepted_repairs": 0, "selected_attempt": "initial",
        "steps": [], "status": "running", "stop_reason": None,
        "source_fidelity": "LLM-reviewed proposals; independent fidelity assessment remains required.",
        "selection_policy": "Latest source-grounded structurally valid compiling revision; SAT is not a selection objective.",
        "excluded_feedback": ["independent judge verdicts", "held-out mutations", "reference formulas"]}
    def save():
        ledger["final_representation"] = selected.get("representation")
        write_json(directory / "feedback_repair.json", ledger)
    history = directory / "feedback_attempts"
    history.mkdir()
    selected_dir = history / "000"
    selected_dir.mkdir()
    _copy_candidate(directory, selected_dir)
    ledger["initial_representation"] = initial.get("representation")
    ledger["selected_attempt"] = "feedback_attempts/000"
    write_json(selected_dir / "attempt.json", {"status": "initial", "accepted": True})
    development_results, semantic_comparison = None, None
    if development_scenarios is not None:
        from canonical_scenarios import run_scenarios, compare_scenario_runs, compare_tlr_semantics
        ledger.update(policy=SCENARIO_POLICY_VERSION,
            feedback_mode="solver_and_development_scenarios",
            selection_policy="Source-grounded structurally valid compiling revision with no regression of passing development scenarios; no engineer approval implied.")
        development_results = run_scenarios(selected["tlr"], development_scenarios, sources,
            selected_dir / "development_scenarios", timeout_seconds=timeout, solver=solver)
        ledger["initial_development_results"] = development_results
    if development_results is not None and development_results.get("status") == "encoding_error":
        # Frozen meanings/background cannot be repaired by changing a candidate.
        # Preserve the initial candidate and diagnostics without spending a call.
        context_error = any(row.get("code") in {"definition_mismatch", "context_revision_required"}
                            for row in development_results.get("diagnostics", []))
        ledger["stop_reason"] = ("context_revision_required" if context_error
                                 else "development_scenario_encoding_error")
    elif compile_model and initial["compilation"].get("status") != "passed":
        ledger["stop_reason"] = "initial_compilation_failed"
    else:
        failure = None
        for index in range(1, budget + 1):
            attempt_dir = history / f"{index:03d}"
            attempt_dir.mkdir()
            step = {"index": index, "directory": str(attempt_dir.relative_to(directory)),
                    "based_on": str(selected_dir.relative_to(directory)), "status": "running",
                    "accepted": False}
            ledger["steps"].append(step)
            artifact_phase = False
            try:
                feedback = solver_feedback(selected["analysis"], selected_dir) if mode == "solver" else None
                if feedback is not None:
                    write_json(attempt_dir / "solver_feedback.json", feedback)
                prompt = feedback_prompt(sources, context, selected["tlr"], feedback, previous_failure=failure,
                    development_scenarios=development_scenarios, development_results=development_results,
                    previous_semantic_comparison=semantic_comparison)
                write_json(attempt_dir / "prompt.json", json.loads(prompt))
                instructions = (FEEDBACK_INSTRUCTIONS +
                    "\nThe following schema instructions apply to the nested tlr member:\n" + TLR_INSTRUCTIONS +
                    "\nReturn the outer semantic_repair_proposal/1 envelope, not a bare TLR.")
                if development_scenarios is not None:
                    instructions += "\n" + SCENARIO_INSTRUCTIONS
                ledger["repair_attempts"] += 1
                save()
                response = ask(instructions, prompt, model, attempt_dir, "feedback_call")
                (attempt_dir / "response.txt").write_text(response, encoding="utf-8")
                (attempt_dir / "proposal_candidate.json").write_text(_strip_fence(response), encoding="utf-8")
                raw = read_json(attempt_dir / "proposal_candidate.json")
                if isinstance(raw, dict) and "tlr" in raw:
                    write_json(attempt_dir / "candidate_tlr.json", raw["tlr"])
                proposal = validate_feedback_proposal(raw, sources, selected["tlr"], fixed_context=context)
                write_json(attempt_dir / "proposal.json", proposal)
                write_json(attempt_dir / "proposal_reviews.json", proposal["reviews"])
                proposed, changes = proposal["tlr"], proposal["changes"]
                step["changes"] = changes
                write_json(attempt_dir / "tlr.json", proposed)
                # Retain explicit before/after formulas/statuses for all semantic edits.
                before_by_id = {r["id"]: r for r in selected["tlr"]["requirements"]}
                after_by_id = {r["id"]: r for r in proposed["requirements"]}
                write_json(attempt_dir / "changes.json", {"summary": changes,
                    "requirements": [{"id": rid, "before": before_by_id[rid], "after": after_by_id[rid]}
                                     for rid in changes["changed_ids"]]})
                if not changes["progress"]:
                    step["status"] = "unchanged"
                    ledger["stop_reason"] = "no_change_after_review"
                else:
                    artifact_phase = True
                    artifacts = _materialize_candidate(attempt_dir, render_sysml(proposed, name), proposed,
                                                       condition, compile_model, solver, timeout)
                    step.update(compilation=artifacts["compilation"], analysis=artifacts["analysis"])
                    development_gate = None
                    if development_scenarios is not None:
                        candidate_results = run_scenarios(proposed, development_scenarios, sources,
                            attempt_dir / "development_scenarios", timeout_seconds=timeout, solver=solver)
                        development_gate = compare_scenario_runs(development_results, candidate_results)
                        semantic_comparison = compare_tlr_semantics(selected["tlr"], proposed,
                            attempt_dir / "semantic_comparison", timeout_seconds=timeout, solver=solver)
                        step.update(development_results=candidate_results, development_gate=development_gate,
                                    semantic_comparison=semantic_comparison)
                        write_json(attempt_dir / "development_gate.json", development_gate)
                    if compile_model and artifacts["compilation"].get("status") != "passed":
                        step.update(status="rejected", error="Generated SysML did not compile; investigate the renderer.")
                        ledger["stop_reason"] = "proposal_compilation_failed"
                    elif development_gate is not None and not development_gate["acceptable"]:
                        step.update(status="rejected", error="Development scenario regression or inconclusive candidate checks; previous candidate retained.")
                        # This is declared development feedback, not an evaluator score.
                        failure = json.dumps({"diagnosis": step["error"], "gate": development_gate,
                                              "candidate_results": candidate_results}, ensure_ascii=False)
                        if len(failure) > 12000:
                            failure = json.dumps({"diagnosis": step["error"], "gate": development_gate}, ensure_ascii=False)[:12000]
                    else:
                        selected, selected_dir = artifacts, attempt_dir
                        if development_scenarios is not None:
                            development_results = candidate_results
                        ledger["accepted_repairs"] += 1
                        ledger["selected_attempt"] = str(attempt_dir.relative_to(directory))
                        step.update(status="accepted", accepted=True)
                        failure = None
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                step.update(status="rejected", error=error)
                # Retry parse/schema/source guards under the same finite budget.
                # Infrastructure failures must not invite reinterpretation of source.
                failure = error if len(error) <= 12000 else error[:11900] + " [truncated; full error in attempt.json]"
                if artifact_phase or "prompt.json" not in {p.name for p in attempt_dir.iterdir()}:
                    ledger["stop_reason"] = "candidate_check_failed"
            write_json(attempt_dir / "attempt.json", step)
            save()
            if ledger["stop_reason"]:
                break
    _copy_candidate(selected_dir, directory)
    ledger["status"] = "completed"
    if ledger["stop_reason"] is None:
        ledger["stop_reason"] = "budget_exhausted"
    if development_scenarios is not None:
        ledger["final_development_results"] = development_results
    save()
    return selected, ledger


def run_candidate(sources, output_dir, condition="C", model=None, name="RequirementsModel", context=None,
                  tlr=None, sysml_text=None, compile_model=True, solver="z3", timeout_seconds=10.0,
                  generator=None, abstention_repairs=0, feedback_repairs=0, development_scenarios=None):
    """One initial generation plus an explicit bounded recovery policy; B never calls Z3."""
    _repair_budget(abstention_repairs)
    _repair_budget(feedback_repairs, "Feedback repair budget")
    if feedback_repairs and abstention_repairs:
        raise ValueError("Choose either abstention recovery or semantic feedback repair, not both")
    if feedback_repairs and condition not in {"B", "C"}:
        raise ValueError("Feedback repair requires separate B or C candidates; A/BC are not eligible")
    if development_scenarios is not None:
        if condition != "C" or not feedback_repairs:
            raise ValueError("Development scenarios require condition C and a positive feedback repair budget")
        from canonical_scenarios import validate_scenario_suite
        development_scenarios = validate_scenario_suite(development_scenarios, sources)
    if condition not in {"A", "B", "C", "BC"}:
        raise ValueError("Condition must be A, B, C or BC")
    if condition == "A" and abstention_repairs:
        raise ValueError("Abstention repair applies to TLR conditions B/C; A has no TLR")
    if tlr is not None and condition == "A" or sysml_text is not None and condition != "A":
        raise ValueError("A accepts only supplied SysML; B/C accept only supplied TLR")
    context = _fixed_context(context)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "sources.json", sources)
    if development_scenarios is not None:
        write_json(directory / "development_suite.json", development_scenarios)
    if context is not None:
        write_json(directory / "context.json", context)
    model = _model(model)
    offline = tlr is not None or sysml_text is not None
    configuration = {"schema": "canonical_execution/1", "condition": condition, "model": model,
                     "generation_mode": "supplied_artifact" if offline else "LLM",
                     "generation_attempts": 0 if offline else 1, "semantic_repairs": 0,
                     "abstention_repair_budget": abstention_repairs, "diagnosis_calls": 0, "repair_attempts": 0,
                     "max_model_transport_invocations": (0 if offline else 1) + 2 * abstention_repairs + feedback_repairs,
                     "feedback_repair_budget": feedback_repairs,
                     "feedback_mode": ("solver" if condition == "C" else "source") if feedback_repairs else "none",
                     "repair_policy": RECOVERY_POLICY_VERSION,
                     "profile": PROFILE, "compile_enabled": compile_model, "solver": solver,
                     "abstraction_policy": POLICY,
                     "solver_timeout_seconds": timeout_seconds, "fixed_context": context is not None,
                     "provider_settings": {"reasoning_effort": os.getenv("CODEX_REASONING_EFFORT", "low"),
                                           "timeout_seconds": os.getenv("CODEX_EXEC_TIMEOUT", "180"),
                                           "sampling": "Codex transport defaults; no temperature override"},
                     "created_at": datetime.now(timezone.utc).isoformat()}
    if feedback_repairs:
        from canonical_feedback import FEEDBACK_POLICY_VERSION
        configuration["repair_policy"] = FEEDBACK_POLICY_VERSION
    if development_scenarios is not None:
        from canonical_feedback import SCENARIO_POLICY_VERSION
        configuration.update(repair_policy=SCENARIO_POLICY_VERSION,
            feedback_mode="solver_and_development_scenarios",
            development_scenarios="development_suite.json",
            scenario_count=len(development_scenarios["scenarios"]))
    write_json(directory / "configuration.json", configuration)
    result = {"schema": "canonical_run/1", "condition": condition, "output_dir": str(directory.resolve()),
              "status": "failed", "tlr": None, "analysis": {"status": "not_run"},
              "compilation": {"status": "not_run"}, "admission": "not_assessed", "source_fidelity": "unassessed",
              "configuration": configuration, "errors": []}
    started = time.monotonic()
    try:
        prompt = _prompt(sources, context, name)
        ask = generator or _ask
        normalized = None
        if condition == "A":
            candidate = sysml_text if sysml_text is not None else ask(SYSML_INSTRUCTIONS, prompt, model, directory, "generation")
            candidate = _strip_fence(candidate)
            if not candidate:
                raise ValueError("Empty SysML generation")
        else:
            from canonical_tlr import render_sysml
            if tlr is None:
                response = ask(TLR_INSTRUCTIONS, prompt, model, directory, "generation")
                (directory / "generation_response.txt").write_text(response, encoding="utf-8")
                (directory / "candidate_tlr.json").write_text(_strip_fence(response), encoding="utf-8")
                tlr = read_json(directory / "candidate_tlr.json")
            else:
                write_json(directory / "candidate_tlr.json", tlr)
            normalized = _normalize_candidate(tlr, sources, context, not offline)
            candidate = render_sysml(normalized, name)
        result.update(_materialize_candidate(directory, candidate, normalized, condition, compile_model, solver, timeout_seconds))
        if normalized is not None and feedback_repairs:
            selected, ledger = _refine_with_feedback(deepcopy(result), sources, context, directory,
                feedback_repairs, ask, model, name, condition, compile_model, solver, timeout_seconds,
                development_scenarios=development_scenarios)
            for key in ("tlr", "representation", "model_file", "candidate_content", "compilation", "analysis", "admission"):
                result[key] = selected[key]
            result["feedback_repair"] = ledger
            configuration.update(semantic_repairs=ledger["accepted_repairs"], repair_attempts=ledger["repair_attempts"],
                                 repair_stop_reason=ledger["stop_reason"])
        elif normalized is not None:
            selected, ledger = _recover_abstentions(deepcopy(result), sources, context, directory,
                abstention_repairs, ask, model, name, condition, compile_model, solver, timeout_seconds)
            # Only candidate fields are replaced; top-level execution metadata survives.
            for key in ("tlr", "representation", "model_file", "candidate_content", "compilation", "analysis", "admission"):
                result[key] = selected[key]
            result["repair"] = ledger
            configuration.update(semantic_repairs=ledger["accepted_repairs"],
                                 diagnosis_calls=ledger["diagnosis_calls"], repair_attempts=ledger["repair_attempts"],
                                 repair_stop_reason=ledger["stop_reason"])
        result["status"] = "completed"
        if condition == "BC":
            result["arm_views"] = {
                "B": {"model_file": "model.sysml", "tlr_file": "tlr.json", "solver_checks": "not_run", "admission": "not_assessed"},
                "C": {"model_file": "model.sysml", "tlr_file": "tlr.json", "solver_checks": "audit/audit.json", "admission": result["admission"]}}
        result["limitations"] = ["Solver and compiler outcomes do not establish source fidelity or engineer approval.",
                                 "Only the declared static expression profile is executable; unsupported clauses remain visible.",
                                 ("No independent SysML read-back is performed; feedback revisions remain LLM-reviewed."
                                  if feedback_repairs else "No independent SysML read-back or solver-feedback correction is performed."),
                                 "Abstention recovery is LLM-reviewed and budgeted; increased coverage does not establish fidelity."]
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        result["admission"] = "withheld"
    result["latency_seconds"] = time.monotonic() - started
    write_json(directory / "configuration.json", configuration)
    write_json(directory / "result.json", result)
    return result


def _study_report(directory, rows):
    summary = {"planned_repetitions": len(rows), "unique_candidate_slots": 2 * len(rows),
               "generated_candidates": sum(bool(r[a].get("model_file")) for r in rows for a in ("A", "BC")),
               "generation_failures": sum(r[a]["status"] != "completed" for r in rows for a in ("A", "BC")),
               "compiled_candidates": sum(r[a]["compilation"].get("status") == "passed" for r in rows for a in ("A", "BC")),
               "C_admitted": sum(r["BC"]["admission"] == "admitted_consistent_encoding" for r in rows),
               "fidelity": "not_assessed_until_independent_judgments", "B_C_candidate_identity": "One shared model.sysml and tlr.json per repetition; no duplicate generation or hashing gate"}
    summary["candidates_without_requirement_constraints"] = sum(
        r[arm].get("candidate_content", {}).get("status") == "no_executable_requirement_content"
        for r in rows for arm in ("A", "BC"))
    summary["BC_representations"] = [r["BC"].get("representation") for r in rows]
    summary["BC_recovery"] = [r["BC"].get("repair") for r in rows]
    summary["accepted_repair_attempts"] = sum(r["BC"]["configuration"]["semantic_repairs"] for r in rows)
    summary["repair_attempts"] = sum(r["BC"]["configuration"]["repair_attempts"] for r in rows)
    summary["diagnosis_calls"] = sum(r["BC"]["configuration"]["diagnosis_calls"] for r in rows)
    payload = {"schema": "canonical_study/1", "status": "completed", "rows": rows, "summary": summary,
               "limitations": ["A/B compares generation routes; B/C holds candidate content fixed and varies audits.",
                   "Repeated outputs are not independent source requirements.",
                   "Optional B/C abstention recovery is recorded separately; independent preservation checking and human approval are not included."]}
    write_json(directory / "study.json", payload)
    lines = ["# A/B/C study", "", "B and C reference the same candidate. C adds diagnostics and admission only.", "",
             "| Repetition | A execution | A compilation | B/C execution | B/C compilation | C admission |",
             "|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['repetition']} | {row['A']['status']} | {row['A']['compilation']['status']} | {row['BC']['status']} | {row['BC']['compilation']['status']} | {row['BC']['admission']} |")
    lines += ["", "Execution completion and compilation do not establish formalization coverage.", "",
              "| Repetition | B/C generated constraints | State | Capability | Occurrence relation | Unsupported | Unresolved |", "|---|---:|---:|---:|---:|---:|---:|"]
    for row in rows:
        rep = row["BC"].get("representation")
        if rep:
            kinds, states = rep["by_kind"], rep["by_status"]
            lines.append(f"| {row['repetition']} | {rep['constraints_generated']} | {kinds['state_constraint']} | {kinds['capability']} | {kinds['event_relation']} | {states['unsupported']} | {states['unresolved']} |")
    lines += ["", "Capability predicates express declared availability. Occurrence relations constrain a modeled occurrence. Neither establishes implemented behavior or a temporal guarantee."]
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


def run_study(sources, output_dir, repetitions=5, model=None, context=None, tlr=None, a_sysml=None,
              compile_model=True, solver="z3", timeout_seconds=10.0, generator=None, abstention_repairs=0, feedback_repairs=0,
              development_scenarios=None):
    _repair_budget(abstention_repairs)
    _repair_budget(feedback_repairs, "Feedback repair budget")
    if development_scenarios is not None and not feedback_repairs:
        raise ValueError("Development scenarios require positive feedback repairs in the C branch")
    if feedback_repairs:
        if abstention_repairs:
            raise ValueError("Choose either abstention recovery or semantic feedback repair, not both")
        from canonical_feedback_study import run_feedback_study
        return run_feedback_study(sources, output_dir, repetitions=repetitions, model=model, context=context,
            tlr=tlr, a_sysml=a_sysml, compile_model=compile_model, solver=solver, timeout_seconds=timeout_seconds,
            generator=generator, feedback_repairs=feedback_repairs, development_scenarios=development_scenarios)
    if type(repetitions) is not int or not 1 <= repetitions <= 50:
        raise ValueError("Repetitions must be from 1 to 50")
    context = _fixed_context(context)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "sources.json", sources)
    rows = []
    model = _model(model)
    write_json(directory / "study_configuration.json", {"model": model, "repetitions": repetitions, "context": context,
        "conditions": ["A", "B", "C"], "shared_BC_candidate": True, "abstention_repair_budget": abstention_repairs,
        "repair_policy": RECOVERY_POLICY_VERSION, "semantic_repairs": 0,
        "semantic_repairs_scope": "Accepted structured recovery proposals; final actual counts are recorded after execution.",
        "max_additional_transport_invocations": 2 * repetitions * abstention_repairs,
        "planned_generation_calls": repetitions * ((a_sysml is None) + (tlr is None)),
        "order": "Alternate A then BC / BC then A across repetitions", "profile": PROFILE,
        "abstraction_policy": POLICY})
    for rep in range(1, repetitions + 1):
        row = {"repetition": rep}
        for arm in (("A", "BC") if rep % 2 else ("BC", "A")):
            print(f"Study repetition {rep}/{repetitions}: {arm}", file=sys.stderr)
            row[arm] = run_candidate(sources, directory / f"rep-{rep:03d}" / arm, arm, model=model, context=context,
                tlr=deepcopy(tlr) if arm == "BC" else None, sysml_text=a_sysml if arm == "A" else None,
                compile_model=compile_model, solver=solver, timeout_seconds=timeout_seconds, generator=generator,
                abstention_repairs=abstention_repairs if arm == "BC" else 0)
        rows.append(row)
        write_json(directory / "progress.json", {"completed_repetitions": len(rows), "planned": repetitions})
    report = _study_report(directory, rows)
    config = read_json(directory / "study_configuration.json")
    config.update(semantic_repairs=report["summary"]["accepted_repair_attempts"],
                  diagnosis_calls=report["summary"]["diagnosis_calls"], repair_attempts=report["summary"]["repair_attempts"])
    write_json(directory / "study_configuration.json", config)
    return report


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__, epilog="Run completion is not semantic validation or model approval. No hashes/reviewer certificates required. Legacy GUI requests: requirements_pipeline.py review --help.")
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "study"):
        sub = subs.add_parser(command)
        sub.add_argument("--statement", type=Path, required=True)
        sub.add_argument("--format", choices=("text", "csv", "json"))
        sub.add_argument("--output-dir", type=Path, help="New directory for plain artifacts; existing results are never overwritten")
        sub.add_argument("--model", help="Generation model; default from the existing Codex configuration")
        sub.add_argument("--context-file", type=Path, help="Optional fixed variables/background JSON with symbol_meanings for generation and comparison")
        sub.add_argument("--tlr-file", type=Path, help="Use a supplied executable TLR without a generation call")
        sub.add_argument("--skip-compile", action="store_true", help="Explicitly omit compiler checking; C cannot admit the result")
        sub.add_argument("--abstention-repairs", type=int, choices=range(6), default=None if command == "run" else 0,
                         help="Bounded TLR recovery attempts (0..5), up to twice as many extra model calls. Run defaults to 2 for generated B/C; studies and supplied fixtures default to 0.")
        sub.add_argument("--feedback-repairs", type=int, choices=range(6), default=0,
                         help="Matched semantic review rounds: B uses source, C adds Z3 evidence. Studies fork one initial TLR into separate B/C candidates. Cannot combine with abstention recovery.")
        sub.add_argument("--development-scenarios", type=Path,
                         help="Source-grounded development_scenarios/1 JSON for C feedback and regression gating. Requires positive --feedback-repairs; never supply final evaluation answers.")
        sub.add_argument("--solver", default="z3")
        sub.add_argument("--timeout-seconds", type=float, default=10)
        if command == "run":
            sub.add_argument("--condition", choices=("A", "B", "C", "BC"), default="C")
            sub.add_argument("--name", default="RequirementsModel")
            sub.add_argument("--sysml-file", type=Path, help="Supplied direct-generation candidate for condition A")
        else:
            sub.add_argument("--repetitions", type=int, default=5)
            sub.add_argument("--a-sysml-file", type=Path, help="Supplied A fixture, without a generation call")
    return parser


def main(argv=None):
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list or args_list[0].startswith("--") and args_list[0] not in {"--help", "-h"}:
        args_list.insert(0, "run")
    args = argument_parser().parse_args(args_list)
    try:
        if not 0 < args.timeout_seconds <= 3600:
            raise ValueError("Solver timeout must be positive and at most 3600 seconds")
        sources = sources_from_file(args.statement, args.format)
        context = read_json(args.context_file) if args.context_file else None
        tlr = read_json(args.tlr_file) if args.tlr_file else None
        output = args.output_dir or ROOT / "out" / ("canonical_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
        common = {"model": args.model, "context": context, "tlr": tlr, "compile_model": not args.skip_compile,
                  "solver": args.solver, "timeout_seconds": args.timeout_seconds, "feedback_repairs": args.feedback_repairs,
                  "development_scenarios": read_json(args.development_scenarios) if args.development_scenarios else None,
                  "abstention_repairs": (args.abstention_repairs if args.abstention_repairs is not None else
                      (2 if args.command == "run" and args.condition != "A" and tlr is None and not args.feedback_repairs else 0))}
        if args.command == "study":
            result = run_study(sources, output, args.repetitions,
                a_sysml=args.a_sysml_file.read_text(encoding="utf-8") if args.a_sysml_file else None, **common)
        else:
            result = run_candidate(sources, output, args.condition, name=args.name,
                sysml_text=args.sysml_file.read_text(encoding="utf-8") if args.sysml_file else None, **common)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["status"] == "completed" else 1
    except (ValueError, OSError, TypeError) as exc:
        print(f"Canonical CLI error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
