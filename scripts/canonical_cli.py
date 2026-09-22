#!/usr/bin/env python3
"""LLM requirements -> executable TLR -> shared SMT/SysML, with A/B/C study support."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PROFILE = "Static Bool/Int/Real constraints, exact decimal quantities and canonical units; conditional obligations and linear arithmetic. Temporal, probabilistic, quantified or unresolved meaning must remain explicitly unsupported."
TLR_INSTRUCTIONS = '''Return a single JSON object, schema "mbse_tlr/1". Required keys:
variables: [{name, type: "Bool"|"Int"|"Real", unit?: string, bounds?: {lower?: exact_decimal_string, upper?: exact_decimal_string}, description?: string}].
assumptions: [{id, text, predicate: Boolean_AST}].
requirements: [{id: exact_source_ID, status: "supported"|"unsupported"|"unresolved", formula: Boolean_AST when supported, reason: explanatory_string when not supported}].
AST forms: true/false; {"var":"declared_name"}; {"value":"exact_decimal_string","unit":"V"}; {"op":"operator","args":[AST,...]}.
Operators: and/or (2..16 Boolean args), not (1), implies (2), ite (condition,then,else), =/!= and </<=/>/>= (2 compatible operands), + (2), - (1 or 2), * (2 with a fixed dimensionless literal factor). No raw SMT/code, floats, quantifiers, next-state references or nonlinear products. Comparison/addition units must agree. Numeric units include 1,s,ms,min,V,mV,A,mA,W,kW,J,kJ,Wh,kWh,m,cm,mm,km,kg,g,%,K. Use at most 24 variables and 40 background assumptions; retain unsupported source clauses explicitly if outside the profile. Empty variables are allowed when all clauses are unsupported.
Every source ID must appear exactly once. Reuse the same variable for the same physical quantity across clauses. Preserve conditions, modalities, endpoints, units and exceptions. Never replace a requirement with an unexplained Boolean like R1_holds or a truth constant. Keep source obligations in requirements, not in background assumptions or variable bounds. Disclose genuine background assumptions in assumptions; do not invent premises to make a result satisfiable. With supplied fixed vocabulary/background, use precisely that context and introduce no further symbols or assumptions. Missing context is unresolved, not permission to invent meaning.
Example expression for an obligation requiring motion disabled when maintenance is enabled:
{"op":"implies","args":[{"var":"maintenance"},{"op":"not","args":[{"var":"motion_enabled"}]}]}.
Do not return SysML, solver outcomes, approvals, hashes, or provenance certificates.
'''
SYSML_INSTRUCTIONS = '''Return only SysML v2 textual syntax for the supplied source requirements. Use one package with ScalarValues, a shared subject part definition with Boolean/Integer/Real attributes, and one requirement usage per source ID with actual require constraints for supported obligations. Retain exact source text in documentation. Numeric attributes denote canonical magnitudes; document their units. Preserve conditions, inclusive/exclusive bounds, quantity identities and genuine environmental assumptions. Unsupported temporal, probabilistic or otherwise unrepresentable clauses must remain explicitly documented as unsupported; do not invent Boolean placeholders or extra architecture. Use the supplied fixed vocabulary/background if present. Do not output TLR, SMT, explanations, approvals, hashes or invented verification results.'''


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
    if not isinstance(raw, dict) or set(raw) != {"variables", "background"}:
        raise ValueError("Context must contain variables and background")
    return validate_context(raw["variables"], raw["background"])


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
                       "requirements": [{k: r[k] for k in ("id", "text", "source") if k in r} for r in sources],
                       "fixed_context": context}, ensure_ascii=False, indent=2)


def _compile(path, enabled):
    if not enabled:
        return {"status": "not_run", "reason": "Explicit --skip-compile; no admission claim"}
    from review_sysml import compile_sysml
    return compile_sysml(path)


def run_candidate(sources, output_dir, condition="C", model=None, name="RequirementsModel", context=None,
                  tlr=None, sysml_text=None, compile_model=True, solver="z3", timeout_seconds=10.0, generator=None):
    """One generation attempt. BC shares the exact candidate; B never invokes Z3."""
    if condition not in {"A", "B", "C", "BC"}:
        raise ValueError("Condition must be A, B, C or BC")
    if tlr is not None and condition == "A" or sysml_text is not None and condition != "A":
        raise ValueError("A accepts only supplied SysML; B/C accept only supplied TLR")
    context = _fixed_context(context)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "sources.json", sources)
    if context is not None:
        write_json(directory / "context.json", context)
    model = _model(model)
    offline = tlr is not None or sysml_text is not None
    configuration = {"schema": "canonical_execution/1", "condition": condition, "model": model,
                     "generation_mode": "supplied_artifact" if offline else "LLM",
                     "generation_attempts": 0 if offline else 1, "semantic_repairs": 0,
                     "profile": PROFILE, "compile_enabled": compile_model, "solver": solver,
                     "solver_timeout_seconds": timeout_seconds, "fixed_context": context is not None,
                     "provider_settings": {"reasoning_effort": os.getenv("CODEX_REASONING_EFFORT", "low"),
                                           "timeout_seconds": os.getenv("CODEX_EXEC_TIMEOUT", "180"),
                                           "sampling": "Codex transport defaults; no temperature override"},
                     "created_at": datetime.now(timezone.utc).isoformat()}
    write_json(directory / "configuration.json", configuration)
    result = {"schema": "canonical_run/1", "condition": condition, "output_dir": str(directory.resolve()),
              "status": "failed", "tlr": None, "analysis": {"status": "not_run"},
              "compilation": {"status": "not_run"}, "admission": "not_assessed", "source_fidelity": "unassessed",
              "configuration": configuration, "errors": []}
    started = time.monotonic()
    try:
        prompt = _prompt(sources, context, name)
        ask = generator or _ask
        if condition == "A":
            candidate = sysml_text if sysml_text is not None else ask(SYSML_INSTRUCTIONS, prompt, model, directory, "generation")
            candidate = _strip_fence(candidate)
            if not candidate:
                raise ValueError("Empty SysML generation")
        else:
            from canonical_tlr import render_sysml, tlr_context, validate_tlr
            if tlr is None:
                response = ask(TLR_INSTRUCTIONS, prompt, model, directory, "generation")
                (directory / "generation_response.txt").write_text(response, encoding="utf-8")
                # Strict JSON parsing preserves malformed output as a generation failure.
                (directory / "candidate_tlr.json").write_text(_strip_fence(response), encoding="utf-8")
                tlr = read_json(directory / "candidate_tlr.json")
            else:
                write_json(directory / "candidate_tlr.json", tlr)
            normalized = validate_tlr(tlr, sources)
            if context is not None and not _context_equal(tlr_context(normalized), context):
                raise ValueError("Generated vocabulary/background differs from the fixed study context")
            result["tlr"] = normalized
            write_json(directory / "tlr.json", normalized)
            candidate = render_sysml(normalized, name)
        path = directory / "model.sysml"
        path.write_text(candidate, encoding="utf-8")
        result["model_file"] = "model.sysml"
        result["compilation"] = _compile(path, compile_model)
        write_json(directory / "compilation.json", result["compilation"])
        if condition in {"C", "BC"}:
            from canonical_audits import audit_tlr
            result["analysis"] = audit_tlr(result["tlr"], directory / "audit", timeout_seconds, solver)
            allowed = result["analysis"].get("admitted", False) and result["compilation"].get("status") == "passed"
            result["admission"] = "admitted_consistent_encoding" if allowed else "withheld"
        else:
            result["analysis"] = {"status": "not_run", "reason": "Condition does not execute solver checks"}
        write_json(directory / "analysis.json", result["analysis"])
        result["status"] = "completed"
        if condition == "BC":
            result["arm_views"] = {
                "B": {"model_file": "model.sysml", "tlr_file": "tlr.json", "solver_checks": "not_run", "admission": "not_assessed"},
                "C": {"model_file": "model.sysml", "tlr_file": "tlr.json", "solver_checks": "audit/audit.json", "admission": result["admission"]}}
        result["limitations"] = ["Solver and compiler outcomes do not establish source fidelity or engineer approval.",
                                 "Only the declared static expression profile is executable; unsupported clauses remain visible.",
                                 "No independent SysML read-back or semantic correction is performed."]
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        result["admission"] = "withheld"
    result["latency_seconds"] = time.monotonic() - started
    write_json(directory / "result.json", result)
    return result


def _study_report(directory, rows):
    summary = {"planned_repetitions": len(rows), "unique_candidate_slots": 2 * len(rows),
               "generated_candidates": sum(bool(r[a].get("model_file")) for r in rows for a in ("A", "BC")),
               "generation_failures": sum(r[a]["status"] != "completed" for r in rows for a in ("A", "BC")),
               "compiled_candidates": sum(r[a]["compilation"].get("status") == "passed" for r in rows for a in ("A", "BC")),
               "C_admitted": sum(r["BC"]["admission"] == "admitted_consistent_encoding" for r in rows),
               "fidelity": "not_assessed_until_independent_judgments", "B_C_candidate_identity": "One shared model.sysml and tlr.json per repetition; no duplicate generation or hashing gate"}
    payload = {"schema": "canonical_study/1", "status": "completed", "rows": rows, "summary": summary,
               "limitations": ["A/B compares generation routes; B/C holds candidate content fixed and varies audits.",
                   "Repeated outputs are not independent source requirements.",
                   "No automatic correction, independent preservation checking, or human approval is included."]}
    write_json(directory / "study.json", payload)
    lines = ["# A/B/C study", "", "B and C reference the same candidate. C adds diagnostics and admission only.", "",
             "| Repetition | A execution | A compilation | B/C execution | B/C compilation | C admission |",
             "|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['repetition']} | {row['A']['status']} | {row['A']['compilation']['status']} | {row['BC']['status']} | {row['BC']['compilation']['status']} | {row['BC']['admission']} |")
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


def run_study(sources, output_dir, repetitions=5, model=None, context=None, tlr=None, a_sysml=None,
              compile_model=True, solver="z3", timeout_seconds=10.0, generator=None):
    if type(repetitions) is not int or not 1 <= repetitions <= 50:
        raise ValueError("Repetitions must be from 1 to 50")
    context = _fixed_context(context)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "sources.json", sources)
    rows = []
    model = _model(model)
    write_json(directory / "study_configuration.json", {"model": model, "repetitions": repetitions, "context": context,
        "conditions": ["A", "B", "C"], "shared_BC_candidate": True, "semantic_repairs": 0,
        "planned_generation_calls": repetitions * ((a_sysml is None) + (tlr is None)),
        "order": "Alternate A then BC / BC then A across repetitions", "profile": PROFILE})
    for rep in range(1, repetitions + 1):
        row = {"repetition": rep}
        for arm in (("A", "BC") if rep % 2 else ("BC", "A")):
            print(f"Study repetition {rep}/{repetitions}: {arm}", file=sys.stderr)
            row[arm] = run_candidate(sources, directory / f"rep-{rep:03d}" / arm, arm, model=model, context=context,
                tlr=deepcopy(tlr) if arm == "BC" else None, sysml_text=a_sysml if arm == "A" else None,
                compile_model=compile_model, solver=solver, timeout_seconds=timeout_seconds, generator=generator)
        rows.append(row)
        write_json(directory / "progress.json", {"completed_repetitions": len(rows), "planned": repetitions})
    return _study_report(directory, rows)


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__, epilog="Run completion is not semantic validation or model approval. No hashes/reviewer certificates required. Legacy GUI requests: requirements_pipeline.py review --help.")
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "study"):
        sub = subs.add_parser(command)
        sub.add_argument("--statement", type=Path, required=True)
        sub.add_argument("--format", choices=("text", "csv", "json"))
        sub.add_argument("--output-dir", type=Path, help="New directory for plain artifacts; existing results are never overwritten")
        sub.add_argument("--model", help="Generation model; default from the existing Codex configuration")
        sub.add_argument("--context-file", type=Path, help="Optional fixed variables/background JSON for generation and comparison")
        sub.add_argument("--tlr-file", type=Path, help="Use a supplied executable TLR without a generation call")
        sub.add_argument("--skip-compile", action="store_true", help="Explicitly omit compiler checking; C cannot admit the result")
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
                  "solver": args.solver, "timeout_seconds": args.timeout_seconds}
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
