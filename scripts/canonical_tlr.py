"""Executable current-state TLR shared by LLM generation, SMT and SysML.

Only source identity/text, typed expressions and explicit background assumptions
are required. No hashes, provenance attestations, or reviewer metadata are needed.
Validation is structural and dimensional; it cannot establish source fidelity.
"""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any

from mutation_core import validate_context, validate_formula

SCHEMA = "mbse_tlr/1"
PROFILE = "Current-state Boolean and linear integer/real constraints; no temporal or probabilistic operators."


def _keys(value: Any, allowed: set[str], required: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object.")
    unknown, missing = set(value) - allowed, required - set(value)
    if unknown or missing:
        raise ValueError(f"{label}: unknown fields {sorted(unknown)}, missing fields {sorted(missing)}.")


def _text(value: Any, label: str, maximum: int = 100000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be nonempty text of at most {maximum} characters.")
    return value


def _references(ast: Any) -> set[str]:
    if not isinstance(ast, dict):
        return set()
    if "var" in ast:
        return {ast["var"]} if isinstance(ast["var"], str) else set()
    return set().union(*(_references(a) for a in ast.get("args", [])))


def validate_tlr(payload: Any, sources: list[dict] | None = None) -> dict:
    """Validate and canonicalize the static executable profile without repairs.

    Numeric values and bounds become exact canonical-unit magnitudes. Source
    packets, when supplied, must have exactly the same unique requirement IDs;
    source wording is preserved and a different generated wording is rejected.
    Unsupported and unresolved obligations remain documentation-only records.
    The shared validator currently supports at most 24 variables, 40 assumptions,
    depth 16 and 1600 expression nodes per validation call.
    """
    _keys(payload, {"schema", "variables", "assumptions", "requirements"},
          {"schema", "variables", "requirements"}, "TLR")
    if payload["schema"] != SCHEMA:
        raise ValueError(f"TLR schema must be {SCHEMA}.")
    raw_variables = payload["variables"]
    raw_assumptions = payload.get("assumptions", [])
    rows = payload["requirements"]
    if not isinstance(raw_variables, list):
        raise ValueError("TLR variables must be a list.")
    if not isinstance(raw_assumptions, list):
        raise ValueError("TLR assumptions must be a list.")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValueError("Provide 1 to 1000 requirement records.")
    descriptions = {}
    variables = []
    for raw in raw_variables:
        _keys(raw, {"name", "type", "unit", "bounds", "description"}, {"name", "type"}, "Variable")
        if "description" in raw:
            descriptions[str(raw["name"])] = _text(raw["description"], "Variable description", 2000)
        variables.append({key: deepcopy(value) for key, value in raw.items() if key != "description"})
    if variables:
        context = validate_context(variables, raw_assumptions)
    else:
        if raw_assumptions:
            raise ValueError("Executable assumptions require typed variables.")
        context = {"variables": [], "background": []}
    table = {v["name"]: v for v in context["variables"]}
    source_by_id = {}
    if sources is not None:
        if not isinstance(sources, list):
            raise ValueError("Sources must be a list of requirement records.")
        for row in sources:
            if not isinstance(row, dict):
                raise ValueError("Source requirement must be an object.")
            rid = _text(row.get("id"), "Source requirement ID", 200)
            _text(row.get("text"), "Source requirement text")
            if rid in source_by_id:
                raise ValueError(f"Duplicate source requirement ID: {rid}.")
            source_by_id[rid] = row
    normalized, ids = [], set()
    for row in rows:
        _keys(row, {"id", "status", "formula", "reason", "text", "source"}, {"id", "status"}, "Requirement")
        rid = _text(row["id"], "Requirement ID", 200)
        if rid in ids:
            raise ValueError(f"Duplicate TLR requirement ID: {rid}.")
        ids.add(rid)
        status = row["status"]
        if not isinstance(status, str) or status not in {"supported", "unsupported", "unresolved"}:
            raise ValueError(f"Requirement {rid} has an invalid status.")
        out = {"id": rid, "status": status}
        if status == "supported":
            if "formula" not in row or "reason" in row:
                raise ValueError(f"Supported requirement {rid} needs formula and no unsupported reason.")
            if not variables:
                raise ValueError("Supported requirements need typed variables.")
            if isinstance(row["formula"], bool):
                raise ValueError(f"Requirement {rid}: whole-formula truth constants are not executable interpretations.")
            formula = validate_formula(row["formula"], context)
            referenced = _references(formula)
            if not referenced:
                raise ValueError(f"Requirement {rid}: formula must constrain declared quantities or conditions.")
            normalized_id = re.sub(r"[^a-z0-9_]", "_", rid.lower())
            placeholders = [name for name in referenced if table[name]["type"] == "Bool" and (
                name.lower() == normalized_id + "_holds" or
                re.fullmatch(r"(?:req(?:uirement)?s?_?\d+|r_?\d+|.*_(?:req|r)_?\d+)_holds", name.lower())
            )]
            if placeholders:
                raise ValueError(f"Requirement {rid}: opaque truth placeholders are unsupported: {placeholders}.")
            out["formula"] = formula
        else:
            if "formula" in row:
                raise ValueError(f"Unsupported/unresolved requirement {rid} cannot carry an executable formula.")
            out["reason"] = _text(row.get("reason"), f"Requirement {rid} reason", 4000)
        if "text" in row:
            out["text"] = _text(row["text"], f"Requirement {rid} text")
        if "source" in row:
            out["source"] = deepcopy(row["source"])
        if sources is not None and rid in source_by_id:
            source = source_by_id[rid]
            if "text" in row and row["text"] != source["text"]:
                raise ValueError(f"Requirement {rid}: generated text differs from original source wording.")
            out["text"] = source["text"]
            if "source" in source:
                out["source"] = deepcopy(source["source"])
            else:
                out.pop("source", None)
        normalized.append(out)
    if sources is not None:
        if ids != set(source_by_id):
            raise ValueError(f"TLR/source IDs differ: missing {sorted(set(source_by_id) - ids)}, extra {sorted(ids - set(source_by_id))}.")
        # Source order also fixes the deterministic rendering order.
        by_id = {row["id"]: row for row in normalized}
        normalized = [by_id[row["id"]] for row in sources]
    for variable in context["variables"]:
        if variable["name"] in descriptions:
            variable["description"] = descriptions[variable["name"]]
    return {"schema": SCHEMA, "variables": context["variables"],
            "assumptions": context["background"], "requirements": normalized}


def tlr_context(tlr: dict) -> dict:
    """Return the typed SMT/mutation vocabulary and explicit background only."""
    normalized = validate_tlr(tlr)
    return {"variables": [{k: deepcopy(v) for k, v in row.items() if k != "description"}
                          for row in normalized["variables"]],
            "background": deepcopy(normalized["assumptions"])}


def requirement_formula(row: dict) -> Any:
    return deepcopy(row["formula"]) if row.get("status") == "supported" else None


def conjunction(formulas: list[Any]) -> Any:
    """Build an AST conjunction without exceeding the shared 16-child arity."""
    level = deepcopy(list(formulas))
    if not level:
        return True
    while len(level) > 1:
        level = [chunk[0] if len(chunk) == 1 else {"op": "and", "args": chunk}
                 for start in range(0, len(level), 16) if (chunk := level[start:start + 16])]
    return level[0]


def _doc(value: Any) -> str:
    return str(value).replace("*/", "* /").replace("/*", "/ *").replace("\x00", "")


def _identifier(value: str) -> str:
    return "M_" + re.sub(r"[^A-Za-z0-9_]", "_", value)[:100]


def _expr(ast: Any, subject: str = "") -> str:
    if isinstance(ast, bool):
        return "true" if ast else "false"
    if "var" in ast:
        return subject + "v_" + ast["var"]
    if "value" in ast:
        value = ast["value"]
        return "(" + value + ")" if value.startswith("-") else value
    args = [_expr(a, subject) for a in ast["args"]]
    op = ast["op"]
    if op == "ite":
        return "(if " + args[0] + " ? " + args[1] + " else " + args[2] + ")"
    if len(args) == 1:
        return "(" + op + " " + args[0] + ")"
    op = {"=": "=="}.get(op, op)
    return "(" + (" " + op + " ").join(args) + ")"


def render_sysml(tlr: dict, name: str = "RequirementsModel") -> str:
    """Render exactly the validated AST into one shared typed requirements model.

    Values are canonical scalar magnitudes with documented physical units. This
    avoids claiming SI quantity types while still enforcing dimensional validity
    in the common AST. Background constraints and requirements remain distinct.
    No inferred architecture or requirement-satisfaction assertion is introduced.
    """
    normalized = validate_tlr(tlr)
    package = _identifier(str(name))
    lines = [f"package {package} {{", "    private import ScalarValues::*;",
             "    doc /* Current-state requirement constraints.",
             "       Numeric values are canonical scalar magnitudes; physical units are documented metadata.",
             "       Source wording and any unsupported meaning appear on each requirement. */",
             "    part def RequirementState {"]
    for variable in normalized["variables"]:
        scalar = {"Bool": "Boolean", "Int": "Integer", "Real": "Real"}[variable["type"]]
        name = "v_" + variable["name"]
        description = f"Quantity: {variable['name']}; canonical unit: {variable.get('unit', '1')}."
        if variable.get("description"):
            description += " " + variable["description"]
        lines += [f"        attribute {name} : ScalarValues::{scalar} {{",
                  f"            doc /* {_doc(description)} */", "        }"]
    lines.append("        doc /* BACKGROUND: domains and explicit assumptions, not requirement guarantees. */")
    for variable in normalized["variables"]:
        for bound, value in variable.get("bounds", {}).items():
            operator = ">=" if bound == "lower" else "<="
            literal = "(" + value + ")" if value.startswith("-") else value
            lines.append(f"        assert constraint domain_{variable['name']}_{bound} {{ v_{variable['name']} {operator} {literal} }}")
    for number, assumption in enumerate(normalized["assumptions"], 1):
        lines += [f"        doc /* Background { _doc(assumption['id']) }: {_doc(assumption['text'])} */",
                  f"        assert constraint background_{number} {{ {_expr(assumption['predicate'])} }}"]
    lines += ["    }", "    part modeledState : RequirementState;", ""]
    for number, row in enumerate(normalized["requirements"], 1):
        lines += [f"    requirement Req_{number}_{_identifier(row['id'])} {{",
                  f"        doc /* Source requirement: {_doc(row['id'])}; formalization status: {row['status']}."]
        if row.get("text"):
            lines.append("           " + _doc(row["text"]))
        if row.get("source"):
            lines.append("           Source: " + _doc(json.dumps(row["source"], ensure_ascii=False, sort_keys=True)))
        if row.get("reason"):
            lines.append("           Limitation: " + _doc(row["reason"]))
        lines.append("        */")
        if row["status"] == "supported":
            lines += ["        subject observedSystem : RequirementState = modeledState;",
                      f"        require constraint obligation {{ {_expr(row['formula'], 'observedSystem.')} }}"]
        lines += ["    }", ""]
    return "\n".join(lines + ["}", ""])
