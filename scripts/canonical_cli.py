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
from mutation_core import STATIC_MAX_VARIABLES

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
TLR_INSTRUCTIONS = TLR_INSTRUCTIONS.replace("at most 24 variables", f"at most {STATIC_MAX_VARIABLES} variables")
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
        "abstraction_policy": POLICY, "requirements": sources, "fixed_context": context},
        ensure_ascii=False, indent=2)


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


def _review_ask(system, prompt, model, directory, call_id):
    """Separate inference call; never inherit a generator's returned verdict."""
    return _ask(system, prompt, model, directory, call_id)


def _recovery_budget(value, label):
    if type(value) is not int or value not in (0, 1):
        raise ValueError(f"{label} must be 0 or 1")


def _generate_initial_tlr(sources, context, directory, instructions, prompt, model, ask,
                          budget, configuration):
    """Correct only malformed generated drafts before normal conversion."""
    ledger = {"schema": "initial_tlr_format_recovery/1", "budget": budget,
              "correction_calls": 0, "attempts": [], "stop_reason": None}
    history = directory / "format_attempts"
    if budget:
        history.mkdir()
    previous = diagnostic = None
    try:
        for index in range(budget + 1):
            target = history / f"{index:03d}" if budget else directory
            if budget:
                target.mkdir()
            call_prompt, call_instructions = prompt, instructions
            if index:
                call_prompt = json.dumps({"original_request": json.loads(prompt),
                    "previous_response": previous, "validation_error": diagnostic}, ensure_ascii=False, indent=2)
                call_instructions += ("\nThis is a separately budgeted initial format/schema correction. "
                    "Return a complete corrected TLR for the unchanged original request. Correct the reported "
                    "JSON/schema/type/profile defect while preserving source meaning, every original requirement "
                    "ID, literal wording, context. Do not drop clauses, invent assumptions, "
                    "or weaken meaning to satisfy validation. No semantic review "
                    "or solver feedback is provided; semantic assessment remains separate.")
                configuration["format_repair_calls"] += 1
                ledger["correction_calls"] += 1
            else:
                configuration["generation_attempts"] = 1
            attempt = {"index": index, "status": "running"}
            ledger["attempts"].append(attempt)
            try:
                response = ask(call_instructions, call_prompt, model,
                    target if index else directory, "format_correction" if index else "generation")
            except Exception as exc:
                attempt.update(status="transport_error", error=f"{type(exc).__name__}: {exc}")
                ledger["stop_reason"] = "transport_error"
                raise
            previous = response
            if index == 0:
                (directory / "generation_response.txt").write_text(response, encoding="utf-8")
            if budget:
                (target / "response.txt").write_text(response, encoding="utf-8")
                write_json(target / "request.json", {"instructions": call_instructions, "prompt": json.loads(call_prompt)})
            candidate_path = directory / "candidate_tlr.json"
            candidate_path.write_text(_strip_fence(response), encoding="utf-8")
            if budget:
                (target / "candidate_tlr.json").write_text(_strip_fence(response), encoding="utf-8")
            try:
                normalized = _normalize_candidate(read_json(candidate_path), sources, context, True)
            except (ValueError, TypeError, KeyError) as exc:
                diagnostic = f"{type(exc).__name__}: {exc}"
                attempt.update(status="invalid_candidate", error=diagnostic)
                if budget:
                    write_json(target / "validation.json", attempt)
                if index < budget:
                    continue
                ledger["stop_reason"] = "budget_exhausted" if budget else "disabled"
                raise
            attempt["status"] = "validated"
            ledger["stop_reason"] = "validated"
            if budget:
                write_json(target / "validation.json", attempt)
            return normalized
    finally:
        write_json(directory / "format_recovery.json", ledger)


def _materialize_candidate(directory, candidate, tlr, condition, compile_model, solver, timeout):
    from canonical_sysml_screen import requirement_content
    result = {"model_file": "model.sysml", "tlr": tlr, "admission": "not_assessed", "source_fidelity": "not_assessed"}
    if tlr is not None:
        write_json(directory / "tlr.json", tlr)
        result["representation"] = representation_summary(tlr)
        emitted = [r["id"] for r in tlr["requirements"] if r["status"] == "supported"]
        result["representation"].update(constraints_emitted=len(emitted), emitted_requirement_ids=emitted)
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
            # Selecting an empty/partial attempt must not retain executable artifacts
            # from an older attempt. These are this run's generated files only.
            if dst.is_dir():
                shutil.rmtree(dst)
            elif dst.exists():
                dst.unlink()
            continue
        if src.is_dir():
            # Only this run's generated evidence is replaced. Attempt histories
            # and source packets are never overwritten by candidate selection.
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def _refine_with_feedback(initial, sources, context, directory, budget, ask, model, name,
                          condition, compile_model, solver, timeout, development_scenarios=None):
    """Matched source review / Z3-informed review with no evaluator feedback."""
    from canonical_feedback import (FEEDBACK_POLICY_VERSION, feedback_instructions,
                                    feedback_prompt, solver_feedback, validate_feedback_proposal,
                                    SCENARIO_POLICY_VERSION, scenario_instructions)
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
                    previous_semantic_comparison=semantic_comparison, allow_generated_context_repair=True)
                write_json(attempt_dir / "prompt.json", json.loads(prompt))
                instructions = (feedback_instructions(True) +
                    "\nThe following schema instructions apply to the nested tlr member:\n" + TLR_INSTRUCTIONS +
                    "\nReturn the outer semantic_repair_proposal/1 envelope, not a bare TLR.")
                if development_scenarios is not None:
                    instructions += "\n" + scenario_instructions(True)
                ledger["repair_attempts"] += 1
                save()
                response = ask(instructions, prompt, model, attempt_dir, "feedback_call")
                (attempt_dir / "response.txt").write_text(response, encoding="utf-8")
                (attempt_dir / "proposal_candidate.json").write_text(_strip_fence(response), encoding="utf-8")
                raw = read_json(attempt_dir / "proposal_candidate.json")
                if isinstance(raw, dict) and "tlr" in raw:
                    write_json(attempt_dir / "candidate_tlr.json", raw["tlr"])
                proposal = validate_feedback_proposal(raw, sources, selected["tlr"], fixed_context=context, allow_generated_context_repair=True)
                write_json(attempt_dir / "proposal.json", proposal)
                write_json(attempt_dir / "proposal_reviews.json", proposal["reviews"])
                write_json(attempt_dir / "context_reviews.json", proposal.get("context_reviews", []))
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


def _check_retired_options(options):
    neutral = {"reviewer": None, "source_review_model": None, "previous_source_review": None,
        "previous_inventory_review": None, "obligation_inventory": None,
        "require_obligation_inventory": False, "inventory_repairs": 0, "review_repairs": 0,
        "require_pattern_diagnostics": False}
    for key, value in options.items():
        if key not in neutral or value is not None and value != neutral[key]:
            raise ValueError(f"{key} belongs to the retired inventory/review gate. Use the source packet and --feedback-repairs; replay historical runs with their frozen implementation.")


def _feedback_budget(feedback, abstention=0):
    _repair_budget(feedback, "Feedback repair budget")
    _repair_budget(abstention, "Deprecated abstention budget")
    if feedback and abstention:
        raise ValueError("Use only --feedback-repairs; --abstention-repairs is its deprecated alias")
    return feedback or abstention


def run_candidate(sources, output_dir, condition="C", model=None, name="RequirementsModel", context=None,
                  tlr=None, sysml_text=None, compile_model=True, solver="z3", timeout_seconds=10.0,
                  generator=None, abstention_repairs=0, feedback_repairs=0, development_scenarios=None,
                  format_repairs=0, **retired_options):
    """Convert a fixed source packet; one source-grounded feedback controller.

    Supported typed rules are draft constraints, not independently approved rules.
    The old abstention budget aliases this same controller; no second loop runs.
    """
    _check_retired_options(retired_options)
    feedback_repairs = _feedback_budget(feedback_repairs, abstention_repairs)
    _recovery_budget(format_repairs, "Initial format correction budget")
    if condition not in {"A", "B", "C", "BC"}:
        raise ValueError("Condition must be A, B, C or BC")
    if feedback_repairs and condition not in {"B", "C"}:
        raise ValueError("Feedback repair requires separate B or C candidates; A/BC are not eligible")
    if development_scenarios is not None:
        if condition != "C" or not feedback_repairs:
            raise ValueError("Development scenarios require condition C and a positive feedback repair budget")
        from canonical_scenarios import validate_scenario_suite
        development_scenarios = validate_scenario_suite(development_scenarios, sources)
    if tlr is not None and condition == "A" or sysml_text is not None and condition != "A":
        raise ValueError("A accepts only supplied SysML; B/C accept only supplied TLR")
    context, model = _fixed_context(context), _model(model)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "sources.json", sources)
    if context is not None:
        write_json(directory / "context.json", context)
    if development_scenarios is not None:
        write_json(directory / "development_suite.json", development_scenarios)
    offline = tlr is not None or sysml_text is not None
    from canonical_feedback import FEEDBACK_POLICY_VERSION
    configuration = {"schema": "canonical_execution/2", "workflow_policy": "direct_tlr_feedback/1",
        "condition": condition, "model": model,
        "generation_mode": "supplied_artifact" if offline else "LLM", "generation_attempts": 0,
        "semantic_repairs": 0, "repair_attempts": 0, "diagnosis_calls": 0,
        "feedback_repair_budget": feedback_repairs, "abstention_repair_budget": 0,
        "deprecated_abstention_budget": abstention_repairs,
        "format_repair_budget": format_repairs, "format_repair_calls": 0,
        "max_format_repair_calls": format_repairs if not offline and condition != "A" else 0,
        "max_model_transport_invocations": int(not offline) + (format_repairs if not offline and condition != "A" else 0) + feedback_repairs,
        "feedback_mode": ("solver" if condition == "C" else "source") if feedback_repairs else "none",
        "repair_policy": FEEDBACK_POLICY_VERSION, "source_review_required": False,
        "source_review_mode": "embedded_in_feedback", "source_review_calls": 0, "source_review_reuses": 0,
        "inventory_calls": 0, "obligation_inventory_required": False,
        "profile": PROFILE, "compile_enabled": compile_model, "solver": solver,
        "static_max_variables": STATIC_MAX_VARIABLES, "abstraction_policy": POLICY,
        "solver_timeout_seconds": timeout_seconds, "fixed_context": context is not None,
        "generated_context_repair": "Source-grounded changes recorded in the same feedback proposal; no independent approval implied.",
        "provider_settings": {"reasoning_effort": os.getenv("CODEX_REASONING_EFFORT", "low"),
            "timeout_seconds": os.getenv("CODEX_EXEC_TIMEOUT", "180"),
            "sampling": "Codex transport defaults; no temperature override"},
        "created_at": datetime.now(timezone.utc).isoformat()}
    write_json(directory / "configuration.json", configuration)
    result = {"schema": "canonical_run/1", "condition": condition, "status": "failed",
        "output_dir": str(directory.resolve()), "configuration": configuration, "errors": [],
        "tlr": None, "model_file": None, "admission": "not_assessed",
        "analysis": {"status": "not_run"}, "compilation": {"status": "not_run"},
        "source_fidelity": "not_assessed"}
    started = time.monotonic()
    ask = generator or _ask
    try:
        prompt = _prompt(sources, context, name)
        normalized = None
        if condition == "A":
            configuration["generation_attempts"] = int(sysml_text is None)
            candidate = sysml_text if sysml_text is not None else ask(SYSML_INSTRUCTIONS, prompt, model, directory, "generation")
            candidate = _strip_fence(candidate)
            if not candidate:
                raise ValueError("Empty SysML generation")
        else:
            from canonical_tlr import render_sysml
            if tlr is None:
                normalized = _generate_initial_tlr(sources, context, directory, TLR_INSTRUCTIONS,
                    prompt, model, ask, format_repairs, configuration)
            else:
                write_json(directory / "candidate_tlr.json", tlr)
                normalized = _normalize_candidate(tlr, sources, context, False)
            candidate = render_sysml(normalized, name)
        result.update(_materialize_candidate(directory, candidate, normalized, condition,
            compile_model, solver, timeout_seconds))
        if normalized is not None and feedback_repairs:
            selected, ledger = _refine_with_feedback(deepcopy(result), sources, context, directory,
                feedback_repairs, ask, model, name, condition, compile_model, solver, timeout_seconds,
                development_scenarios=development_scenarios)
            for key in ("source_fidelity", "tlr", "representation", "model_file", "candidate_content", "compilation", "analysis", "admission"):
                result[key] = selected[key]
            result["feedback_repair"] = ledger
            configuration.update(semantic_repairs=ledger["accepted_repairs"], repair_attempts=ledger["repair_attempts"],
                                 repair_stop_reason=ledger["stop_reason"])
        result["status"] = "completed"
        if condition == "BC":
            result["arm_views"] = {
                "B": {"model_file": result.get("model_file"), "tlr_file": "tlr.json", "solver_checks": "not_run", "admission": "not_assessed"},
                "C": {"model_file": result.get("model_file"), "tlr_file": "tlr.json", "solver_checks": "audit/audit.json", "admission": result["admission"]}}
        result["limitations"] = [
            "Supported rules are draft interpretations. Solver/compiler success is not source fidelity or engineer approval.",
            "Source review is embedded in budgeted feedback; no mandatory separate inventory or eligibility gate runs.",
            "The source packet and supplied fixed context stay unchanged; generated interpretations may be revised with source-grounded reasons.",
            "Only the declared static profile is executable. Unsupported/unresolved source rows remain explicit.",
            "No independent SysML read-back or final-judge feedback is performed during conversion."]
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        result["admission"] = "withheld"
    configuration["model_transport_invocations"] = (configuration["generation_attempts"] +
        configuration["format_repair_calls"] + configuration["repair_attempts"])
    result["latency_seconds"] = time.monotonic() - started
    from canonical_assurance import conversion_assurance
    result["assurance"] = conversion_assurance(result, sources)
    write_json(directory / "assurance.json", result["assurance"])
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
    summary["BC_recovery"] = [r["BC"].get("feedback_repair") for r in rows]
    summary["accepted_repair_attempts"] = sum(r["BC"]["configuration"]["semantic_repairs"] for r in rows)
    summary["repair_attempts"] = sum(r["BC"]["configuration"]["repair_attempts"] for r in rows)
    summary["diagnosis_calls"] = sum(r["BC"]["configuration"]["diagnosis_calls"] for r in rows)
    payload = {"schema": "canonical_study/1", "status": "completed", "rows": rows, "summary": summary,
               "limitations": ["A/B compares generation routes; B/C holds candidate content fixed and varies audits.",
                   "Repeated outputs are not independent source requirements.",
                   "Conversion uses a single bounded feedback controller; independent preservation checking and human approval are not included."]}
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
              compile_model=True, solver="z3", timeout_seconds=10.0, generator=None,
              abstention_repairs=0, feedback_repairs=0, development_scenarios=None,
              format_repairs=0, **retired_options):
    _check_retired_options(retired_options)
    feedback_repairs = _feedback_budget(feedback_repairs, abstention_repairs)
    _recovery_budget(format_repairs, "Initial format correction budget")
    if type(repetitions) is not int or not 1 <= repetitions <= 50:
        raise ValueError("Repetitions must be from 1 to 50")
    if development_scenarios is not None and not feedback_repairs:
        raise ValueError("Development scenarios require positive feedback repairs in the C branch")
    if feedback_repairs:
        from canonical_feedback_study import run_feedback_study
        return run_feedback_study(sources, output_dir, repetitions=repetitions, model=model, context=context,
            tlr=tlr, a_sysml=a_sysml, compile_model=compile_model, solver=solver, timeout_seconds=timeout_seconds,
            generator=generator, feedback_repairs=feedback_repairs, development_scenarios=development_scenarios,
            format_repairs=format_repairs)
    context, model = _fixed_context(context), _model(model)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "sources.json", sources)
    config = {"workflow_policy": "direct_tlr_feedback/1", "model": model, "repetitions": repetitions,
        "context": context, "conditions": ["A", "B", "C"], "shared_BC_candidate": True,
        "feedback_repair_budget": 0, "abstention_repair_budget": 0, "format_repair_budget": format_repairs,
        "source_review_required": False, "source_review_mode": "embedded_in_feedback",
        "obligation_inventory_required": False, "profile": PROFILE, "abstraction_policy": POLICY,
        "static_max_variables": STATIC_MAX_VARIABLES,
        "planned_generation_calls": repetitions * (int(a_sysml is None) + int(tlr is None)),
        "max_model_transport_invocations": repetitions * (int(a_sysml is None) + int(tlr is None) * (1 + format_repairs))}
    write_json(directory / "study_configuration.json", config)
    rows = []
    for rep in range(1, repetitions + 1):
        row = {"repetition": rep}
        for arm in (("A", "BC") if rep % 2 else ("BC", "A")):
            print(f"Study repetition {rep}/{repetitions}: {arm}", file=sys.stderr)
            row[arm] = run_candidate(sources, directory / f"rep-{rep:03d}" / arm, arm,
                model=model, context=context, tlr=deepcopy(tlr) if arm == "BC" else None,
                sysml_text=a_sysml if arm == "A" else None, compile_model=compile_model,
                solver=solver, timeout_seconds=timeout_seconds, generator=generator, format_repairs=format_repairs)
        rows.append(row)
        write_json(directory / "progress.json", {"completed_repetitions": len(rows), "planned": repetitions})
    report = _study_report(directory, rows)
    config["actual_model_transport_invocations"] = sum(row[a]["configuration"]["model_transport_invocations"] for row in rows for a in ("A", "BC"))
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
        sub.add_argument("--tlr-file", type=Path, help="Use a supplied TLR without inference when the feedback budget is zero")
        sub.add_argument("--skip-compile", action="store_true", help="Explicitly omit compiler checking; C cannot admit the result")
        sub.add_argument("--abstention-repairs", type=int, choices=range(6), default=None if command == "run" else 0,
                         help="Deprecated alias for --feedback-repairs; invokes the same source/solver feedback controller.")
        sub.add_argument("--feedback-repairs", type=int, choices=range(6), default=None if command == "run" else 0,
                         help="Bounded candidate correction: B uses source feedback, C adds Z3 evidence. Generated standalone B/C default to 2 when neither repair option is supplied; studies, supplied artifacts and A/BC default to 0. Explicit 0 disables repair.")
        sub.add_argument("--development-scenarios", type=Path,
                         help="Source-grounded development_scenarios/1 JSON for C feedback and regression gating. Requires positive --feedback-repairs; never supply final evaluation answers.")
        sub.add_argument("--solver", default="z3")
        sub.add_argument("--timeout-seconds", type=float, default=10)
        sub.add_argument("--format-repairs", type=int, choices=(0, 1), default=0,
                         help="One optional initial generated TLR JSON/schema correction; default 0. Separate from semantic repair; supplied candidates are not rewritten.")
        if command == "run":
            sub.add_argument("--condition", choices=("A", "B", "C", "BC"), default="C")
            sub.add_argument("--name", default="RequirementsModel")
            sub.add_argument("--sysml-file", type=Path, help="Supplied direct-generation candidate for condition A")
        else:
            sub.add_argument("--repetitions", type=int, default=5)
            sub.add_argument("--a-sysml-file", type=Path, help="Supplied A fixture, without a generation call")
    judge = subs.add_parser("judge", help="Independent LLM judgments; no condition metadata or solver outcomes are sent")
    source = judge.add_mutually_exclusive_group(required=True)
    source.add_argument("--study-dir", type=Path)
    source.add_argument("--packets-file", type=Path, help="Explicit source/SysML packets, optionally with held-out calibration labels")
    provider = judge.add_mutually_exclusive_group(required=True)
    provider.add_argument("--judge-model", action="append", help="Legacy fidelity labels through Codex; supply twice")
    provider.add_argument("--judge-config", type=Path, help="bedrock_judges/1 configuration for assertion assessment")
    judge.add_argument("--assertions-file", type=Path, help="Frozen sysml_assertions/1, /2 or /3 suite; required for Bedrock judges")
    judge.add_argument("--output-dir", type=Path, required=True)
    judge.add_argument("--judge-workers", type=int, choices=(1, 2), default=1, help="Run the two Bedrock judges sequentially or concurrently; prompts and scoring stay identical")
    judge.add_argument("--recover-leading-brace", action="store_true",
                       help="Opt in before judging to receipt-checked removal of one surplus leading '{'; preserves raw replies and verdicts, makes no extra calls, and records recovered parses")
    rescore = subs.add_parser("rescore-judgments", help="Revalidate saved assertion judgments offline; no model calls")
    rescore.add_argument("--assessment-dir", type=Path, required=True, help="Saved assertion-assessment directory with frozen packets and raw responses")
    rescore.add_argument("--output-dir", type=Path, required=True, help="New directory; original results are preserved")
    rescore.add_argument("--recover-leading-brace", action="store_true",
                         help="Opt-in secondary offline correction of one surplus leading '{' with matching completed provider text; never edits verdicts or original evidence")
    repair = subs.add_parser("repair-judgments", help="Bounded evidence correction for unreviewed assertions using their original judges; freezes valid judgments and candidates")
    repair.add_argument("--assessment-dir", type=Path, required=True,
                        help="Saved assertion assessment, including its original frozen provider configuration")
    repair.add_argument("--output-dir", type=Path,
                        help="New directory preserving original results; required unless --dry-run")
    repair.add_argument("--max-attempts", type=int, choices=(1, 2), default=2,
                        help="Maximum follow-up calls per affected judge/requirement cell; stop at the first usable verdict")
    repair.add_argument("--judge-workers", type=int, choices=(1, 2), default=2)
    repair.add_argument("--continue-budget", action="store_true",
                        help="Explicitly continue a completed correction campaign for remaining gaps, retaining history and enforcing a cumulative four-attempt cap per cell")
    repair.add_argument("--dry-run", action="store_true",
                        help="Validate saved inputs and print the affected-call plan without creating a provider client or making calls")
    prepare = subs.add_parser("prepare-assertions", help="Draft a source-only assertion suite through Bedrock; no candidate is read")
    prepared_source = prepare.add_mutually_exclusive_group(required=True)
    prepared_source.add_argument("--statement", type=Path)
    prepared_source.add_argument("--study-dir", type=Path, help="Read only the study's source packet and fixed context")
    prepare.add_argument("--format", choices=("text", "csv", "json"))
    prepare.add_argument("--context-file", type=Path, help="Fixed context for --statement; not allowed with --study-dir")
    prepare.add_argument("--judge-config", type=Path, required=True, help="Bedrock configuration including assertion_author")
    prepare.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv=None):
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list or args_list[0].startswith("--") and args_list[0] not in {"--help", "-h"}:
        args_list.insert(0, "run")
    args = argument_parser().parse_args(args_list)
    try:
        if args.command == "repair-judgments":
            from canonical_assertion_repair import prepare_repair_plan, repair_assessment
            if args.dry_run:
                plan = prepare_repair_plan(args.assessment_dir, continue_budget=args.continue_budget)
                print(json.dumps({"plan": plan, "max_attempts": args.max_attempts,
                                  "judge_workers": args.judge_workers, "dry_run": True},
                                 indent=2, ensure_ascii=False))
                return 0
            if args.output_dir is None:
                raise ValueError("repair-judgments requires --output-dir unless --dry-run")
            from bedrock_judging import BedrockTransport
            configuration = read_json(args.assessment_dir / "configuration.json").get("provider")
            if not isinstance(configuration, dict):
                raise ValueError("Saved assessment lacks a frozen Bedrock provider configuration")
            transport = BedrockTransport(configuration)
            result = repair_assessment(args.assessment_dir, args.output_dir,
                                       [transport.for_judge(i) for i in (0, 1)], transport.config,
                                       max_attempts=args.max_attempts, workers=args.judge_workers,
                                       continue_budget=args.continue_budget)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["status"] == "completed" else 1
        if args.command == "rescore-judgments":
            from canonical_assertion_rescore import rescore_assessment
            result = rescore_assessment(args.assessment_dir, args.output_dir,
                                        recover_leading_brace=args.recover_leading_brace)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["status"] == "completed" else 1
        if args.command == "prepare-assertions":
            from bedrock_judging import BedrockTransport
            from canonical_assertion_judging import prepare_assertions
            if args.study_dir:
                if args.context_file or args.format:
                    raise ValueError("--study-dir uses its saved source/context; omit --context-file and --format")
                sources = read_json(args.study_dir / "sources.json")
                context = read_json(args.study_dir / "study_configuration.json").get("context")
            else:
                sources = sources_from_file(args.statement, args.format)
                context = read_json(args.context_file) if args.context_file else None
            transport = BedrockTransport(read_json(args.judge_config))
            callback = transport.for_assertion_author()
            result = prepare_assertions(sources, context, transport.config["assertion_author"]["model"],
                                        args.output_dir, callback, transport.config)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result["status"] == "completed" else 1
        if args.command == "judge":
            if args.judge_config:
                from bedrock_judging import BedrockTransport
                from canonical_assertion_judging import evaluate_packets, evaluate_study
                if not args.assertions_file:
                    raise ValueError("Bedrock assertion assessment requires --assertions-file; use prepare-assertions or a supplied suite")
                suite = read_json(args.assertions_file)
                transport = BedrockTransport(read_json(args.judge_config))
                models = [c["model"] for c in transport.judge_configs]
                callbacks = [transport.for_judge(i) for i in (0, 1)]
                if args.study_dir:
                    result = evaluate_study(args.study_dir, suite, models, args.output_dir, callbacks, transport.config,
                        workers=args.judge_workers, recover_leading_brace=args.recover_leading_brace)
                else:
                    packets = read_json(args.packets_file)
                    if not isinstance(packets, list) or not packets:
                        raise ValueError("Supply at least one assertion assessment packet")
                    result = evaluate_packets(packets, suite, models, args.output_dir, callbacks, transport.config,
                        workers=args.judge_workers, recover_leading_brace=args.recover_leading_brace)
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return 0 if result["status"] == "completed" else 1
            from canonical_judging import judge_packets, judge_study
            if args.recover_leading_brace:
                raise ValueError("--recover-leading-brace requires --judge-config for receipt-checked assertion assessment")
            if args.judge_workers != 1:
                raise ValueError("--judge-workers requires --judge-config")
            if args.assertions_file:
                raise ValueError("--assertions-file requires --judge-config for Bedrock assertion assessment")
            if len(args.judge_model) != 2:
                raise ValueError("Supply --judge-model twice")
            result = (judge_study(args.study_dir, args.judge_model, args.output_dir) if args.study_dir else
                      judge_packets(read_json(args.packets_file), args.judge_model, args.output_dir))
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if not 0 < args.timeout_seconds <= 3600:
            raise ValueError("Solver timeout must be positive and at most 3600 seconds")
        sources = sources_from_file(args.statement, args.format)
        context = read_json(args.context_file) if args.context_file else None
        tlr = read_json(args.tlr_file) if args.tlr_file else None
        feedback_repairs = args.feedback_repairs
        if feedback_repairs is None:
            feedback_repairs = (2 if args.command == "run" and args.condition in {"B", "C"}
                                and tlr is None and args.abstention_repairs is None else 0)
        output = args.output_dir or ROOT / "out" / ("canonical_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
        common = {"model": args.model, "context": context, "tlr": tlr, "compile_model": not args.skip_compile,
                  "format_repairs": args.format_repairs,
                  "solver": args.solver, "timeout_seconds": args.timeout_seconds, "feedback_repairs": feedback_repairs,
                  "development_scenarios": read_json(args.development_scenarios) if args.development_scenarios else None,
                  "abstention_repairs": args.abstention_repairs if args.abstention_repairs is not None else 0}
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
