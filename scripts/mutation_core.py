"""Typed, static mutation comparisons under an explicit fixed background.

The AST is the current-state subset of review_behavior/1. Reusing its validator
and emitter does not add temporal semantics: only index zero is declared, and
next-state references, candidate transitions and implementation rules are absent.
No provider calls, repairs, generated assumptions, or source-fidelity claims occur.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

from review_behavior import validate_behavior, _emit, _number

SCHEMA = "mutation_comparison/1"


def _static_capacity():
    raw = os.environ.get("MBSE_STATIC_MAX_VARIABLES", "24")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("MBSE_STATIC_MAX_VARIABLES must be an integer from 1 to 128") from exc
    if not 1 <= value <= 128:
        raise ValueError("MBSE_STATIC_MAX_VARIABLES must be an integer from 1 to 128")
    return value


STATIC_MAX_VARIABLES = _static_capacity()


def _keys(value: Any, allowed: set[str], required: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object.")
    if set(value) - allowed or required - set(value):
        raise ValueError(f"{label} has missing or unknown fields.")


def _wrapper(variables: list[dict], background: list[dict], formula: Any = True) -> dict:
    return {"schema": "review_behavior/1", "horizon": 1,
            "step": {"value": "1", "unit": "s"},
            "variables": [{**deepcopy(v), "role": "input"} for v in variables],
            "initial": [], "transitions": [],
            "assumptions": [{**deepcopy(a), "scope": "initial"} for a in background],
            "properties": [{"id": "MutationTarget", "kind": "always",
                            "requirement_ids": ["Target"], "predicate": formula}]}


def validate_context(variables: Any, background: Any) -> dict:
    """Normalize bounded typed symbols and up to 40 explicit static assumptions.

    The process-level MBSE_STATIC_MAX_VARIABLES setting defaults to 24 and must
    be frozen with a study (maximum 128). It changes capacity, not AST semantics
    or the default capacity of the separate transition-model validator.

    Variable fields: name, type, optional numeric unit/bounds. Background fields:
    id, explanatory text, predicate. Unknown fields, parameter values, roles and
    next-state references are rejected. Numeric quantities use exact decimals.
    """
    if not isinstance(variables, list) or not 1 <= len(variables) <= STATIC_MAX_VARIABLES:
        raise ValueError(f"Provide 1 to {STATIC_MAX_VARIABLES} typed variables.")
    if not isinstance(background, list) or len(background) > 40:
        raise ValueError("Provide at most 40 explicit background assumptions.")
    for variable in variables:
        _keys(variable, {"name", "type", "unit", "bounds"}, {"name", "type"}, "Variable")
    for assumption in background:
        _keys(assumption, {"id", "text", "predicate"}, {"id", "text", "predicate"}, "Background")
    normalized = validate_behavior(_wrapper(variables, background), ["Target"], max_variables=STATIC_MAX_VARIABLES)
    return {"variables": [{k: v for k, v in row.items() if k != "role"}
                          for row in normalized["variables"]],
            "background": [{k: v for k, v in row.items() if k != "scope"}
                           for row in normalized["assumptions"]]}


def _context(context: Any) -> dict:
    _keys(context, {"variables", "background"}, {"variables", "background"}, "Context")
    return validate_context(context["variables"], context["background"])


def validate_formula(formula: Any, context: dict) -> Any:
    """Validate a Boolean formula; reject unsupported operators and dimensions."""
    context = _context(context)
    normalized = validate_behavior(_wrapper(context["variables"], context["background"], formula), ["Target"], max_variables=STATIC_MAX_VARIABLES)
    return normalized["properties"][0]["predicate"]


def emit_formula(ast: Any, context: dict) -> str:
    """Emit a validated static expression with deterministic v_<name>_0 symbols."""
    context = _context(context)
    normalized = validate_formula(ast, context)
    table = {v["name"]: {**v, "role": "input"} for v in context["variables"]}
    return _emit(normalized, 0, table)


def _json(path: Path, value: Any) -> str:
    text = json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)
    return text


def _base(context: dict) -> tuple[str, dict[str, str]]:
    lines = ["(set-logic QF_LIRA)", "(set-option :produce-models true)"]
    names = {}
    for variable in context["variables"]:
        name = "v_" + variable["name"] + "_0"
        names[name] = variable["name"]
        lines.append(f"(declare-const {name} {variable['type']})")
        for bound, value in variable.get("bounds", {}).items():
            op = ">=" if bound == "lower" else "<="
            lines.append(f"(assert ({op} {name} {_number(value)}))")
    for assumption in context["background"]:
        lines.append(f"(assert {emit_formula(assumption['predicate'], context)})")
    return "\n".join(lines) + "\n", names


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


def _execute(query: str, executable: str, timeout: float, version: dict) -> dict:
    started = time.monotonic()
    result = {"solver": "z3", "executable": executable, "solver_version": version,
              "timeout_seconds": timeout, "stdout": "", "stderr": "", "exit_code": None}
    try:
        process = subprocess.run([executable, "-in"], input=query, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=timeout, check=False)
        result.update(stdout=process.stdout, stderr=process.stderr, exit_code=process.returncode)
        tokens = [line.strip() for line in process.stdout.splitlines()
                  if line.strip() in {"sat", "unsat", "unknown"}]
        if (process.returncode != 0 or re.search(r"\(error\b", process.stdout, re.I)
                or re.search(r"(^|\n)\s*(?:\(error\b|error\b)", process.stderr, re.I) or len(tokens) != 1):
            result.update(status="solver_error", diagnostic="Solver failed, rejected the query, or returned no unique verdict.")
        else:
            result["status"] = tokens[0]
    except subprocess.TimeoutExpired as exc:
        result.update(status="timeout", stdout=_text(exc.stdout), stderr=_text(exc.stderr),
                      diagnostic="Solver process exceeded the recorded timeout.")
    except OSError as exc:
        result.update(status="solver_error", diagnostic=str(exc))
    result["latency_seconds"] = time.monotonic() - started
    return result


def _version(executable: str, timeout: float) -> dict:
    try:
        proc = subprocess.run([executable, "-version"], capture_output=True, text=True,
                              timeout=min(timeout, 10.0), check=False)
        return {"status": "ok" if proc.returncode == 0 else "solver_error",
                "stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode}
    except subprocess.TimeoutExpired as exc:
        return {"status": "timeout", "stdout": _text(exc.stdout), "stderr": _text(exc.stderr)}
    except OSError as exc:
        return {"status": "solver_error", "diagnostic": str(exc)}


def _values(stdout: str, names: dict[str, str]) -> dict[str, str]:
    """Read generated get-value output without evaluating or coercing exact rationals."""
    from requirements_pipeline import _parse_sexpr
    remainder = "\n".join(line for line in stdout.splitlines() if line.strip() != "sat")
    forms = _parse_sexpr(remainder)
    def display(value):
        return "(" + " ".join(display(x) for x in value) + ")" if isinstance(value, list) else str(value)
    if len(forms) != 1 or not isinstance(forms[0], list):
        raise ValueError("Unexpected get-value response.")
    values = {}
    for pair in forms[0]:
        if not isinstance(pair, list) or len(pair) != 2 or pair[0] not in names:
            raise ValueError("Unexpected witness binding.")
        values[names[pair[0]]] = display(pair[1])
    if len(values) != len(names):
        raise ValueError("Incomplete witness bindings.")
    return values


def _save_query(directory: Path, label: str, query: str, result: dict) -> dict:
    query_path, result_path = directory / f"{label}.smt2", directory / f"{label}.json"
    with query_path.open("x", encoding="utf-8") as handle:
        handle.write(query)
    _json(result_path, result)
    return {**result, "artifacts": {"query": query_path.name, "result": result_path.name}}


def _query(directory: Path, label: str, base: str, goal: str | None,
           names: dict[str, str], executable: str, timeout: float, version: dict) -> dict:
    query = base + (f"(assert {goal})\n" if goal else "") + "(check-sat)\n"
    result = _save_query(directory, label, query, _execute(query, executable, timeout, version))
    if result["status"] == "sat":
        witness_query = query + "(get-value (" + " ".join(names) + "))\n"
        witness_result = _execute(witness_query, executable, timeout, version)
        values = {}
        if witness_result["status"] == "sat":
            try:
                values = _values(witness_result["stdout"], names)
                witness_result["witness_status"] = "available"
            except (ValueError, TypeError, IndexError, KeyError) as exc:
                witness_result.update(witness_status="parse_error", witness_diagnostic=str(exc))
        else:
            witness_result["witness_status"] = "unavailable"
        witness_result = _save_query(directory, label + "_witness", witness_query, witness_result)
        result.update(witness=values, witness_evidence=witness_result,
                      witness_kind="static contract valuation; no implementation behavior is asserted")
    return result


def compare_formulas(context: dict, canonical: Any, candidate: Any, output_dir: str | Path,
                     timeout_seconds: float = 10.0, solver: str = "z3") -> dict:
    """Persist a background check and both implication directions using local Z3.

    An existing nonempty output directory is rejected. Validation errors become
    encoding_error results; solver inconclusiveness never implies equivalence.
    SAT in either direction proves a difference even if the other query fails.
    """
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("Mutation comparison output directory must be empty; evidence is never overwritten.")
    result = {"schema": SCHEMA, "status": "encoding_error", "classification": "inconclusive",
              "difference_detected": None,
              "scope": "Static current-state formulas under declared domains and explicit background assumptions only.",
              "limitations": ["Canonical means the recorded comparison reference, not established stakeholder intent.",
                              "No candidate transitions, other requirement guarantees, temporal semantics or SysML preservation checks are added."]}
    try:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be finite, positive and at most 3600.")
        if not isinstance(solver, str) or not solver.strip() or "\x00" in solver:
            raise ValueError("solver must be the local Z3 executable name or path.")
        normalized = _context(context)
        original = validate_formula(canonical, normalized)
        changed = validate_formula(candidate, normalized)
        base, names = _base(normalized)
        old, new = emit_formula(original, normalized), emit_formula(changed, normalized)
        inputs = {"context": normalized, "canonical": original, "candidate": changed}
        _json(directory / "inputs.json", inputs)
        result.update(inputs_artifact="inputs.json",
                      formulas={"canonical": old, "candidate": new}, timeout_seconds=timeout_seconds)
        version = _version(solver, timeout_seconds)
        result["solver_version"] = version
        feasible = _query(directory, "background", base, None, names, solver, timeout_seconds, version)
        result["background"] = feasible
        if feasible["status"] != "sat":
            result["status"] = "background_inconsistent" if feasible["status"] == "unsat" else "background_inconclusive"
        else:
            permitted = _query(directory, "newly_permitted", base, f"(and {new} (not {old}))", names, solver, timeout_seconds, version)
            forbidden = _query(directory, "newly_forbidden", base, f"(and {old} (not {new}))", names, solver, timeout_seconds, version)
            pair = permitted["status"], forbidden["status"]
            classification = {("sat", "unsat"): "weakened", ("unsat", "sat"): "strengthened",
                              ("sat", "sat"): "changed", ("unsat", "unsat"): "equivalent"}.get(pair, "inconclusive")
            detected = True if "sat" in pair else False if pair == ("unsat", "unsat") else None
            result.update(status="compared", classification=classification, difference_detected=detected,
                          newly_permitted=permitted, newly_forbidden=forbidden)
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError) as exc:
        result.update(status="encoding_error", diagnostic=str(exc))
    _json(directory / "comparison.json", result)
    return result


def check_consistency(context: dict, formula: Any, output_dir: str | Path,
                      timeout_seconds: float = 10.0, solver: str = "z3") -> dict:
    """Check Gamma AND formula, preserving evidence independently of equivalence.

    Callers can supply a conjunction of canonical requirements or a target
    replacement with frozen neighbors. This is an offline audit, not evidence
    that the runtime pipeline checked the same bundle. UNSAT alone does not
    distinguish an inconsistent background from conflicting requirements.
    """
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("Consistency output directory must be empty; evidence is never overwritten.")
    result = {"schema": "mutation_consistency/1", "status": "encoding_error",
              "scope": "Static formula conjunction with declared domains and explicit background assumptions."}
    try:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be finite, positive and at most 3600.")
        if not isinstance(solver, str) or not solver.strip() or "\x00" in solver:
            raise ValueError("solver must be the local Z3 executable name or path.")
        normalized = _context(context)
        normalized_formula = validate_formula(formula, normalized)
        expression = emit_formula(normalized_formula, normalized)
        _json(directory / "inputs.json", {"context": normalized, "formula": normalized_formula})
        base, names = _base(normalized)
        version = _version(solver, timeout_seconds)
        query_result = _query(directory, "satisfiability", base, expression,
                              names, solver, timeout_seconds, version)
        result.update(query_result, formula=expression, inputs_artifact="inputs.json")
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError) as exc:
        result.update(status="encoding_error", diagnostic=str(exc))
    _json(directory / "consistency.json", result)
    return result
