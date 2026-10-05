"""Structural coverage of a frozen, source-only obligation inventory.

Coverage evidence must identify an asserted formula or positive conjunction
factor. Merely mentioning a symbol in documentation, a disjunct, an antecedent,
or a conditional consequence cannot establish that the obligation is asserted.
These checks constrain the controlled representation; they do not prove that
the source-only inventory or variable meanings faithfully interpret English.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

SCHEMA = "mbse_obligation_coverage/1"
SLOT_NAMES = {"operation", "condition", "quantity", "operator", "bound", "unit", "participants"}
COVERAGE_INSTRUCTIONS = """Component coverage contract:
Every requirement row must include coverage, with exactly one record for each
obligation in its frozen source-only inventory. Never modify that inventory.
A represented component is {obligation_id,status:'represented',formula_path,
slots:{slot_name:[JSON pointers]}}. All pointers are relative to this requirement
row, starting /formula. formula_path must identify the complete asserted formula
or a positive (possibly nested) conjunction factor, never a disjunct, negated
subexpression, antecedent, or implication consequence. Every asserted conjunction
factor must be assigned; no extra unassigned constraints. Choose the abstraction
from the frozen obligation kind, not a convenient replacement for its meaning.
For capability use one directly asserted Bool variable and slots.operation points
to that variable. Capability availability cannot cover state or event obligations.
For an inventory condition use an implies root; slots.condition points exactly to
its nonconstant antecedent. An event_relation must use an implies root and
slots.participants points, in inventory participant order, to distinct variable
occurrences inside its consequence. This asserts only the inventoried static
occurrence relation, never execution, ordering, delivery or eventuality.
For numeric state slots, use the source comparison operator with the quantity
variable on the left and the source bound literal on the right, either directly
or as the consequence of the inventoried guard. slots.quantity points to the
left variable, slots.operator to the comparison, and slots.bound and slots.unit
to the right literal. Source units and decimal bounds will be normalized exactly.
Subjects, scope, descriptions, modality and semantic correspondence still require
independent source review; documentation is not executable coverage.
For unsupported/unresolved components use {obligation_id,status,reason}; no
formula_path or slots. If any component is not represented, retain the whole
requirement as unsupported/unresolved with all components explicitly disposed;
this milestone does not emit partial executable projections as full coverage.
Use a state_constraint row abstraction for a conjunction of differently typed
components; their individual kinds remain fixed by the inventory. Missing or
invalid coverage is a repair diagnostic, never acceptance evidence."""


def normalize_coverage(raw: Any) -> list[dict]:
    """Validate metadata shape without deciding completeness or source meaning."""
    if not isinstance(raw, list) or len(raw) > 1000:
        raise ValueError("Requirement coverage must be a list of at most 1000 components")
    result = []
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"obligation_id", "status", "formula_path", "slots", "reason"}:
            raise ValueError("Coverage component has invalid fields")
        if not isinstance(item.get("obligation_id"), str) or not item["obligation_id"].strip():
            raise ValueError("Coverage component needs an obligation_id")
        if not isinstance(item.get("status"), str) or item["status"] not in {"represented", "unsupported", "unresolved"}:
            raise ValueError("Coverage component needs represented, unsupported or unresolved status")
        for key in ("formula_path", "reason"):
            if key in item and (not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > 4000):
                raise ValueError(f"Coverage {key} must be nonempty text of at most 4000 characters")
        if "slots" in item:
            slots = item["slots"]
            if not isinstance(slots, dict) or set(slots) - SLOT_NAMES:
                raise ValueError("Coverage slots must use the declared executable slot names")
            for name, paths in slots.items():
                if (not isinstance(paths, list) or not 1 <= len(paths) <= 40
                        or any(not isinstance(p, str) or not p.startswith("/formula") or len(p) > 1000 for p in paths)):
                    raise ValueError(f"Coverage slot {name} needs formula JSON pointers")
        result.append(deepcopy(item))
    return result


def _node(row: dict, path: str) -> Any:
    if not isinstance(path, str) or not path.startswith("/formula"):
        raise ValueError("Expected a row-relative formula pointer")
    value: Any = row
    for token in path[1:].split("/"):
        if isinstance(value, list):
            if not token.isdecimal() or str(int(token)) != token:
                raise ValueError("Array pointer indices must be canonical nonnegative integers")
            value = value[int(token)]
        elif isinstance(value, dict):
            value = value[token]
        else:
            raise ValueError("Pointer does not identify a formula node")
    return value


def _positive(formula: Any, path: str = "/formula") -> dict[str, Any]:
    found = {path: formula}
    if isinstance(formula, dict) and formula.get("op") == "and":
        for index, child in enumerate(formula["args"]):
            found.update(_positive(child, f"{path}/args/{index}"))
    return found


def _variables(formula: Any) -> set[str]:
    if not isinstance(formula, dict):
        return set()
    if "var" in formula:
        return {formula["var"]}
    return set().union(*(_variables(a) for a in formula.get("args", [])))


def _implications(formula: Any, path: str) -> list[str]:
    if not isinstance(formula, dict):
        return []
    found = [path] if formula.get("op") == "implies" else []
    for index, child in enumerate(formula.get("args", [])):
        found.extend(_implications(child, f"{path}/args/{index}"))
    return found


def _check_component(row: dict, component: dict, obligation: dict, variables: dict, context: dict) -> list[dict]:
    from mutation_core import validate_formula

    findings = []
    def fail(code, message):
        findings.append({"code": code, "message": message})
    kind = obligation["kind"]
    if component["status"] != "represented":
        if not component.get("reason"):
            fail("COMPONENT_REASON_MISSING", "An unsupported/unresolved component needs a specific reason.")
        if "formula_path" in component or "slots" in component:
            fail("ABSTENTION_WITH_FORMULA", "An abstained component cannot claim executable bindings.")
        fail("COMPONENT_NOT_REPRESENTED", "The source obligation has no asserted representation.")
        return findings
    if kind in {"unsupported", "unresolved"}:
        fail("FROZEN_PLAN_UNSUPPORTED", "The candidate cannot override a frozen unsupported/unresolved abstraction selection.")
        return findings
    if row["status"] != "supported":
        fail("REQUIREMENT_NOT_EXECUTABLE", "A represented component requires an executable whole requirement.")
        return findings
    path = component.get("formula_path")
    positive = _positive(row["formula"])
    if path not in positive:
        fail("NONASSERTED_COMPONENT_PATH", "Component evidence must be the root or a positive conjunction factor.")
        return findings
    formula = positive[path]
    slots = component.get("slots", {})
    planned = obligation.get("slots", {})
    if component.get("reason"):
        fail("REPRESENTED_WITH_ABSTENTION", "A represented component cannot carry an abstention reason.")
    for name, pointers in slots.items():
        for pointer in pointers:
            try:
                target = _node(row, pointer)
                if (pointer != path and not pointer.startswith(path + "/")) or not isinstance(target, (dict, bool)):
                    raise ValueError("Slot is outside its component")
            except (ValueError, KeyError, IndexError, TypeError):
                fail("INVALID_SLOT_POINTER", f"Slot {name} does not identify an AST node within this component: {pointer}.")

    def exact(name, expected):
        if slots.get(name) != expected:
            fail("SLOT_BINDING_MISMATCH", f"Slot {name} must bind {expected}.")

    body, body_path = formula, path
    guarded = isinstance(formula, dict) and formula.get("op") == "implies"
    if "condition" in planned or kind == "event_relation":
        if not guarded:
            fail("MISSING_GUARD", "The frozen condition needs a complete implication, not an availability flag or consequence alone.")
        else:
            exact("condition", [path + "/args/0"])
            if not _variables(formula["args"][0]):
                fail("CONSTANT_GUARD", "The source condition must be bound to defined variables.")
            body, body_path = formula["args"][1], path + "/args/1"
    elif guarded:
        fail("UNPLANNED_GUARD", "A candidate guard absent from the frozen plan narrows an unconditional obligation.")
    allowed_guards = {path} if "condition" in planned or kind == "event_relation" else set()
    if any(p not in allowed_guards for p in _implications(formula, path)):
        fail("UNPLANNED_NESTED_GUARD", "A conditional restriction must be explicit in its own frozen obligation selection; nested added implications cannot narrow the relation.")
    if kind == "capability":
        name = formula.get("var") if isinstance(formula, dict) else None
        if not name or variables[name]["type"] != "Bool":
            fail("CAPABILITY_SHAPE", "A pure capability must directly assert its bound Boolean availability symbol.")
        exact("operation", [path])
    elif row.get("abstraction", {}).get("kind") == "capability":
        fail("CAPABILITY_SUBSTITUTION", "A capability row cannot cover a state or behavioral obligation.")
    if "operation" in planned and kind != "capability":
        operation_paths = slots.get("operation", [])
        if len(operation_paths) != 1:
            fail("OPERATION_BINDING", "The inventoried operation needs one explicit variable binding in its asserted relation.")
        else:
            try:
                target = _node(row, operation_paths[0])
                if not isinstance(target, dict) or "var" not in target:
                    raise ValueError("Operation binding is not a variable")
            except (ValueError, KeyError, IndexError, TypeError):
                fail("OPERATION_BINDING", "The inventoried operation must bind an actual variable occurrence.")
    if "quantity" in planned:
        expected_operator = planned["operator"]
        if not isinstance(body, dict) or body.get("op") != expected_operator or len(body.get("args", [])) != 2:
            fail("COMPARISON_MISMATCH", "The comparison operator must preserve the frozen source endpoint.")
        else:
            left, right = body["args"]
            if not isinstance(left, dict) or "var" not in left or variables[left["var"]]["type"] not in {"Int", "Real"}:
                fail("QUANTITY_BINDING", "A numeric source quantity must bind the comparison's left variable.")
            else:
                try:
                    expected = validate_formula({"op": expected_operator, "args": [left,
                        {"value": planned["bound"], "unit": planned["unit"]}]}, context)
                    if right != expected["args"][1]:
                        fail("BOUND_MISMATCH", "The literal bound and unit must equal the frozen source bound after unit normalization.")
                except ValueError as exc:
                    fail("BOUND_UNIT_MISMATCH", str(exc))
            exact("quantity", [body_path + "/args/0"])
            exact("operator", [body_path])
            exact("bound", [body_path + "/args/1"])
            exact("unit", [body_path + "/args/1"])
    if "participants" in planned:
        bindings = slots.get("participants", [])
        names = []
        for pointer in bindings:
            try:
                value = _node(row, pointer)
                if not isinstance(value, dict) or "var" not in value:
                    raise ValueError("Participant must bind a variable")
                if kind == "event_relation" and not pointer.startswith(body_path + "/"):
                    raise ValueError("Participant must occur in the consequence")
                names.append(value["var"])
            except (ValueError, KeyError, IndexError, TypeError):
                fail("PARTICIPANT_BINDING", "Each participant must bind a variable occurrence in the asserted relation.")
        if len(names) != len(planned["participants"]) or len(set(names)) != len(names):
            fail("PARTICIPANTS_MISSING", "All inventoried participants need distinct variable bindings in the inventoried order.")
    applicable = set(planned) & SLOT_NAMES
    if set(slots) - applicable:
        fail("UNPLANNED_SLOTS", "Executable slots must correspond to the frozen abstraction plan.")
    return findings


def validate_coverage(tlr: dict, inventory: dict) -> dict:
    """Return inspectable acceptance findings; never infer English obligations.

    The caller supplies a validated frozen obligation inventory. Missing coverage
    remains a repairable report rather than invalidating the executable TLR AST.
    Legacy artifact rendering remains possible when no inventory is supplied.
    """
    from canonical_tlr import validate_tlr, tlr_context
    normalized = validate_tlr(tlr)
    variables = {v["name"]: v for v in normalized["variables"]}
    context = tlr_context(normalized)
    planned_rows = {r["id"]: r for r in inventory["requirements"]}
    output = {"schema": SCHEMA, "status": "passed", "eligible_ids": [], "requirements": [], "findings": []}
    for row in normalized["requirements"]:
        rid = row["id"]
        result = {"id": rid, "status": "passed", "components": [], "findings": []}
        expected = {o["id"]: o for o in planned_rows.get(rid, {}).get("obligations", [])}
        coverage = row.get("coverage", [])
        mapped = {}
        for component in coverage:
            oid = component["obligation_id"]
            if oid in mapped:
                result["findings"].append({"code": "DUPLICATE_COMPONENT", "message": f"Duplicate coverage for {oid}."})
            mapped[oid] = component
        if not expected:
            result["findings"].append({"code": "MISSING_INVENTORY", "message": "No frozen source obligations exist for this requirement."})
        for oid in sorted(set(mapped) - set(expected)):
            result["findings"].append({"code": "UNKNOWN_COMPONENT", "message": f"Coverage names an obligation absent from the frozen source inventory: {oid}."})
        covered_paths = []
        for oid, obligation in expected.items():
            component = mapped.get(oid)
            if component is None:
                detail = {"obligation_id": oid, "status": "missing", "kind": obligation["kind"],
                          "findings": [{"code": "MISSING_COMPONENT", "message": "The inventoried obligation has no coverage disposition."}]}
            else:
                detail = {"obligation_id": oid, "status": component["status"], "kind": obligation["kind"],
                          "findings": _check_component(row, component, obligation, variables, context)}
                if "formula_path" in component:
                    detail["formula_path"] = component["formula_path"]
                if not detail["findings"]:
                    covered_paths.append(component["formula_path"])
            result["components"].append(detail)
        if row["status"] == "supported":
            for path, node in _positive(row["formula"]).items():
                if isinstance(node, dict) and node.get("op") == "and":
                    continue
                if not any(path == p or path.startswith(p + "/") for p in covered_paths):
                    result["findings"].append({"code": "UNASSIGNED_CONSTRAINT", "message": f"Asserted factor {path} has no validated component assignment."})
        if result["findings"] or any(c["findings"] for c in result["components"]):
            result["status"] = "incomplete"
        else:
            output["eligible_ids"].append(rid)
        output["requirements"].append(result)
        for issue in result["findings"]:
            output["findings"].append({**issue, "requirement_id": rid})
        for component in result["components"]:
            for issue in component["findings"]:
                output["findings"].append({**issue, "requirement_id": rid, "obligation_id": component["obligation_id"]})
    for rid in sorted(set(planned_rows) - {r["id"] for r in normalized["requirements"]}):
        output["findings"].append({"code": "MISSING_REQUIREMENT", "message": "Frozen requirement missing from candidate.", "requirement_id": rid})
    if output["findings"]:
        output["status"] = "incomplete"
    return output
