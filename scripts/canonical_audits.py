"""Source-linked, static Z3 audits of an authoritative ``mbse_tlr/1``.

These queries diagnose the encoded specification under its declared background.
They do not verify a design, judge source fidelity, or repair a requirement.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import mutation_core as solver_core

SCHEMA = "canonical_audits/1"
INCONCLUSIVE = {"unknown", "timeout", "solver_error", "encoding_error"}
CHECK_NAMES = ("trigger_reachability", "in_model_trigger", "violatability",
               "redundancy_context", "redundancy")


def _write(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")


def _and(expressions: list[str]) -> str:
    if not expressions:
        return "true"
    if len(expressions) == 1:
        return expressions[0]
    return "(and " + " ".join(expressions) + ")"


def _skip(status: str, reason: str, requirement_ids: list[str]) -> dict:
    return {"status": status, "reason": reason, "requirement_ids": requirement_ids}


def audit_tlr(tlr: dict, output_dir: str | Path, timeout_seconds: float = 10,
              solver: str = "z3") -> dict:
    """Audit Gamma, the full supported conjunction, and individual obligations.

    Every executed check preserves its exact SMT query and result. A SAT witness
    is a possible static valuation, not an observed or synthesized design trace.
    ``admitted`` means only that all source clauses are supported and Gamma/M
    are SAT; compilation and semantic review remain separate. Unreachable
    triggers and redundancy are findings, not automatic admission vetoes.
    The output directory must be empty; evidence is never overwritten.
    """
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("Canonical audit output directory must be empty.")
    result = {"schema": SCHEMA, "status": "inconclusive", "admitted": False,
              "background_status": "not_run", "consistency_status": "not_run",
              "requirements": [], "findings": [], "inconclusive_checks": [],
              "scope": "Static encoded requirements under declared variable domains and explicit environmental assumptions.",
              "limitations": ["Solver results do not establish source fidelity or stakeholder intent.",
                              "A violating valuation is not a counterexample to an independently supplied design.",
                              "Admission does not establish compiler acceptance or engineer approval."]}
    try:
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 3600):
            raise ValueError("timeout_seconds must be finite, positive and at most 3600.")
        if not isinstance(solver, str) or not solver.strip() or "\x00" in solver:
            raise ValueError("solver must be a local Z3 executable name or path.")
        from canonical_tlr import validate_tlr, tlr_context
        normalized = validate_tlr(tlr)
        _write(directory / "tlr.json", normalized)
        rows = normalized["requirements"]
        supported = [row for row in rows if row["status"] == "supported"]
        source_ids = [row["id"] for row in rows]
        supported_ids = [row["id"] for row in supported]
        result.update(requirement_ids=source_ids, supported_requirement_ids=supported_ids,
                      unsupported_requirement_ids=[row["id"] for row in rows if row["status"] != "supported"],
                      assumption_ids=[row["id"] for row in normalized["assumptions"]])
        # No executable formula exists in a context with no variables. Do not
        # introduce a dummy symbol, or treat an empty conjunction as coverage.
        if not normalized["variables"]:
            reason = "No executable requirement or symbolic context is available."
            result.update(status="unsupported", background_status="not_run", consistency_status="unsupported",
                          background=_skip("not_run", reason, []),
                          consistency=_skip("unsupported", reason, source_ids))
            for row in rows:
                result["requirements"].append({"id": row["id"], "status": row["status"],
                    "checks": {name: _skip("unsupported", row.get("reason", reason), [row["id"]])
                               for name in CHECK_NAMES}})
            _write(directory / "audit.json", result)
            return result

        context = tlr_context(normalized)
        base, names = solver_core._base(context)
        version = solver_core._version(solver, timeout_seconds)
        formulas = {row["id"]: solver_core.emit_formula(row["formula"], context) for row in supported}

        def run(label: str, goal: str | None, requirement_ids: list[str], purpose: str) -> dict:
            query = base + (f"(assert {goal})\n" if goal is not None else "") + "(check-sat)\n"
            evidence = solver_core._execute(query, solver, timeout_seconds, version)
            evidence.pop("query_sha256", None)
            evidence.update(requirement_ids=requirement_ids,
                            assumption_ids=result["assumption_ids"], purpose=purpose)
            evidence["artifacts"] = {"query": label + ".smt2", "result": label + ".json"}
            with (directory / (label + ".smt2")).open("x", encoding="utf-8") as handle:
                handle.write(query)
            if evidence["status"] == "sat":
                witness_query = query + "(get-value (" + " ".join(names) + "))\n"
                witness = solver_core._execute(witness_query, solver, timeout_seconds, version)
                witness.pop("query_sha256", None)
                witness.update(requirement_ids=requirement_ids,
                               assumption_ids=result["assumption_ids"], purpose=purpose)
                if witness["status"] == "sat":
                    try:
                        evidence["witness"] = solver_core._values(witness["stdout"], names)
                        witness["witness_status"] = "available"
                    except (ValueError, TypeError, IndexError, KeyError) as exc:
                        witness.update(witness_status="parse_error", diagnostic=str(exc))
                else:
                    witness["witness_status"] = "unavailable"
                witness["artifacts"] = {"query": label + "_witness.smt2", "result": label + "_witness.json"}
                with (directory / (label + "_witness.smt2")).open("x", encoding="utf-8") as handle:
                    handle.write(witness_query)
                _write(directory / (label + "_witness.json"), witness)
                evidence["witness_evidence"] = witness
                evidence["witness_kind"] = "Static valuation under the query; no design behavior is asserted."
            _write(directory / (label + ".json"), evidence)
            if evidence["status"] in INCONCLUSIVE:
                result["inconclusive_checks"].append({"check": label, "status": evidence["status"],
                                                     "requirement_ids": requirement_ids})
            return evidence

        def finding(code: str, ids: list[str], explanation: str) -> None:
            result["findings"].append({"code": code, "requirement_ids": ids, "explanation": explanation})

        background = run("background", None, [], "Feasibility of declared domains and environmental assumptions (Gamma).")
        result.update(background=background, background_status=background["status"])
        background_ok = background["status"] == "sat"
        if background["status"] == "unsat":
            finding("inconsistent_background", [], "The declared domains and assumptions conflict before requirements are added.")
        if not background_ok:
            consistency = _skip("blocked", "Background feasibility has not been established.", supported_ids)
        elif not supported:
            consistency = _skip("unsupported", "No supported source requirement can be checked.", source_ids)
        else:
            consistency = run("consistency", _and(list(formulas.values())), supported_ids,
                              "Joint consistency of Gamma and every supported source requirement (M).")
            if consistency["status"] == "unsat":
                finding("inconsistent_requirements", supported_ids,
                        "The supported requirements conflict under the feasible background; this does not identify which source is wrong.")
        result.update(consistency=consistency, consistency_status=consistency["status"])
        model_ok = consistency["status"] == "sat"

        for index, row in enumerate(rows):
            rid = row["id"]
            checks: dict[str, dict] = {}
            entry = {"id": rid, "status": row["status"], "checks": checks}
            result["requirements"].append(entry)
            if row["status"] != "supported":
                checks.update({key: _skip("unsupported", row.get("reason", "The source meaning is not executable in this profile."), [rid])
                               for key in CHECK_NAMES})
                continue
            if not background_ok:
                checks.update({key: _skip("blocked", "Background feasibility has not been established.", [rid])
                               for key in CHECK_NAMES})
                continue
            prefix = f"requirement_{index + 1:04d}"
            formula = row["formula"]
            conditional = isinstance(formula, dict) and formula.get("op") == "implies"
            expression = formulas[rid]
            if conditional:
                p, q = [solver_core.emit_formula(arg, context) for arg in formula["args"]]
                checks["trigger_reachability"] = run(prefix + "_trigger", p, [rid], "Can the conditional trigger occur under Gamma?")
                if checks["trigger_reachability"]["status"] == "unsat":
                    finding("unreachable_trigger", [rid], "The trigger is impossible under the declared domains and environmental assumptions.")
                if model_ok:
                    checks["in_model_trigger"] = run(prefix + "_in_model_trigger", _and([*formulas.values(), p]), supported_ids,
                                                     "Can the trigger occur while the complete supported specification holds?")
                    if checks["in_model_trigger"]["status"] == "unsat":
                        finding("trigger_excluded_by_specification", [rid], "The otherwise feasible specification excludes this conditional trigger.")
                else:
                    checks["in_model_trigger"] = _skip("blocked", "The full supported specification is not established SAT.", [rid])
                violation = _and([p, f"(not {q})"])
            else:
                for name in ("trigger_reachability", "in_model_trigger"):
                    checks[name] = _skip("not_applicable", "The root formula is not an implication; nested guards are not extracted.", [rid])
                violation = f"(not {expression})"
            checks["violatability"] = run(prefix + "_violatability", violation, [rid],
                                           "Can Gamma admit a valuation violating this requirement? The target requirement is not assumed.")
            if checks["violatability"]["status"] == "unsat":
                if conditional and checks["trigger_reachability"]["status"] == "unsat":
                    checks["violatability"]["interpretation"] = "No violation exists because the trigger is unreachable."
                else:
                    checks["violatability"]["interpretation"] = "Background alone entails the requirement; inspect for copied guarantees or justified domain consequences."
                    finding("background_entails_requirement", [rid], checks["violatability"]["interpretation"])
            other_ids = [other for other in supported_ids if other != rid]
            others = _and([formulas[other] for other in other_ids])
            checks["redundancy_context"] = run(prefix + "_redundancy_context", others, other_ids,
                                                 "Check feasibility of Gamma and all other supported requirements before testing redundancy.")
            if checks["redundancy_context"]["status"] == "sat":
                checks["redundancy"] = run(prefix + "_redundancy", _and([others, f"(not {expression})"]), supported_ids,
                                             "Can the remaining feasible specification violate the removed target?")
                if checks["redundancy"]["status"] == "unsat":
                    finding("redundant_requirement", [rid], "The other requirements and background entail the target; no source correction is implied.")
            else:
                checks["redundancy"] = _skip("blocked", "The remaining specification is not established SAT; redundancy would be vacuous.", [rid])

        result["admitted"] = background_ok and model_ok and len(supported) == len(rows)
        if result["inconclusive_checks"]:
            result["status"] = "inconclusive"
        elif result["findings"]:
            result["status"] = "findings"
        elif len(supported) != len(rows) or not supported:
            result["status"] = "unsupported"
        else:
            result["status"] = "passed"
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError) as exc:
        result.update(status="inconclusive", background_status="encoding_error", consistency_status="not_run",
                      diagnostic=str(exc), admitted=False)
        result["inconclusive_checks"].append({"check": "validation", "status": "encoding_error", "requirement_ids": []})
    _write(directory / "audit.json", result)
    return result
