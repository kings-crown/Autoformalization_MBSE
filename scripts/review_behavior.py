"""Explicit bounded synchronous behavior proposals, independent of their properties.

All solver queries are quantifier-free finite unrollings. Check verdicts are
bounded_pass, counterexample, vacuous, pending, unknown, unproved, unsupported.
No result authorizes a source change or establishes unbounded liveness.
"""
from __future__ import annotations

from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import requirements_pipeline as solver
from review_profile import UNIT_MAP, UNIT_WORDS

SCHEMA = "review_behavior/1"
MAX_HORIZON = 20
MAX_VARIABLES = 24
MAX_PROPERTIES = 16
MAX_NODES = 1600
IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,47}\Z")
MAX_VARIABLE_NAME_LENGTH = 64
VARIABLE_IDENT = re.compile(rf"[A-Za-z][A-Za-z0-9_]{{0,{MAX_VARIABLE_NAME_LENGTH - 1}}}\Z")
VARIABLE_NAME_GUIDE = (
    "Variable names start with an ASCII letter, contain only ASCII letters, digits or underscores, "
    f"and have at most {MAX_VARIABLE_NAME_LENGTH} characters."
)
NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?\Z")
ROLES = {"state", "input", "output", "disturbance", "parameter"}
TYPES = {"Bool", "Int", "Real"}


BEHAVIOR_GUIDE = """Produce one JSON object, with no raw code/SMT, schema review_behavior/1.
Required top-level fields: schema, horizon (integer 1..20), step:{value:exact decimal string,unit:time unit}, variables (1..24), initial:[Boolean AST], transitions:[Boolean AST], properties (1..16). Optional assumptions defaults []. No other fields are allowed anywhere.
A variable is {name:identifier,type:Bool|Int|Real,role:state|input|output|disturbance|parameter,unit?:unit,bounds?:{lower?:decimal,upper?:decimal},value?:fixed_value}. Bool has no unit/bounds. Numeric defaults unit 1. Parameters MUST have fixed value (Boolean for Bool; exact decimal string/integer otherwise); other roles MUST NOT have value. Initial values belong in initial predicates. Numeric bounds are inclusive assumptions, not properties. Int requires canonical units and integral bounds/value. Unconstrained state/output/input/disturbance values are nondeterministic; every step's disturbance may vary adversarially. Parameters are constant literal values, not synthesis unknowns.
AST forms: true/false; {var:name,at?:current|next}; {value:exact_decimal_string_or_integer,unit?:unit}; {op:operator,args:[AST,...]}. next is permitted only within transitions and transition-scope assumptions. Numbers cannot be floats, exponent strings, NaN, raw SMT, or code; at most 30 characters after normalization and absolute value <=1000000000000. Supported units include 1, s, ms, min, V, mV, A, mA, W, kW, J, kJ, Wh, kWh, m, cm, mm, km, kg, g, %, K (plus their declared word aliases). Unit conversion is exact; comparisons/addition/subtraction require equal physical dimensions. step must normalize to positive seconds. Window values are integer STEP OFFSETS, not physical durations; multiply by step for elapsed time.
Allowed operators and arity: and/or 2..16 Bool; not 1 Bool; implies 2 Bool; =/!= 2 compatible operands; </<=/>/>= 2 numeric operands of same dimension; + 2 numeric same dimension; - 1 or 2 numeric same dimension; * 2 numeric, with at least one fixed dimensionless literal/parameter factor (linear arithmetic only); ite 3 (Boolean condition, two compatible branches). No division, quantifiers, temporal raw formulas, dynamic indexing, function calls, or nonlinear products. Expression depth <=16; total nodes <=1600. Each initial/transitions list <=80 predicates.
Assumption: {id:identifier,text:nonempty explanation <=2000 characters,scope:initial|always|transition,predicate:Boolean_AST}; <=40, unique ids. initial means step 0, always all states 0..H, transition each transition 0..H-1. Dynamics must be stated independently of properties; never assume a required guarantee as an invariant merely to make its proof pass.
Property ids must be unique identifiers. Every property has id, requirement_ids:[existing source ids, no duplicates], kind. Kinds:
1. always or robustness: required predicate:Boolean_AST, optional margin:numeric_AST (retained for inspection only, not optimized). No trigger/response/window/response_semantics.
2. bounded_response: required trigger:Boolean_AST, response:Boolean_AST, window:{min:integer 0..100,max:integer min..100}, response_semantics:first_after_trigger. No predicate/margin. The FIRST response at or after trigger must fall inside this inclusive window; Boolean response should model an event. Overlapping triggers before completion are unsupported without explicit transaction correlation. Use state/schedule assumptions to make one outstanding command explicit; never add assumptions that assert successful completion.
3. eventual_response: required trigger:Boolean_AST,response:Boolean_AST; no window, response_semantics, predicate, or margin. This states unbounded eventual response and is ALWAYS unproved by this finite engine, even if a finite completion witness exists.
The SMT model is a finite synchronous transition-system proposal. Queries quantify implicitly over full H-transition executions satisfying initial/dynamics/domains/assumptions; deadlocks and shorter nonextendable executions are not verified. Properties remain separate counterexample queries. SAT feasibility is not safety; bounded UNSAT is not unbounded liveness. Preserve source uncertainty in model assumptions and leave engineer approval pending.
"""
BEHAVIOR_GUIDE += VARIABLE_NAME_GUIDE + "\n"


def _keys(obj: Any, allowed: set[str], required: set[str], label: str) -> dict:
    if not isinstance(obj, dict):
        raise ValueError(f"{label} must be an object.")
    if set(obj) - allowed or required - set(obj):
        raise ValueError(f"{label} has missing or unsupported fields: missing={sorted(required-set(obj))}, extra={sorted(set(obj)-allowed)}.")
    return obj


def _name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise ValueError(f"{label} must be a simple identifier of at most 48 characters.")
    return value


def _unit(value: Any) -> tuple[str, Decimal]:
    if value in (None, "1", "unitless", "dimensionless"):
        return "1", Decimal(1)
    if not isinstance(value, str):
        raise ValueError("Units must be supported unit strings.")
    key = value if value in UNIT_MAP else value.lower() if value.lower() in UNIT_WORDS else ""
    if key not in UNIT_MAP:
        raise ValueError(f"Unsupported unit: {value!r}.")
    return UNIT_MAP[key]


def _num(value: Any, factor: Decimal = Decimal(1)) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Use decimal strings or integers for exact numeric values.")
    raw = str(value)
    if len(raw) > 30 or not NUMBER.fullmatch(raw):
        raise ValueError("Numbers must be decimal literals of at most 30 characters.")
    with localcontext() as context:
        context.prec = 80
        number = Decimal(raw) * factor
    if abs(number) > Decimal("1e12"):
        raise ValueError("Numeric magnitude exceeds the bounded profile limit.")
    text = format(number, "f")
    normalized = "0" if not number else text.rstrip("0").rstrip(".") if "." in text else text
    if len(normalized) > 30:
        raise ValueError("Normalized numbers must fit within 30 decimal characters.")
    return normalized


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} through {maximum}.")
    return value


def validate_behavior(payload: dict, requirement_ids: list[str], *, max_variables: int = MAX_VARIABLES) -> dict:
    """Validate and normalize the constrained JSON AST; never execute input code."""
    _integer(max_variables, "Variable capacity", 1, 128)
    _keys(payload, {"schema", "horizon", "step", "variables", "initial", "transitions", "assumptions", "properties"},
          {"schema", "horizon", "step", "variables", "initial", "transitions", "properties"}, "Behavior")
    if payload["schema"] != SCHEMA:
        raise ValueError(f"Behavior schema must be {SCHEMA}.")
    horizon = _integer(payload["horizon"], "horizon", 1, MAX_HORIZON)
    step = _keys(payload["step"], {"value", "unit"}, {"value", "unit"}, "step")
    step_unit, step_factor = _unit(step["unit"])
    step_value = _num(step["value"], step_factor)
    if step_unit != "s" or Decimal(step_value) <= 0:
        raise ValueError("step must be a positive duration in a supported time unit.")
    raw_variables = payload["variables"]
    if not isinstance(raw_variables, list) or not 1 <= len(raw_variables) <= max_variables:
        raise ValueError(f"Provide 1 to {max_variables} typed variables.")
    variables, table = [], {}
    for raw in raw_variables:
        _keys(raw, {"name", "type", "role", "unit", "bounds", "value"}, {"name", "type", "role"}, "Variable")
        name = raw["name"]
        if not isinstance(name, str) or not VARIABLE_IDENT.fullmatch(name):
            raise ValueError(f"Variable name must be a simple identifier of at most {MAX_VARIABLE_NAME_LENGTH} characters.")
        if name in table or not isinstance(raw["type"], str) or raw["type"] not in TYPES or not isinstance(raw["role"], str) or raw["role"] not in ROLES:
            raise ValueError(f"Duplicate variable or unsupported type/role: {name}.")
        var = {"name": name, "type": raw["type"], "role": raw["role"]}
        if var["type"] == "Bool":
            if "unit" in raw or "bounds" in raw:
                raise ValueError("Boolean variables cannot have numerical units or bounds.")
        else:
            unit, factor = _unit(raw.get("unit"))
            if var["type"] == "Int" and factor != 1:
                raise ValueError("Integer variables require canonical units; conversion would change their integer grid.")
            var["unit"] = unit
            bounds = _keys(raw.get("bounds", {}), {"lower", "upper"}, set(), f"{name} bounds")
            normalized = {key: _num(value, factor) for key, value in bounds.items()}
            if var["type"] == "Int" and any(Decimal(v) != Decimal(v).to_integral_value() for v in normalized.values()):
                raise ValueError("Integer bounds must be integral.")
            if "lower" in normalized and "upper" in normalized and Decimal(normalized["lower"]) > Decimal(normalized["upper"]):
                raise ValueError("A variable's lower bound exceeds its upper bound.")
            if normalized:
                var["bounds"] = normalized
        if var["role"] == "parameter":
            if "value" not in raw:
                raise ValueError(f"Parameter {name} requires a fixed value; the solver cannot select a favorable design parameter.")
            if var["type"] == "Bool":
                if not isinstance(raw["value"], bool):
                    raise ValueError("Boolean parameters require true or false.")
                var["value"] = raw["value"]
            else:
                var["value"] = _num(raw["value"], _unit(raw.get("unit"))[1])
                if var["type"] == "Int" and Decimal(var["value"]) != Decimal(var["value"]).to_integral_value():
                    raise ValueError("Integer parameters require integral fixed values.")
        elif "value" in raw:
            raise ValueError("Only fixed parameters use value; specify other initial values through initial predicates.")
        variables.append(var)
        table[name] = var
    budget = [0]

    def expr(raw: Any, allow_next: bool = False, depth: int = 0) -> tuple[Any, str, str]:
        budget[0] += 1
        if budget[0] > MAX_NODES or depth > 16:
            raise ValueError("Behavior expressions exceed the size/depth limit.")
        if isinstance(raw, bool):
            return raw, "Bool", "1"
        if not isinstance(raw, dict):
            raise ValueError("An expression must be a Boolean or constrained AST object, never raw code.")
        if "var" in raw:
            _keys(raw, {"var", "at"}, {"var"}, "Variable reference")
            name = raw["var"]
            if not isinstance(name, str) or name not in table:
                raise ValueError(f"Unknown behavior variable: {name!r}.")
            at = raw.get("at", "current")
            if not isinstance(at, str) or at not in {"current", "next"} or (at == "next" and not allow_next):
                raise ValueError("Next-state references are allowed only in transition predicates.")
            var = table[name]
            return {"var": name, "at": at}, var["type"], var.get("unit", "1")
        if "value" in raw:
            _keys(raw, {"value", "unit"}, {"value"}, "Numeric literal")
            unit, factor = _unit(raw.get("unit"))
            value = _num(raw["value"], factor)
            sort = "Int" if Decimal(value) == Decimal(value).to_integral_value() else "Real"
            return {"value": value, "unit": unit}, sort, unit
        _keys(raw, {"op", "args"}, {"op", "args"}, "Operator")
        op, args = raw["op"], raw["args"]
        if not isinstance(op, str) or not isinstance(args, list) or not 1 <= len(args) <= 16:
            raise ValueError("Operators need an allowed name and 1 to 16 arguments.")
        children = [expr(arg, allow_next, depth + 1) for arg in args]
        sorts, units = [x[1] for x in children], [x[2] for x in children]
        ast = {"op": op, "args": [x[0] for x in children]}
        if op in {"and", "or", "not", "implies"}:
            expected = 1 if op == "not" else 2 if op == "implies" else None
            if any(t != "Bool" for t in sorts) or (expected is not None and len(args) != expected) or (expected is None and len(args) < 2):
                raise ValueError(f"{op} requires Boolean arguments and the correct arity.")
            return ast, "Bool", "1"
        if op == "ite":
            if len(args) != 3 or sorts[0] != "Bool" or units[1] != units[2] or ((sorts[1] == "Bool") != (sorts[2] == "Bool")):
                raise ValueError("ite requires a Boolean condition and compatible branch types/units.")
            return ast, "Real" if "Real" in sorts[1:] else sorts[1], units[1]
        if op in {"=", "!=", "<", "<=", ">", ">="}:
            if len(args) != 2 or units[0] != units[1] or ((sorts[0] == "Bool") != (sorts[1] == "Bool")) or (op not in {"=", "!="} and "Bool" in sorts):
                raise ValueError(f"{op} compares two compatible types and dimensions.")
            return ast, "Bool", "1"
        if op in {"+", "-"}:
            if len(args) not in ({1, 2} if op == "-" else {2}) or "Bool" in sorts or len(set(units)) != 1:
                raise ValueError("Addition/subtraction requires numeric operands with equal dimensions.")
            return ast, "Real" if "Real" in sorts else "Int", units[0]
        if op == "*":
            if len(args) != 2 or "Bool" in sorts:
                raise ValueError("Multiplication requires two numeric operands.")
            constants = []
            for child, _, unit in children:
                fixed = isinstance(child, dict) and ("value" in child or ("var" in child and table[child["var"]]["role"] == "parameter"))
                constants.append(fixed and unit == "1")
            if not any(constants):
                raise ValueError("Linear multiplication requires a fixed dimensionless literal/parameter factor.")
            return ast, "Real" if "Real" in sorts else "Int", units[1] if constants[0] else units[0]
        raise ValueError(f"Unsupported AST operator: {op!r}.")

    def predicate(raw: Any, allow_next: bool = False) -> Any:
        ast, sort, _ = expr(raw, allow_next)
        if sort != "Bool":
            raise ValueError("A predicate must have Boolean type.")
        return ast

    def predicates(raw: Any, label: str, allow_next: bool = False) -> list:
        if not isinstance(raw, list) or len(raw) > 80:
            raise ValueError(f"{label} must be a list of at most 80 predicates.")
        return [predicate(item, allow_next) for item in raw]

    initial = predicates(payload["initial"], "initial")
    transitions = predicates(payload["transitions"], "transitions", True)
    assumptions, assumption_ids = [], set()
    raw_assumptions = payload.get("assumptions", [])
    if not isinstance(raw_assumptions, list) or len(raw_assumptions) > 40:
        raise ValueError("Provide at most 40 explicit assumptions.")
    for item in raw_assumptions:
        _keys(item, {"id", "text", "scope", "predicate"}, {"id", "text", "scope", "predicate"}, "Assumption")
        aid = _name(item["id"], "Assumption id")
        if aid in assumption_ids or not isinstance(item["scope"], str) or item["scope"] not in {"initial", "always", "transition"}:
            raise ValueError("Assumption IDs must be unique and scopes explicit.")
        if not isinstance(item["text"], str) or not 1 <= len(item["text"]) <= 2000:
            raise ValueError("Assumptions require explanatory text of at most 2000 characters.")
        assumption_ids.add(aid)
        assumptions.append({**item, "predicate": predicate(item["predicate"], item["scope"] == "transition")})
    raw_properties = payload["properties"]
    if not isinstance(raw_properties, list) or not 1 <= len(raw_properties) <= MAX_PROPERTIES:
        raise ValueError(f"Provide 1 to {MAX_PROPERTIES} independently stated properties.")
    known_ids, property_ids, properties = set(requirement_ids), set(), []
    for item in raw_properties:
        _keys(item, {"id", "requirement_ids", "kind", "predicate", "trigger", "response", "window", "response_semantics", "margin"}, {"id", "requirement_ids", "kind"}, "Property")
        pid = _name(item["id"], "Property id")
        ids = item["requirement_ids"]
        if pid in property_ids or not isinstance(ids, list) or not ids or any(not isinstance(rid, str) or rid not in known_ids for rid in ids) or len(set(ids)) != len(ids):
            raise ValueError("Property IDs must be unique and link to existing source requirement IDs.")
        property_ids.add(pid)
        kind = item["kind"]
        if not isinstance(kind, str):
            raise ValueError("Property kind must be a supported string.")
        out = {"id": pid, "requirement_ids": ids, "kind": kind}
        if kind in {"always", "robustness"}:
            if "predicate" not in item or set(item) & {"trigger", "response", "window", "response_semantics"}:
                raise ValueError("Safety/robustness properties require an independent predicate.")
            out["predicate"] = predicate(item["predicate"])
            if "margin" in item:
                margin, sort, unit = expr(item["margin"])
                if sort == "Bool":
                    raise ValueError("A robustness margin must be numeric.")
                out["margin"] = margin
        elif kind in {"bounded_response", "eventual_response"}:
            if not {"trigger", "response"} <= set(item) or set(item) & {"predicate", "margin"}:
                raise ValueError("Response properties require trigger and response predicates.")
            out.update(trigger=predicate(item["trigger"]), response=predicate(item["response"]))
            if kind == "bounded_response":
                if item.get("response_semantics") != "first_after_trigger":
                    raise ValueError("Bounded response requires explicit first_after_trigger semantics.")
                window = _keys(item.get("window"), {"min", "max"}, {"min", "max"}, "Response window")
                lo = _integer(window["min"], "window.min", 0, 100)
                hi = _integer(window["max"], "window.max", lo, 100)
                out.update(window={"min": lo, "max": hi}, response_semantics="first_after_trigger")
            elif "window" in item or "response_semantics" in item:
                raise ValueError("Unbounded eventual response cannot carry a finite window or be relabeled a bounded proof.")
        else:
            raise ValueError(f"Unsupported property kind: {kind!r}.")
        properties.append(out)
    return {"schema": SCHEMA, "horizon": horizon, "step": {"value": step_value, "unit": "s"}, "variables": variables,
            "initial": initial, "transitions": transitions, "assumptions": assumptions, "properties": properties}


def _number(text: str) -> str:
    return f"(- {text[1:]})" if text.startswith("-") else text


def _and(parts: list[str]) -> str:
    return "true" if not parts else parts[0] if len(parts) == 1 else "(and " + " ".join(parts) + ")"


def _or(parts: list[str]) -> str:
    return "false" if not parts else parts[0] if len(parts) == 1 else "(or " + " ".join(parts) + ")"


def _emit(ast: Any, step: int, table: dict) -> str:
    if isinstance(ast, bool):
        return "true" if ast else "false"
    if "value" in ast:
        return _number(ast["value"])
    if "var" in ast:
        var = table[ast["var"]]
        if var["role"] == "parameter":
            return ("true" if var["value"] else "false") if var["type"] == "Bool" else _number(var["value"])
        return f"v_{var['name']}_{step + (ast['at'] == 'next')}"
    op = {"implies": "=>", "!=": "distinct"}.get(ast["op"], ast["op"])
    return "(" + op + " " + " ".join(_emit(arg, step, table) for arg in ast["args"]) + ")"


def _value(ast: Any) -> Any:
    if ast in ("true", "false"):
        return ast == "true"
    if isinstance(ast, list):
        if ast[0] == "-" and len(ast) == 2:
            return -_value(ast[1])
        if ast[0] == "/" and len(ast) == 3:
            return _value(ast[1]) / _value(ast[2])
        raise ValueError("Unsupported exact solver value.")
    return Fraction(str(ast))


def _display(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, Fraction):
        return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"
    return value


def _time(step: str, index: int) -> str:
    with localcontext() as context:
        context.prec = 80
        value = Decimal(step) * index
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def analyze_behavior(behavior: dict, run_dir: Path) -> dict:
    """Check finite counterexample queries, preserving every inconclusive boundary."""
    contract_context = None
    if behavior.get("schema") == "review_contracts/1":
        from review_contracts import behavior_from_contracts
        contract_context = {"source_hash": behavior["source_hash"], "context_sha256": behavior["context_sha256"], "behavior_sha256": behavior["behavior_sha256"]}
        behavior = behavior_from_contracts(behavior)
        if behavior is None:
            raise ValueError("The shared contracts contain no executable behavior model.")
    ids = list(dict.fromkeys(rid for prop in behavior.get("properties", []) for rid in prop.get("requirement_ids", [])))
    behavior = validate_behavior(behavior, ids)
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    table = {var["name"]: var for var in behavior["variables"]}
    horizon = behavior["horizon"]
    lines = ["(set-logic QF_LIRA)", "(set-option :produce-models true)", "(set-option :produce-unsat-cores true)"]
    names = []
    for var in table.values():
        if var["role"] != "parameter":
            for index in range(horizon + 1):
                name = f"v_{var['name']}_{index}"
                names.append((name, var["name"], index))
                lines.append(f"(declare-const {name} {var['type']})")
    labels = {}

    def assertion(expression: str, description: str) -> None:
        label = f"base_{len(labels)}"
        labels[label] = description
        lines.append(f"(assert (! {expression} :named {label}))")

    for var in table.values():
        for index in range(1 if var["role"] == "parameter" else horizon + 1):
            ref = _emit({"var": var["name"], "at": "current"}, index, table)
            for key, bound in var.get("bounds", {}).items():
                operator = ">=" if key == "lower" else "<="
                assertion(f"({operator} {ref} {_number(bound)})", f"Domain assumption: {var['name']} {operator} {bound} {var.get('unit', '1')} at step {index}.")
    for expr in behavior["initial"]:
        assertion(_emit(expr, 0, table), "Initial-state predicate.")
    for index in range(horizon):
        for expr in behavior["transitions"]:
            assertion(_emit(expr, index, table), f"Transition predicate from step {index} to {index+1}.")
    for assumption in behavior["assumptions"]:
        indices = [0] if assumption["scope"] == "initial" else range(horizon) if assumption["scope"] == "transition" else range(horizon + 1)
        for index in indices:
            assertion(_emit(assumption["predicate"], index, table), f"{assumption['id']}: {assumption['text']} (step {index}).")
    base = "\n".join(lines) + "\n"
    variables_to_get = " ".join(name for name, _, _ in names)
    scope = {"horizon_steps": horizon, "step_seconds": behavior["step"]["value"],
             "time_interval_seconds": ["0", _time(behavior["step"]["value"], horizon)],
             "quantifier_free_logic": "QF_LIRA", "meaning": "Full executions extendable through H transitions under the explicit initial conditions, transitions, domains, and assumptions; shorter nonextendable executions are not checked."}

    def query(label: str, extra: str | None = None, trace: bool = False) -> dict:
        text = base + (f"(assert (! {extra} :named query_goal))\n" if extra else "") + "(check-sat)\n"
        path = run_dir / f"behavior_{label}.smt2"
        result = solver.run_z3_fragment(text)
        verdict = solver._solver_verdict(result) or "unknown"
        if result.get("cross_check") and not result["cross_check"].get("agree"):
            verdict = "unknown"
        if trace and verdict == "sat" and names:
            text += f"(get-value ({variables_to_get}))\n"
            evidence = solver.run_z3_fragment(text)
            if solver._solver_verdict(evidence) != "sat" or (evidence.get("cross_check") and not evidence["cross_check"].get("agree")):
                verdict = "unknown"
            result = evidence
        elif verdict == "unsat":
            text += "(get-unsat-core)\n"
            evidence = solver.run_z3_fragment(text)
            if solver._solver_verdict(evidence) != "unsat" or (evidence.get("cross_check") and not evidence["cross_check"].get("agree")):
                verdict = "unknown"
            result = evidence
        path.write_text(text, encoding="utf-8")
        record = {"verdict": verdict, "solver_result": result, "query_sha256": hashlib.sha256(text.encode()).hexdigest(),
                  "artifacts": {"query": path.name, "result": f"behavior_{label}.json"}, "trace": []}
        if trace and verdict == "sat":
            values = {}
            try:
                if names:
                    parsed = solver._parse_sexpr("\n".join(result.get("result", "").splitlines()[1:]))[0]
                    values = {item[0]: _display(_value(item[1])) for item in parsed}
                for index in range(horizon + 1):
                    row = {var["name"]: var["value"] if var["role"] == "parameter" else values.get(f"v_{var['name']}_{index}") for var in table.values()}
                    record["trace"].append({"step": index, "time_seconds": _time(behavior["step"]["value"], index), "values": row})
            except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
                record["trace_diagnostics"] = "Exact trace decoding failed; inspect raw solver output."
        if verdict == "unsat":
            try:
                core = solver._parse_sexpr("\n".join(result.get("result", "").splitlines()[1:]))[0]
                record["core"] = [labels.get(label, "Property-negation query.") for label in core]
            except (ValueError, TypeError, IndexError):
                record["core"] = []
        (run_dir / record["artifacts"]["result"]).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return record

    feasible = query("model", trace=True)
    checks = []
    limitations = [
        "This is a finite synchronous discrete-time proposal, not an ODE simulation or unbounded proof.",
        "Only executions extendable through H transitions are checked; transition totality, deadlocks, and shorter nonextendable executions are not verified.",
        "Unit conversions and dimensional checks occur before numeric SMT translation; integer grids require canonical units.",
        "Variable bounds, initial predicates, transitions, and assumptions restrict the behaviors considered and require engineer review.",
        "Inputs and disturbances are adversarial free choices at each step within declared constraints; parameters are fixed values.",
        "A bounded response concerns the first Boolean response at or after the trigger; overlapping commands require explicit transaction correlation and are not accepted by this profile.",
        "Near-horizon response windows are pending. A finite completed witness never proves eventual response on every infinite execution.",
        "Solver UNSAT is bounded counterexample absence, not an independently checked proof certificate or source-fidelity guarantee.",
    ]
    assumptions = [item["text"] for item in behavior["assumptions"]]
    assumptions += [f"{var['name']} has fixed value {var['value']} {var.get('unit', '')}." for var in table.values() if var["role"] == "parameter"]
    for prop in behavior["properties"]:
        check = {"id": prop["id"], "requirement_ids": prop["requirement_ids"], "kind": prop["kind"], "scope": dict(scope),
                 "verdict": "unknown", "summary": "No conclusive property result.", "trace": [], "artifacts": {}}
        if feasible["verdict"] != "sat":
            check["summary"] = "Property was not checked: assumptions/model are infeasible or feasibility is unknown. No vacuous pass is allowed."
            checks.append(check)
            continue
        if prop["kind"] in {"always", "robustness"}:
            bad = _or([f"(not {_emit(prop['predicate'], index, table)})" for index in range(horizon + 1)])
            result = query(prop["id"] + "_counterexample", bad, True)
            check.update(verdict={"sat": "counterexample", "unsat": "bounded_pass"}.get(result["verdict"], "unknown"),
                         trace=result["trace"], artifacts=result["artifacts"], evidence=result)
            check["summary"] = {"counterexample": "A modeled finite trace violates this property.", "bounded_pass": "No violating full H-transition execution exists under the stated model assumptions; shorter nonextendable executions are outside this check.", "unknown": "Solver did not establish counterexample existence or absence."}[check["verdict"]]
            if prop["kind"] == "robustness":
                check["disturbances"] = [var for var in table.values() if var["role"] == "disturbance"]
                check["summary"] += " Disturbance values were searched adversarially, with fixed design parameters."
            if "margin" in prop:
                check["margin_expression"] = prop["margin"]
                check["margin_scope"] = "Proposed signed margin expression retained for inspection; no worst-case optimization was performed."
            checks.append(check)
            continue
        trigger = [_emit(prop["trigger"], index, table) for index in range(horizon + 1)]
        response = [_emit(prop["response"], index, table) for index in range(horizon + 1)]
        reachable = query(prop["id"] + "_trigger", _or(trigger), True)
        check["trigger_reachability"] = reachable
        if prop["kind"] == "eventual_response":
            complete_trace = _or([_and([trigger[k], _or(response[k:])]) for k in range(horizon + 1)])
            witness = query(prop["id"] + "_completion_witness", complete_trace, True)
            check.update(verdict="unproved", summary="Unbounded eventual response is unproved. A finite completion witness, if present, establishes possibility only.",
                         trace=witness["trace"], finite_completion_witness=witness, artifacts=witness["artifacts"])
            checks.append(check)
            continue
        lo, hi = prop["window"]["min"], prop["window"]["max"]
        complete_indices = list(range(max(0, horizon - hi + 1)))
        late_indices = list(range(max(0, horizon - hi + 1), horizon + 1))
        check["scope"].update(response_window_steps={"min": lo, "max": hi}, complete_trigger_steps=complete_indices, pending_trigger_steps=late_indices)
        if reachable["verdict"] != "sat":
            check.update(verdict="vacuous" if reachable["verdict"] == "unsat" else "unknown",
                         summary="The trigger is unreachable on full H-transition executions under this model; no response guarantee is claimed." if reachable["verdict"] == "unsat" else "Trigger reachability is unknown.")
            checks.append(check)
            continue
        overlaps = [_and([trigger[k], trigger[j], *[f"(not {response[m]})" for m in range(k, j)]]) for k in range(horizon) for j in range(k + 1, horizon + 1)]
        overlap = query(prop["id"] + "_overlap", _or(overlaps), True)
        check["overlap_check"] = overlap
        if overlap["verdict"] != "unsat":
            check.update(verdict="unsupported" if overlap["verdict"] == "sat" else "unknown", trace=overlap["trace"], artifacts=overlap["artifacts"],
                         summary="Overlapping triggers without an intervening completion are reachable or unresolved. Add transaction correlation; one Boolean response cannot discharge multiple commands here.")
            checks.append(check)
            continue
        bad_cases = [_and([trigger[k], _or([_or(response[k:k + lo]), _and([f"(not {response[j]})" for j in range(k + lo, k + hi + 1)])])]) for k in complete_indices]
        result = query(prop["id"] + "_counterexample", _or(bad_cases), True)
        late = query(prop["id"] + "_pending", _or([trigger[index] for index in late_indices]), True)
        check.update(trace=result["trace"], artifacts=result["artifacts"], evidence=result, pending_windows=late)
        if result["verdict"] == "sat":
            check.update(verdict="counterexample", summary="A fully observed trigger has an early or missing first response in its required window.")
        elif result["verdict"] != "unsat" or late["verdict"] == "unknown":
            check.update(verdict="unknown", summary="The bounded response or pending-window query is inconclusive.")
        elif late["verdict"] == "sat":
            check.update(verdict="pending", summary="Complete windows have no violation, but a reachable late trigger extends beyond the horizon; its obligation remains pending.", trace=late["trace"])
        else:
            check.update(verdict="bounded_pass", summary="Among full H-transition executions, the trigger is reachable, commands do not overlap, and every response window is observed with no violation.")
        checks.append(check)
    verdicts = {check["verdict"] for check in checks}
    status = "infeasible" if feasible["verdict"] == "unsat" else "unknown" if feasible["verdict"] != "sat" else "counterexample" if "counterexample" in verdicts else "bounded_pass" if verdicts == {"bounded_pass"} else "partial"
    result = {"schema": "review_behavior_evidence/1", "status": status, "summary": {
        "infeasible": "The behavior model and assumptions are inconsistent; properties receive no pass.",
        "unknown": "Model feasibility is unknown; no property guarantee is available.",
        "counterexample": "A finite modeled counterexample was found; inspect its source-linked property and trace.",
        "bounded_pass": "All requested properties passed only for full H-transition executions in the recorded finite scope; shorter nonextendable executions and semantic approval remain separate.",
        "partial": "Some properties remain pending, vacuous, unsupported, or unproved; there is no complete guarantee.",
    }[status], "scope": scope, "checks": checks, "assumptions": assumptions, "model_feasibility": feasible,
        "limitations": limitations, "source_fidelity": "pending engineer review", "approval_blocked": True,
        "linked_requirement_ids": ids, "checked_requirement_ids": list(dict.fromkeys(rid for check in checks if check["verdict"] in {"bounded_pass", "counterexample"} for rid in check["requirement_ids"]))}
    if contract_context is not None:
        result["contract_input"] = contract_context
    (run_dir / "behavior_analysis.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result
