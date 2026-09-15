"""Conservative local requirements profile, sharing the existing pipeline's solver seam.

This is an explicit, limited grammar. Unrecognized text stays in the model and
review packet; it never becomes a placeholder Boolean or an asserted True.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import requirements_pipeline as pipeline
from tlr_contracts import ensure_requirements_tlf_contract

MAX_REQUIREMENTS = 500
MAX_INPUT_BYTES = 512_000
MAX_REQUIREMENT_CHARS = 4_000
NAME = r"[A-Za-z][A-Za-z0-9_-]*(?: [A-Za-z][A-Za-z0-9_-]*)*?"
NUMBER = r"[-+]?\d+(?:\.\d+)?"
OPS = {"within": "le", "at most": "le", "no more than": "le", "not exceed": "le", "<=": "le",
       "no earlier than": "ge", "at least": "ge", "no less than": "ge", ">=": "ge",
       "less than": "lt", "before": "lt", "<": "lt", "greater than": "gt", "later than": "gt", ">": "gt",
       "equal to": "eq", "exactly": "eq", "=": "eq", "==": "eq"}
OP = "(?:" + "|".join(re.escape(x) for x in sorted(OPS, key=len, reverse=True)) + ")"
UNIT = r"[A-Za-z%]+"
UNIT_MAP: dict[str, tuple[str, Decimal]] = {}
UNIT_SYMBOLS = {"s", "ms", "min", "V", "mV", "kg", "g", "m", "mm", "cm", "km", "A", "mA", "W", "kW", "J", "kJ", "Wh", "kWh", "K", "%"}
UNIT_WORDS: set[str] = set()


def _units(canonical: str, factor: str, *aliases: str) -> None:
    for alias in aliases:
        UNIT_MAP[alias] = (canonical, Decimal(factor))
        if alias not in UNIT_SYMBOLS:
            UNIT_WORDS.add(alias.lower())


_units("s", "1", "s", "sec", "second", "seconds")
_units("s", "0.001", "ms", "millisecond", "milliseconds")
_units("s", "60", "min", "minute", "minutes")
_units("V", "1", "V", "volt", "volts")
_units("V", "0.001", "mV", "millivolt", "millivolts")
_units("kg", "1", "kg", "kilogram", "kilograms")
_units("kg", "0.001", "g", "gram", "grams")
_units("m", "1", "m", "meter", "meters", "metre", "metres")
_units("m", "0.001", "mm", "millimeter", "millimeters")
_units("m", "0.01", "cm", "centimeter", "centimeters")
_units("m", "1000", "km", "kilometer", "kilometers")
_units("A", "1", "A", "amp", "amps", "ampere", "amperes")
_units("A", "0.001", "mA", "milliamp", "milliamps")
_units("W", "1", "W", "watt", "watts")
_units("W", "1000", "kW", "kilowatt", "kilowatts")
_units("J", "1", "J", "joule", "joules")
_units("J", "1000", "kJ", "kilojoule", "kilojoules")
_units("J", "3600", "Wh")
_units("J", "3600000", "kWh")
_units("K", "1", "K", "kelvin")
_units("%", "1", "%", "percent")
_units("1", "1", "unitless", "dimensionless")

TIMING = re.compile(
    rf"After (?:each|every|the|a) (?P<trigger>{NAME}) command,? (?:the )?(?P<subject>{NAME}) "
    rf"shall (?:produce|send|issue) (?:the |a |an )?(?P<response>{NAME}) "
    rf"(?P<op>{OP}) (?P<value>{NUMBER}) (?P<unit>{UNIT})", re.I)
QUANTITY = re.compile(
    rf"(?:The )?(?P<subject>{NAME}) shall have (?:a |an |the )?(?P<quantity>{NAME}) "
    rf"(?:of )?(?P<op>{OP}) (?P<value>{NUMBER})(?: (?P<unit>{UNIT}))?", re.I)
POSSESSIVE = re.compile(
    rf"(?:The )?(?P<subject>{NAME})['’]s (?P<quantity>{NAME}) shall (?:be )?"
    rf"(?P<op>{OP}) (?P<value>{NUMBER})(?: (?P<unit>{UNIT}))?", re.I)
DOT_QUANTITY = re.compile(
    rf"(?P<subject>[A-Za-z][A-Za-z0-9_-]*)\.(?P<quantity>[A-Za-z][A-Za-z0-9_-]*) "
    rf"(?:shall (?:be )?)?(?P<op>{OP}) (?P<value>{NUMBER})(?: (?P<unit>{UNIT}))?", re.I)

SAMPLE = {
    "name": "Controller response and electrical limits", "format": "csv",
    "text": 'id,requirement,owner,source\n'
    'REQ-042,"After each START command, the controller shall produce the response within 5 seconds.",Operations owner,Operations requirements §4.2\n'
    'REQ-087,"After each START command, the controller shall produce the response no earlier than 8 seconds.",Timing owner,Timing constraints §2.7\n'
    'REQ-101,"The battery shall have a voltage of at least 10.5 V.",Electrical owner,Electrical requirements §3.1\n'
    'REQ-102,"The battery shall have a voltage of at most 28 V.",Electrical owner,Electrical requirements §3.2\n'
    'REQ-103,"The controller shall have a mass of at most 2 kg.",Mechanical owner,Mass budget §1.1\n'
}


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def parse_requirements(text: str, format: str, name: str) -> list[dict[str, Any]]:
    if not text.strip():
        raise ValueError("Provide at least one requirement.")
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError(f"Input exceeds {MAX_INPUT_BYTES:,} bytes.")
    if "\x00" in text:
        raise ValueError("Input must be text, without NUL bytes.")
    rows: list[tuple[dict, int, int]] = []
    if format == "csv":
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), strict=True)
        if not reader.fieldnames:
            raise ValueError("CSV needs a header with a requirement or text column.")
        names = [str(x).strip().lower() for x in reader.fieldnames]
        if len(names) != len(set(names)):
            raise ValueError("CSV contains duplicate column names.")
        if not any(x in names for x in ("requirement", "text", "statement", "description")):
            raise ValueError("CSV needs a requirement, text, statement, or description column.")
        previous_line = 1
        try:
            for row in reader:
                if None in row:
                    raise ValueError("A CSV row has extra fields; quote requirement text containing commas.")
                normalized = {str(k).strip().lower(): v for k, v in row.items()}
                rows.append((normalized, previous_line + 1, reader.line_num))
                previous_line = reader.line_num
        except csv.Error as exc:
            raise ValueError(f"Malformed CSV: {exc}") from exc
    elif format == "json":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSON: {exc.msg} at line {exc.lineno}.") from exc
        if isinstance(value, dict):
            value = value.get("requirements")
        if not isinstance(value, list):
            raise ValueError("JSON must be an array or an object containing a requirements array.")
        for i, row in enumerate(value, 1):
            if isinstance(row, str):
                row = {"text": row}
            if not isinstance(row, dict):
                raise ValueError(f"Requirement {i} must be an object or a string.")
            rows.append((row, i, i))
    elif format == "text":
        for i, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            line = re.sub(r"^[-*•]\s+", "", line)
            match = re.match(r"^([A-Za-z][A-Za-z0-9_.-]*|\d+)[.):]\s+(.+)$", line)
            rows.append(({"id": match[1], "text": match[2]} if match else {"text": line}, i, i))
    else:
        raise ValueError("Choose text, csv, or json.")
    if not 1 <= len(rows) <= MAX_REQUIREMENTS:
        raise ValueError(f"Found {len(rows):,} requirements; provide between 1 and {MAX_REQUIREMENTS:,} requirements per run.")
    result, seen = [], set()
    for i, (row, line_start, line_end) in enumerate(rows, 1):
        clause = next((row[k] for k in ("text", "requirement", "statement", "description") if row.get(k)), None)
        if not isinstance(clause, str) or not clause.strip():
            raise ValueError(f"Requirement {i} has no text.")
        clause = clause.strip()
        if len(clause) > MAX_REQUIREMENT_CHARS:
            raise ValueError(f"Requirement {i} is too long; use one obligation per row.")
        req_id = str(row.get("id") or f"REQ-{i:03d}").strip()
        if not req_id or len(req_id) > 120 or any(ord(c) < 32 for c in req_id):
            raise ValueError(f"Requirement {i} has an invalid identifier.")
        if req_id in seen:
            raise ValueError(f"Duplicate requirement identifier: {req_id}.")
        seen.add(req_id)
        provenance = row.get("source")
        if isinstance(provenance, dict):
            # Preserve citations as user-supplied metadata; retain the actual input location separately.
            source = {"document": str(provenance.get("document") or name),
                      "location": str(provenance.get("location") or f"item {i}")}
        else:
            source = {"document": name, "location": str(provenance or (f"item {i}" if format == "json" else f"line {line_start}"))}
        source.update({"line_start": line_start, "line_end": line_end,
                       "input_location": f"item {i}" if format == "json" else f"lines {line_start}–{line_end}",
                       "provenance_status": "User supplied; authority not independently established."})
        result.append({"id": req_id, "text": clause, "source": source,
                       "owner": str(row["owner"]) if row.get("owner") else None,
                       "authority": str(row["authority"]) if row.get("authority") else None})
    return result


def _canonical_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if value == 0 else text


def interpret(requirements: list[dict]) -> dict:
    result = []
    for req in requirements:
        entry = {**req, "status": "unsupported", "kind": "uninterpreted", "assumptions": [],
                 "reason": "Outside the local grammar. Preserve and review this clause, or use the existing Codex pipeline."}
        text = req["text"].strip().removesuffix(".")
        if re.search(r"\b(and|or|if|unless|when|while|until|except|only)\b", text, re.I):
            entry["reason"] = "Compound clauses or conditional applicability need an explicit interpretation; they are not silently simplified."
            result.append(entry)
            continue
        match = TIMING.fullmatch(text)
        kind = "timing" if match else "quantity"
        match = match or QUANTITY.fullmatch(text) or POSSESSIVE.fullmatch(text) or DOT_QUANTITY.fullmatch(text)
        if not match:
            result.append(entry)
            continue
        parts = match.groupdict()
        unit_text = parts.get("unit") or "unitless"
        unit_key = unit_text if unit_text in UNIT_MAP else unit_text.lower() if unit_text.lower() in UNIT_WORDS else ""
        if unit_key not in UNIT_MAP:
            entry["reason"] = f"Unsupported unit {unit_text!r}; no unit conversion was assumed."
            result.append(entry)
            continue
        if kind == "quantity" and parts["op"].lower() in {"within", "no earlier than", "before", "later than"}:
            entry["reason"] = "Timing-specific comparison used for a scalar quantity; clarify the intended bound."
            result.append(entry)
            continue
        unit, factor = UNIT_MAP[unit_key]
        with localcontext() as ctx:
            ctx.prec = 80
            numeric = Decimal(parts["value"]) * factor
        if numeric.copy_abs() > Decimal("1e12") or len(parts["value"]) > 30:
            entry["reason"] = "Number is outside this prototype's supported magnitude/precision."
            result.append(entry)
            continue
        if kind == "timing" and unit != "s":
            entry["reason"] = "Response timing requires a supported time unit."
            result.append(entry)
            continue
        subject = _canonical_name(parts["subject"])
        trigger = _canonical_name(parts.get("trigger", ""))
        response = _canonical_name(parts.get("response", ""))
        quantity = f"{response} delay after {trigger}" if kind == "timing" else _canonical_name(parts["quantity"])
        key = json.dumps([subject, quantity, trigger, response], ensure_ascii=True)
        stem = re.sub(r"[^a-z0-9]+", "_", f"{subject}_{quantity}").strip("_")[:55]
        symbol = f"v_{stem}_{digest(key)[:8]}"
        assumptions = ["Names denote the same quantity only when subject, property, response, and trigger names match.",
                       "This clause applies without an unstated operating-mode exception.",
                       f"The quantity is a real scalar in {unit}; the source comparison determines whether endpoints are inclusive."]
        if kind == "timing":
            assumptions.extend([f"A {response} occurrence is required for each {trigger} command.",
                                "Time is measured from reception of the named command to the named response occurrence.",
                                "Elapsed time is nonnegative; repeated command instances are represented by the same per-instance bound."])
        entry.update(status="supported", kind=kind, reason="Recognized by the local constraint grammar; source fidelity awaits engineer review.",
                     subject=subject, quantity=quantity, symbol=symbol, unit=unit, relation=OPS[parts["op"].lower()],
                     value=_decimal(numeric), original_value=parts["value"], original_unit=unit_text,
                     trigger=trigger, response=response, context="all operating contexts", assumptions=assumptions)
        result.append(entry)
    dimensions: dict[str, set] = {}
    for req in result:
        if req["status"] == "supported":
            dimensions.setdefault(req["symbol"], set()).add(req["unit"])
    for req in result:
        if req["status"] == "supported" and len(dimensions[req["symbol"]]) > 1:
            req.update(status="unsupported", reason="The same named quantity has incompatible dimensions across requirements; resolve its units before analysis.")
    symbols = {}
    for req in result:
        if req["status"] == "supported":
            symbols[req["symbol"]] = {"name": req["symbol"], "type": "Real", "unit": req["unit"],
                                     "subject": req["subject"], "quantity": req["quantity"],
                                     **({"minimum": "0"} if req["kind"] == "timing" else {})}
    return {"schema": "review_tlr/1", "requirements": result, "symbols": list(symbols.values()),
            "profile": "Conjunctive scalar bounds and per-command response delays; explicit source review required."}


def existing_contract(tlr: dict, source_hash: str, path: Path) -> dict:
    """Expose the same exact bounds through the existing requirements-centric contract."""
    symbol_table = [{**s, "role": "value"} for s in tlr["symbols"]]
    symbol_map = {s["name"]: s for s in symbol_table}
    rows = []
    for req in tlr["requirements"]:
        supported = req["status"] == "supported"
        ranges = []
        if supported:
            relation = req["relation"]
            ranges = [{"symbol": req["symbol"], "lower": req["value"] if relation in ("ge", "gt", "eq") else None,
                       "upper": req["value"] if relation in ("le", "lt", "eq") else None,
                       "lower_inclusive": relation in ("ge", "eq"), "upper_inclusive": relation in ("le", "eq"),
                       "unit": req["unit"], "origin_text": req["text"]}]
        rows.append({**req, "category": "performance" if supported else "other", "temporal_kind": "invariant",
                     "symbols": [symbol_map[req["symbol"]]] if supported else [], "ranges": ranges,
                     "source_span": {"line_start": req["source"]["line_start"], "line_end": req["source"]["line_end"]}})
    result = ensure_requirements_tlf_contract({"schema_version": "1.0", "source": {"statement_path": str(path), "statement_sha256": source_hash},
                                               "requirements": rows, "symbol_table": symbol_table,
                                               "traceability": [{"requirement_id": r["id"], "source_span": r["source_span"]} for r in rows]})
    pipeline._validate_tlf_schema(result)
    result["typecheck"] = pipeline._native_typecheck_tlf(result)
    if not result["typecheck"]["ok"]:
        raise ValueError("The existing TLR contract typecheck rejected the interpreted bounds.")
    return result


def _smt_number(value: str) -> str:
    return f"(- {value[1:]})" if value.startswith("-") else value


def _sexpr_text(value: Any) -> str:
    return "(" + " ".join(_sexpr_text(x) for x in value) + ")" if isinstance(value, list) else str(value)


def analyze(tlr: dict, run_dir: Path) -> dict:
    supported = [r for r in tlr["requirements"] if r["status"] == "supported"]
    unsupported = [r["id"] for r in tlr["requirements"] if r["status"] != "supported"]
    common = {"checked_ids": [r["id"] for r in supported], "unsupported_ids": unsupported,
              "scope": "Consistency of the explicitly supported scalar constraints and required-response bounds. No complete behavioral or liveness proof.",
              "assumptions": list(dict.fromkeys(a for r in supported for a in r["assumptions"])),
              "unsat_core": [], "witness": {}, "diagnostics": "", "source_fidelity": "pending engineer review"}
    if not supported:
        return {**common, "status": "not_run", "solver_status": "not_run", "summary": "No requirement is in the local formal grammar. All original clauses remain in the model for review."}
    lines = ["(set-logic QF_LRA)", "(set-option :produce-unsat-cores true)", "(set-option :produce-models true)"]
    labels, background = {}, {}
    for i, symbol in enumerate(tlr["symbols"]):
        lines.append(f"(declare-const {symbol['name']} Real)")
        if "minimum" in symbol:
            label = f"assumption_{i}"
            background[label] = f"{symbol['name']} >= {symbol['minimum']} {symbol['unit']} (elapsed time)"
            lines.append(f"(assert (! (>= {symbol['name']} {_smt_number(symbol['minimum'])}) :named {label}))")
    relation_map = {"le": "<=", "ge": ">=", "lt": "<", "gt": ">", "eq": "="}
    for i, req in enumerate(supported):
        label = f"req_{i}"
        labels[label] = req["id"]
        lines.append(f"(assert (! ({relation_map[req['relation']]} {req['symbol']} {_smt_number(req['value'])}) :named {label}))")
    base = "\n".join(lines) + "\n(check-sat)\n"
    (run_dir / "constraints.smt2").write_text(base)
    query = base
    first = pipeline.run_z3_fragment(query)
    verdict = pipeline._solver_verdict(first) or "unknown"
    result = first
    if verdict in ("sat", "unsat"):
        extra = "(get-unsat-core)\n" if verdict == "unsat" else "(get-value (" + " ".join(s["name"] for s in tlr["symbols"]) + "))\n"
        query = base + extra
        result = pipeline.run_z3_fragment(query)
        (run_dir / "constraints.smt2").write_text(query)
        if pipeline._solver_verdict(result) != verdict:
            verdict = "unknown"
    (run_dir / "solver_result.json").write_text(json.dumps({"initial": first, "evidence": result}, indent=2))
    output = result.get("result", "")
    diagnostics = result.get("diagnostics") or result.get("stderr") or ""
    cross_check = result.get("cross_check", {})
    if cross_check and not cross_check.get("agree"):
        diagnostics += "\nRequested solver cross-check was inconclusive or disagreed."
        verdict = "unknown"
    common.update(solver_status=verdict, solver=result.get("solver", "unknown"), diagnostics=diagnostics,
                  smt_sha256=digest(query),
                  evidence_text=output, background_assumptions=background, core_kind="Solver-reported conflicting subset; not a preferred correction or guaranteed minimum core.")
    if verdict in ("sat", "unsat"):
        try:
            evidence = pipeline._parse_sexpr("\n".join(output.splitlines()[1:]))[0]
            if verdict == "unsat":
                common["unsat_core"] = [labels[x] for x in evidence if x in labels]
                common["core_assumptions"] = [background[x] for x in evidence if x in background]
            else:
                units = {s["name"]: s["unit"] for s in tlr["symbols"]}
                common["witness"] = {item[0]: {"value": _sexpr_text(item[1]), "unit": units[item[0]]} for item in evidence if isinstance(item, list) and len(item) == 2 and item[0] in units}
        except (ValueError, TypeError, KeyError, IndexError):
            common["diagnostics"] += "\nStructured evidence could not be decoded; inspect the saved raw solver result."
    status = "partial" if verdict == "sat" and unsupported else verdict
    summary = {"sat": "The supported requirements have a satisfying assignment under the recorded interpretation.",
               "partial": "The encoded subset is satisfiable; unencoded requirements still need interpretation and checking.",
               "unsat": "The encoded requirements conflict under the recorded assumptions. The core identifies clauses to investigate, not which clause is wrong.",
               "unknown": "The solver did not establish a usable verdict; inspect the diagnostics."}[status]
    return {**common, "status": status, "summary": summary}
