#!/usr/bin/env python3
"""Reproducible, source-linked mutation campaigns; no model approval is implied."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import time

from mutation_core import compare_formulas, validate_context, validate_formula

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = {
    "required_response_suppression", "operating_guard_removal",
    "value_binding_mismatch", "limit_violation", "equivalent_control",
}
RELATIONS = {"weakened", "strengthened", "changed", "equivalent"}
OPERATORS = {
    "suppress_response": "required_response_suppression",
    "remove_guard": "operating_guard_removal",
    "replace_value": "value_binding_mismatch",
    "replace_binding": "value_binding_mismatch",
    "shift_bound": "limit_violation", "flip_boundary": "limit_violation",
    "double_negation": "equivalent_control", "reverse_conjunction": "equivalent_control",
}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def read_json(path):
    def unique(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValueError(f"Duplicate JSON key: {k}")
            out[k] = v
        return out
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique,
                      parse_constant=lambda s: (_ for _ in ()).throw(ValueError(f"Invalid JSON constant: {s}")))


def fields(obj, allowed, required, label):
    if not isinstance(obj, dict) or set(obj) - set(allowed) or set(required) - set(obj):
        raise ValueError(f"{label}: missing or unsupported fields")


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError(f"{label} must be a safe ID (letters, digits, hyphens, underscores; max 80)")
    return value


def apply_mutation(formula, mutation):
    """Apply one explicit AST edit. No inferred or random source changes."""
    fields(mutation, {"operator", "path", "value"}, {"operator"}, "mutation")
    op = mutation["operator"]
    if op not in OPERATORS and op != "explicit":
        raise ValueError(f"Unknown mutation operator: {op}")
    needs_value = op in {"replace_value", "replace_binding", "shift_bound", "explicit"}
    if ("value" in mutation) != needs_value:
        raise ValueError(f"{op}: {'requires' if needs_value else 'does not accept'} value")
    path = mutation.get("path", [])
    if not isinstance(path, list) or len(path) > 32:
        raise ValueError("Mutation path must be a short list of keys/indices")
    result = deepcopy(formula)
    node = result
    parent, key = None, None
    for key in path:
        parent = node
        if isinstance(node, dict) and isinstance(key, str) and key in node:
            node = node[key]
        elif isinstance(node, list) and type(key) is int and 0 <= key < len(node):
            node = node[key]
        else:
            raise ValueError("Mutation path does not identify an AST node")
    if not isinstance(node, (dict, bool)):
        raise ValueError("Mutation must select an expression, not a string or argument list")
    changed = deepcopy(node)
    if op in {"suppress_response", "remove_guard"}:
        if not isinstance(node, dict) or node.get("op") != "implies" or len(node.get("args", [])) != 2:
            raise ValueError(f"{op} requires a conditional requirement")
        changed = True if op == "suppress_response" else deepcopy(node["args"][1])
    elif op == "replace_value":
        if not isinstance(node, dict) or "value" not in node:
            raise ValueError("replace_value requires a numeric literal")
        changed["value"] = mutation["value"]
    elif op == "replace_binding":
        if not isinstance(node, dict) or "var" not in node:
            raise ValueError("replace_binding requires a variable reference")
        changed["var"] = mutation["value"]
    elif op in {"shift_bound", "flip_boundary"}:
        if not isinstance(node, dict) or node.get("op") not in {"<", "<=", ">", ">="}:
            raise ValueError(f"{op} requires an ordered comparison")
        if op == "flip_boundary":
            changed["op"] = {"<": "<=", "<=": "<", ">": ">=", ">=": ">"}[node["op"]]
        else:
            rhs = node["args"][1]
            if not isinstance(rhs, dict) or "value" not in rhs:
                raise ValueError("shift_bound requires a right-hand numeric literal")
            from review_behavior import _num
            delta = Decimal(_num(mutation["value"]))
            with localcontext() as arithmetic:
                arithmetic.prec = 80
                changed["args"][1]["value"] = format(Decimal(rhs["value"]) + delta, "f")
    elif op == "double_negation":
        changed = {"op": "not", "args": [{"op": "not", "args": [deepcopy(node)]}]}
    elif op == "reverse_conjunction":
        if not isinstance(node, dict) or node.get("op") != "and":
            raise ValueError("reverse_conjunction requires an and expression")
        changed["args"] = list(reversed(node["args"]))
    elif op == "explicit":
        changed = deepcopy(mutation["value"])
    if parent is None:
        return changed
    parent[key] = changed
    return result


def validate_manifest(payload):
    fields(payload, {"schema", "id", "reference", "context", "requirements", "variants"},
           {"schema", "id", "context", "requirements", "variants"}, "manifest")
    if payload["schema"] != "mutation_campaign/1":
        raise ValueError("Manifest schema must be mutation_campaign/1")
    identifier(payload["id"], "campaign ID")
    reference = payload.get("reference", {})
    fields(reference, {"status", "author", "description"}, set(), "reference")
    for key, value in reference.items():
        if not isinstance(value, str):
            raise ValueError(f"reference.{key} must be a string")
    reference = {"status": "unspecified", **reference}
    fields(payload["context"], {"variables", "background"}, {"variables", "background"}, "context")
    context = validate_context(**payload["context"])
    if not isinstance(payload["requirements"], list) or not 1 <= len(payload["requirements"]) <= 500:
        raise ValueError("Provide 1 to 500 source requirements")
    requirements, by_id = [], {}
    for raw in payload["requirements"]:
        fields(raw, {"id", "text", "source", "formula", "unsupported_reason", "binding"},
               {"id", "text", "source", "formula"}, "requirement")
        rid = identifier(raw["id"], "requirement ID")
        if rid in by_id:
            raise ValueError(f"Duplicate requirement ID: {rid}")
        nonempty(raw["text"], f"{rid} text")
        if not isinstance(raw["source"], dict) or not raw["source"]:
            raise ValueError(f"{rid}: preserve source document/location metadata")
        item = deepcopy(raw)
        if raw["formula"] is None:
            nonempty(raw.get("unsupported_reason"), f"{rid} unsupported reason")
        else:
            item["formula"] = validate_formula(raw["formula"], context)
        if "binding" in raw:
            fields(raw["binding"], {"variable", "subject", "quantity", "kind", "trigger", "response"},
                   {"variable", "subject", "quantity"}, "scalar extraction binding")
            if raw["binding"]["variable"] not in {v["name"] for v in context["variables"]}:
                raise ValueError("Scalar extraction binding uses an undeclared variable")
            for k, v in raw["binding"].items():
                if not isinstance(v, str):
                    raise ValueError(f"binding.{k} must be a string")
        requirements.append(item)
        by_id[rid] = item
    if not isinstance(payload["variants"], list) or not 1 <= len(payload["variants"]) <= 1000:
        raise ValueError("Provide 1 to 1000 explicitly eligible variants")
    variants, seen = [], set()
    for raw in payload["variants"]:
        fields(raw, {"id", "requirement_id", "kind", "category", "eligibility", "rationale", "expected_relation", "mutation", "text"},
               {"id", "requirement_id", "kind", "category", "eligibility", "rationale", "expected_relation", "mutation"}, "variant")
        vid = identifier(raw["id"], "variant ID")
        if vid in seen or vid == "baseline":
            raise ValueError(f"Duplicate or reserved variant ID: {vid}")
        seen.add(vid)
        if raw["requirement_id"] not in by_id:
            raise ValueError(f"Unknown variant requirement: {raw['requirement_id']}")
        if raw["kind"] not in {"mutant", "control"} or raw["category"] not in CATEGORIES:
            raise ValueError("Unknown variant kind/category")
        control = raw["kind"] == "control"
        if (raw["category"] == "equivalent_control") != control:
            raise ValueError("Controls require the equivalent_control category")
        if raw["expected_relation"] not in RELATIONS or (raw["expected_relation"] == "equivalent") != control:
            raise ValueError("Controls must expect equivalence; mutants must expect a difference")
        for k in ("eligibility", "rationale"):
            nonempty(raw[k], f"{vid} {k}")
        if "text" in raw:
            nonempty(raw["text"], f"{vid} text")
            if raw["text"] == by_id[raw["requirement_id"]]["text"]:
                raise ValueError("Source variant text must differ from its original")
        canonical = by_id[raw["requirement_id"]]["formula"]
        item = deepcopy(raw)
        fields(raw["mutation"], {"operator", "path", "value"}, {"operator"}, "mutation")
        operator = raw["mutation"]["operator"]
        if operator != "explicit" and OPERATORS.get(operator) != raw["category"]:
            raise ValueError("Operator does not match the declared primary category")
        if canonical is not None:
            validate_formula(apply_mutation(canonical, raw["mutation"]), context)
        else:
            raise ValueError("Do not invent an executable mutant of an unsupported reference")
        variants.append(item)
    return {**deepcopy(payload), "reference": reference, "context": context, "requirements": requirements, "variants": variants}


def _new_output(output, manifest, campaign, configuration):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "manifest.json", manifest)
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        revision = None
    metadata = {"schema": "mutation_execution/1", "campaign": campaign,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "python": platform.python_version(), "git_revision": revision, "configuration": configuration}
    write_json(output / "execution.json", metadata)
    return output, metadata


def summarize(records):
    summary = {}
    for kind in ("mutant", "control"):
        rows = [r for r in records if r["kind"] == kind]
        comparable = [r for r in rows if r.get("comparison", {}).get("classification") in RELATIONS]
        detected = sum(r.get("comparison", {}).get("difference_detected") is True for r in rows)
        expected = sum(r.get("expected_relation_met") is True for r in rows)
        summary[kind] = {"planned": len(rows), "comparable": len(comparable),
                         "inconclusive": len(rows) - len(comparable), "detected": detected,
                         "expected_relation_met": expected,
                         "difference_rate_comparable": (sum(r["comparison"].get("difference_detected") is True for r in comparable) / len(comparable)) if comparable else None,
                         "operational_detection_yield": detected / len(rows) if rows else None}
        if kind == "control":
            summary[kind]["false_alarms"] = detected
    summary["by_category"] = {}
    for category in sorted({r["category"] for r in records}):
        rows = [r for r in records if r["category"] == category]
        summary["by_category"][category] = {"planned": len(rows),
            "detected": sum(r.get("comparison", {}).get("difference_detected") is True for r in rows),
            "conclusive": sum(r.get("comparison", {}).get("classification") in RELATIONS for r in rows)}
    return summary


def _record(variant, comparison, **extra):
    classification = comparison.get("classification", "inconclusive")
    return {"variant_id": variant["id"], "requirement_id": variant["requirement_id"],
            "kind": variant["kind"], "category": variant["category"],
            "expected_relation": variant["expected_relation"], "comparison": comparison,
            "expected_relation_met": classification == variant["expected_relation"] if classification in RELATIONS else None,
            **extra}


def _finish(output, manifest, metadata, records, **extra):
    report = {"schema": "mutation_report/1", "status": "completed", "campaign": metadata["campaign"],
              "reference": manifest["reference"], "comparisons": records, "summary": summarize(records),
              "source_inventory": [{"requirement_id": r["id"], "source": r["source"],
                                    "status": "unsupported" if r["formula"] is None else "executable_reference",
                                    "unsupported_reason": r.get("unsupported_reason")} for r in manifest["requirements"]],
              "limitations": ["Constructed variants are not independent source cases or observed engineering defects.",
                  "Formal difference under this reference/context does not establish fidelity to stakeholder intent.",
                  "Offline comparison findings do not count as the runtime pipeline's detections.",
                  "Static quantifier-free linear arithmetic only; temporal and probabilistic semantics are unsupported."], **extra}
    if metadata["campaign"] != "formal":
        report["summary"]["control"].pop("false_alarms", None)
        report["summary"]["comparison_basis"] = "Canonical reference versus generated target; differences may predate the mutation. See source_metrics for intended-change preservation and baseline-stratified controls."
    write_json(output / "report.json", report)
    lines = ["# Mutation stress report", "", f"Campaign: `{metadata['campaign']}`. Reference: {manifest['reference']['status']}.", "",
             "Counts describe constructed variants/trials; they are not measures of source fidelity.", "",
             "| Variant | Requirement | Kind | Relation | Difference | Expected relation |", "|---|---|---|---|---|---|"]
    for row in records:
        comp = row["comparison"]
        lines.append(f"| {row['variant_id']} | {row['requirement_id']} | {row['kind']} | {comp.get('classification', 'inconclusive')} | {comp.get('difference_detected')} | {row['expected_relation_met']} |")
    lines.extend(["", "## Counts", "", "```json", json.dumps(report["summary"], indent=2), "```", "",
                  "Inspect report.json for inconclusive outcomes, prerequisites, attribution, and evidence paths.", ""])
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return report


def run_formal(manifest, output, timeout_seconds=10.0, solver="z3"):
    manifest = validate_manifest(manifest)
    output, metadata = _new_output(output, manifest, "formal", {"solver": solver, "timeout_seconds": timeout_seconds, "provider_calls": 0})
    by_id = {r["id"]: r for r in manifest["requirements"]}
    records = []
    for variant in manifest["variants"]:
        original = by_id[variant["requirement_id"]]["formula"]
        candidate = apply_mutation(original, variant["mutation"])
        directory = output / "comparisons" / variant["id"]
        comparison = compare_formulas(manifest["context"], original, candidate, directory, timeout_seconds, solver)
        records.append(_record(variant, comparison, evidence_directory=str(directory.relative_to(output))))
        write_json(output / "progress.json", {"completed": len(records), "planned": len(manifest["variants"])})
    return _finish(output, manifest, metadata, records)



def run_source(*args, **kwargs):
    from mutation_sources import run_source as execute
    return execute(*args, **kwargs)


def run_replay(*args, **kwargs):
    from mutation_sources import run_replay as execute
    return execute(*args, **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "formal", "source", "replay"):
        sub = subs.add_parser(command)
        sub.add_argument("--manifest", type=Path, required=True)
        if command != "validate":
            sub.add_argument("--output", type=Path, required=True, help="New evidence directory; never overwritten")
            sub.add_argument("--timeout-seconds", type=float, default=10.0)
            sub.add_argument("--solver", default="z3", help="Local Z3 executable")
        if command == "source":
            sub.add_argument("--engine", choices=("pipeline", "local"), default="pipeline",
                             help="LLM pipeline by default; local retains legacy fixture compatibility")
            sub.add_argument("--model", help="Generation model for the LLM pipeline")
            sub.add_argument("--repetitions", type=int, default=1)
            sub.add_argument("--max-generations", type=int, default=20)
            sub.add_argument("--generation-timeout-seconds", type=float, default=600)
        if command == "replay":
            sub.add_argument("--candidates", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = validate_manifest(read_json(args.manifest))
        if args.command == "validate":
            result = {"status": "valid",
                      "requirements": len(manifest["requirements"]), "variants": len(manifest["variants"]),
                      "note": "Schema/type validation only; run formal to check expected semantic relations."}
        else:
            if not 0 < args.timeout_seconds <= 3600:
                raise ValueError("Solver timeout must be positive and at most 3600 seconds")
            options = {"timeout_seconds": args.timeout_seconds, "solver": args.solver}
            if args.command == "formal":
                result = run_formal(manifest, args.output, **options)
            elif args.command == "source":
                result = run_source(manifest, args.output, engine=args.engine, repetitions=args.repetitions,
                                    max_generations=args.max_generations,
                                    generation_timeout_seconds=args.generation_timeout_seconds, model=args.model, **options)
            else:
                result = run_replay(manifest, read_json(args.candidates), args.output, **options)
            result = {"status": result["status"], "report": str(args.output / "report.json"), "summary": result["summary"]}
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(f"Mutation campaign error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
