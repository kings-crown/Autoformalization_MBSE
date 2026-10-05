#!/usr/bin/env python3
"""Matched source-consistency trials with evaluator-only reference formulas.

This campaign exercises condition C; its clean/fault labels are input treatments,
not the A/B/C generation arms. Reference checks and final judges cannot repair
the converter. No candidate or source packet is silently resampled.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

from canonical_cli import read_json, write_json, _fixed_context, run_candidate
from mutation_core import check_consistency, compare_formulas, validate_formula

SCHEMA = "source_issue_campaign/1"


def conjunction(formulas):
    if not formulas:
        return True
    if len(formulas) == 1:
        return deepcopy(formulas[0])
    # The shared AST limits each conjunction to 16 arguments.
    return {"op": "and", "args": [conjunction(formulas[:len(formulas)//2]),
                                    conjunction(formulas[len(formulas)//2:])]}


def reference_context(manifest):
    return {k: deepcopy(manifest["context"][k]) for k in ("variables", "background")}


def reference_rows(manifest, variant=None):
    rows = deepcopy(manifest["baseline"]["requirements"])
    if variant:
        if variant["operation"] == "append":
            rows.append(deepcopy(variant["requirement"]))
        else:
            rows = [deepcopy(variant["requirement"]) if r["id"] == variant["target_requirement_id"] else r
                    for r in rows]
    return rows


def source_packet(manifest, variant=None):
    """Allowlist the converter input; never forward formulas or expected labels."""
    return [{k: deepcopy(row[k]) for k in ("id", "text", "source")}
            for row in reference_rows(manifest, variant)]


def validate_manifest(raw):
    manifest = deepcopy(raw)
    if manifest.get("schema") != SCHEMA:
        raise ValueError(f"Expected {SCHEMA}")
    if not isinstance(manifest.get("id"), str) or not manifest["id"].strip():
        raise ValueError("A campaign ID is required")
    manifest["context"] = _fixed_context(manifest.get("context"))
    if manifest["context"] is None:
        raise ValueError("A fixed comparison vocabulary/background is required")
    context = reference_context(manifest)
    baseline = manifest.get("baseline", {})
    rows = baseline.get("requirements")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("Baseline needs 1..100 source requirements")
    if baseline.get("expected_status") != "sat":
        raise ValueError("The clean baseline must declare SAT")
    variants = manifest.get("variants")
    if not isinstance(variants, list) or not 1 <= len(variants) <= 100:
        raise ValueError("Provide 1..100 variants")

    def row_check(row):
        if not isinstance(row, dict) or set(row) != {"id", "text", "source", "formula"}:
            raise ValueError("Each reference requirement needs exactly id/text/source/formula")
        if any(not isinstance(row[k], str) or not row[k].strip() for k in ("id", "text")):
            raise ValueError("Nonempty requirement ID and source text are required")
        if not isinstance(row["source"], dict):
            raise ValueError("Source metadata must be an object")
        row["formula"] = validate_formula(row["formula"], context)
        # Prevent accidental answer leakage in nested source metadata.
        def inspect(value):
            if isinstance(value, dict):
                if set(value) & {"formula", "expected_status", "expected_conflict_ids", "expected_answer",
                                 "expected_formula", "reference_formula", "canonical_formula", "expected_classification",
                                 "ground_truth", "treatment", "mutation_kind", "fault_category"}:
                    raise ValueError("Evaluation answers cannot appear in source metadata")
                for child in value.values():
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)
        inspect(row["source"])

    for row in rows:
        row_check(row)
    ids = {row["id"] for row in rows}
    if len(ids) != len(rows):
        raise ValueError("Baseline source IDs must be unique")
    seen = {"baseline"}
    from canonical_obligations import source_registry
    source_registry(source_packet(manifest))
    for variant in variants:
        vid = variant.get("id")
        if not isinstance(vid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", vid) or vid in seen:
            raise ValueError("Variant IDs must be unique safe names")
        seen.add(vid)
        if variant.get("operation") not in {"append", "replace"}:
            raise ValueError("Mutation operation must be append or replace")
        if variant.get("kind") not in {"conflict", "consistent_control"}:
            raise ValueError("Unknown input treatment")
        expected = "unsat" if variant["kind"] == "conflict" else "sat"
        if variant.get("expected_status") != expected:
            raise ValueError("Treatment and expected status disagree")
        if variant.get("target_requirement_id") not in ids:
            raise ValueError("The mutation target must be a baseline requirement")
        row_check(variant["requirement"])
        rid = variant["requirement"]["id"]
        if ((variant["operation"] == "append" and rid in ids) or
                (variant["operation"] == "replace" and rid != variant["target_requirement_id"])):
            raise ValueError("Appends need a new ID; replacements preserve the target ID")
        packet = source_packet(manifest, variant)
        source_registry(packet)
        core = variant.get("expected_conflict_ids", [])
        if (not isinstance(core, list) or len(core) != len(set(core)) or
                not set(core) <= {r["id"] for r in packet} or
                (expected == "unsat" and len(core) < 2) or (expected == "sat" and core)):
            raise ValueError("Conflict labels need at least two distinct source IDs; controls need none")
        if variant["operation"] == "replace":
            original = next(r["text"] for r in rows if r["id"] == rid)
            # Repeated source quotations must be deliberately revised or removed.
            def stale(value):
                if isinstance(value, str):
                    return original in value
                if isinstance(value, dict):
                    return any(stale(v) for v in value.values())
                if isinstance(value, list):
                    return any(stale(v) for v in value)
                return False
            if original != variant["requirement"]["text"] and any(stale(r["source"]) for r in packet):
                raise ValueError("Replacement retains a stale original quotation in source metadata")
    return manifest


def preflight(manifest, output_dir, *, solver="z3", timeout_seconds=10):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    context = reference_context(manifest)
    def query(label, formulas):
        return check_consistency(context, conjunction(formulas), directory / label, timeout_seconds, solver)
    background = query("background", [])
    baseline = query("baseline", [r["formula"] for r in reference_rows(manifest)])
    valid = background["status"] == baseline["status"] == "sat"
    checks = []
    for index, variant in enumerate(manifest["variants"], 1):
        rows = reference_rows(manifest, variant)
        tested = query(f"variant-{index:03d}", [r["formula"] for r in rows])
        restored = query(f"restored-{index:03d}", [r["formula"] for r in reference_rows(manifest)])
        core = variant.get("expected_conflict_ids", [])
        core_check, deletions = None, []
        if core:
            selected = [r for r in rows if r["id"] in core]
            core_check = query(f"core-{index:03d}", [r["formula"] for r in selected])
            for j, row in enumerate(selected):
                check = query(f"core-{index:03d}-without-{j:03d}",
                              [r["formula"] for r in selected if r["id"] != row["id"]])
                deletions.append({"removed_id": row["id"], "result": check})
        passed = (tested["status"] == variant["expected_status"] and restored["status"] == "sat" and
                  (not core or core_check["status"] == "unsat" and all(d["result"]["status"] == "sat" for d in deletions)))
        valid = valid and passed
        checks.append({"variant_id": variant["id"], "passed": passed, "variant": tested,
                       "restoration": restored, "expected_core": core_check, "core_deletions": deletions})
    result = {"schema": "source_issue_preflight/1", "status": "passed" if valid else "failed",
              "background": background, "baseline": baseline, "variants": checks,
              "claim": "Authored reference fixtures have the declared logical behavior; this does not independently validate their English interpretation."}
    write_json(directory / "report.json", result)
    return result


def audited_requirement_ids(result):
    """Executed coverage, preserving historical gates when replaying old results.

    Current runs audit every supported row. An old gated audit can contain only
    a subset, so prefer its recorded executed IDs over candidate support alone.
    These IDs describe representation coverage, never assessed source fidelity.
    """
    audit = result.get("analysis") or {}
    if "supported_requirement_ids" in audit:
        return set(audit["supported_requirement_ids"])
    review = result.get("source_review") or {}
    if "eligible_ids" in review:
        return set(review["eligible_ids"])
    from canonical_tlr import validate_tlr
    try:
        tlr = validate_tlr(result.get("tlr"))
    except (ValueError, TypeError, KeyError):
        return set()
    return {row["id"] for row in tlr["requirements"] if row["status"] == "supported"}


def score_candidate(manifest, variant, result, output_dir, *, solver="z3", timeout_seconds=10):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    rows = reference_rows(manifest, variant)
    expected = variant["expected_status"] if variant else "sat"
    expected_ids = set(variant.get("expected_conflict_ids", [])) if variant else set()
    audit = result.get("analysis") or {}
    eligible = audited_requirement_ids(result)
    required = {r["id"] for r in rows}
    consistency = audit.get("consistency_status", "not_run")
    background = audit.get("background_status", "not_run")
    core = audit.get("consistency", {}).get("unsat_core", {})
    reported_ids = set(core.get("requirement_ids", [])) if core.get("status") == "available" else set()
    raw_conflict = background == "sat" and consistency == "unsat"
    complete = required == eligible
    if raw_conflict:
        outcome = "detected" if expected == "unsat" else "false_alarm"
    elif consistency == "sat" and complete and background == "sat":
        outcome = "missed" if expected == "unsat" else "correct_negative"
    else:
        outcome = "inconclusive"
    localized = bool(expected_ids) and reported_ids == expected_ids
    context_matches = False
    comparisons = []
    tlr = result.get("tlr")
    scoring_diagnostics = []
    if isinstance(tlr, dict):
        from canonical_tlr import tlr_context
        from mutation_sources import same_context
        try:
            context_matches = same_context(tlr_context(tlr), reference_context(manifest))
        except (ValueError, TypeError, KeyError) as exc:
            scoring_diagnostics.append(f"Candidate context unavailable: {exc}")
    candidate = {r["id"]: r for r in tlr["requirements"]} if context_matches else {}
    for index, row in enumerate(rows):
        proposed = candidate.get(row["id"], {})
        if context_matches and proposed.get("status") == "supported":
            comparison = compare_formulas(reference_context(manifest), row["formula"], proposed["formula"],
                                          directory / f"requirement-{index+1:03d}", timeout_seconds, solver)
            classification = comparison["classification"]
        else:
            classification = "not_comparable"
        comparisons.append({"requirement_id": row["id"], "classification": classification,
                            "eligible": row["id"] in eligible})
    preserved_ids = {r["requirement_id"] for r in comparisons if r["classification"] == "equivalent"}
    report = {"schema": "source_issue_score/1", "expected_status": expected,
              "execution_status": result.get("status"), "background_status": background,
              "consistency_status": consistency, "diagnostic_outcome": outcome,
              "coverage_complete": complete, "eligible_requirement_ids": sorted(eligible),
              "expected_conflict_ids": sorted(expected_ids), "reported_conflict_ids": sorted(reported_ids),
              "exact_localization": localized if expected_ids else None,
              "context_matches": context_matches, "reference_comparisons": comparisons,
              "scoring_diagnostics": scoring_diagnostics,
              "all_formulas_preserved": preserved_ids == required,
              "reference_supported_detection": outcome == "detected" and localized and expected_ids <= preserved_ids & eligible,
              "compilation_status": result.get("compilation", {}).get("status", "not_run"),
              "sysml_available": bool(result.get("model_file")),
              "limitations": ["Formula comparison is under background alone, never an inconsistent requirement conjunction.",
                              "Supported-row coverage and authored references do not establish source fidelity or human-validated ground truth.",
                              "Independent judges assess final SysML and diagnostic explanations separately."]}
    write_json(directory / "score.json", report)
    return report


def summarize(samples):
    scores = [s["score"] for s in samples]
    conflicts = [s for s in scores if s["expected_status"] == "unsat"]
    clean = [s for s in scores if s["expected_status"] == "sat"]
    return {"planned_packets": len(scores), "conflict_packets": len(conflicts), "consistent_packets": len(clean),
            "detected_conflicts": sum(s["diagnostic_outcome"] == "detected" for s in conflicts),
            "reference_supported_detections": sum(s["reference_supported_detection"] for s in conflicts),
            "exact_localizations": sum(s["exact_localization"] is True for s in conflicts),
            "missed_conflicts": sum(s["diagnostic_outcome"] == "missed" for s in conflicts),
            "correct_negatives": sum(s["diagnostic_outcome"] == "correct_negative" for s in clean),
            "false_alarms": sum(s["diagnostic_outcome"] == "false_alarm" for s in clean),
            "inconclusive_packets": sum(s["diagnostic_outcome"] == "inconclusive" for s in scores),
            "all_formulas_preserved": sum(s["all_formulas_preserved"] for s in scores),
            "compiled_models": sum(s["compilation_status"] == "passed" for s in scores)}


def run_campaign(manifest, output_dir, *, model=None, feedback_repairs=2, inventory_repairs=0, format_repairs=0, workers=1,
                 solver="z3", timeout_seconds=10, judge_config=None, runner=None):
    manifest = validate_manifest(manifest)
    if type(workers) is not int or workers not in (1, 2, 3, 4):
        raise ValueError("Use 1..4 independent workflow workers")
    if type(feedback_repairs) is not int or not 0 <= feedback_repairs <= 5:
        raise ValueError("Use 0..5 feedback repair opportunities")
    if type(inventory_repairs) is not int or inventory_repairs != 0:
        raise ValueError("inventory_repairs is historical; new campaigns do not prepare or gate on inventories")
    if type(format_repairs) is not int or format_repairs not in (0, 1):
        raise ValueError("Use 0 or 1 initial response-format corrections")
    if judge_config is not None:
        from bedrock_judging import validate_config
        judge_config = validate_config(judge_config)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "manifest.json", manifest)
    shutil.copytree(Path(__file__).parent, directory / "implementation" / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if judge_config:
        write_json(directory / "judge_config.json", judge_config)
    config = {"condition": "C", "repetitions": 1, "model": model,
              "feedback_repairs": feedback_repairs, "format_repairs": format_repairs,
              "workers": workers, "solver": solver,
              "timeout_seconds": timeout_seconds, "reasoning_effort": os.getenv("CODEX_REASONING_EFFORT", "low"),
              "model_timeout_seconds": os.getenv("CODEX_EXEC_TIMEOUT", "180"),
              "created_at": datetime.now(timezone.utc).isoformat(),
              "fixed_vocabulary": True, "source_fidelity": "not_assessed",
              "candidate_resampling": False, "judge_feedback_into_conversion": False,
              "scope": manifest.get("reference", {}),
              "max_model_transport_invocations_per_packet": 1 + format_repairs + feedback_repairs}
    from review_sysml import compiler_capability
    config.update(python=sys.version, compiler=compiler_capability(),
                  implementation_snapshot="implementation/scripts",
                  generation_usage="Current Codex transport does not expose per-call tokens or cost; recorded as unknown, not zero.")
    write_json(directory / "configuration.json", config)
    checked = preflight(manifest, directory / "reference_checks", solver=solver, timeout_seconds=timeout_seconds)
    if checked["status"] != "passed":
        report = {"schema": "source_issue_campaign_result/1", "status": "reference_preflight_failed", "samples": []}
        write_json(directory / "report.json", report)
        return report
    variants = [None, *manifest["variants"]]
    start = time.monotonic()
    def convert(item):
        index, variant = item
        sample_dir = directory / f"packet-{index:03d}"
        sample_dir.mkdir()
        sources = source_packet(manifest, variant)
        write_json(sample_dir / "sources.json", sources)
        try:
            result = (runner or run_candidate)(sources, sample_dir / "generation", condition="C", model=model,
                context=manifest["context"], name="SourcePacketModel", compile_model=True, solver=solver,
                timeout_seconds=timeout_seconds, feedback_repairs=feedback_repairs, format_repairs=format_repairs)
        except Exception as exc:
            result = {"status": "failed", "analysis": {"status": "not_run"}, "tlr": None,
                      "compilation": {"status": "not_run"}, "errors": [f"{type(exc).__name__}: {exc}"]}
            write_json(sample_dir / "conversion_failure.json", result)
        return {"packet": sample_dir.name, "variant_id": variant["id"] if variant else "baseline",
                "generation": result, "variant": variant}
    # Finish and freeze all conversions before any evaluation reference or judge is consulted.
    samples = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for sample in executor.map(convert, enumerate(variants, 1)):
            samples.append(sample)
            write_json(directory / "progress.json", {"stage": "generation", "collected": len(samples), "planned": len(variants)})
    write_json(directory / "frozen_candidates.json", [{"packet": s["packet"], "result": s["generation"]} for s in samples])
    for sample in samples:
        sample_dir = directory / sample["packet"]
        sample["score"] = score_candidate(manifest, sample.pop("variant"), sample["generation"], sample_dir / "evaluation",
                                          solver=solver, timeout_seconds=timeout_seconds)
    if judge_config:
        from canonical_issue_judging import assess_issue_candidate
        def judge(sample):
            path = directory / sample["packet"]
            model_path = path / "generation" / "model.sysml"
            try:
                sample["judgments"] = assess_issue_candidate(read_json(path / "sources.json"),
                    model_path.read_text(encoding="utf-8") if model_path.exists() else None,
                    sample["generation"].get("analysis", {}), path / "judging", bedrock_config=judge_config)
            except Exception as exc:
                sample["judgments"] = {"status": "failed", "planned_dimensions": 5,
                                       "error": f"{type(exc).__name__}: {exc}"}
                write_json(path / "judging_failure.json", sample["judgments"])
            return sample
        with ThreadPoolExecutor(max_workers=workers) as executor:
            samples = list(executor.map(judge, samples))
    report = {"schema": "source_issue_campaign_result/1", "status": "completed",
              "configuration": config, "summary": summarize(samples), "samples": samples,
              "latency_seconds": time.monotonic()-start}
    write_json(directory / "report.json", report)
    lines = ["# Source inconsistency campaign", "", "One C workflow per packet; clean/conflict are input treatments, not A/B generation conditions.", "",
             "| Packet | Treatment | Logical result | Diagnostic outcome | Exact conflict IDs | Reference formulas preserved | Compiler |",
             "|---|---|---|---|---|---|---|"]
    for sample in samples:
        s = sample["score"]
        lines.append(f"| {sample['packet']} | {sample['variant_id']} | {s['consistency_status']} | {s['diagnostic_outcome']} | {s['exact_localization']} | {s['all_formulas_preserved']} | {s['compilation_status']} |")
    lines += ["", "Failures and partial coverage remain in denominators. Faithful UNSAT is a successful diagnostic; no clause is declared wrong by the solver.",
              "", "References qualify the represented interpretation; final SysML fidelity and explanations are separately LLM-assessed. This is not a proof of source intent or a comparison of A/B/C generation quality."]
    (directory / "report.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--feedback-repairs", type=int, default=2, choices=range(6))
    parser.add_argument("--inventory-repairs", type=int, default=0, choices=(0,), help=argparse.SUPPRESS)
    parser.add_argument("--format-repairs", type=int, default=0, choices=(0, 1))
    parser.add_argument("--workers", type=int, default=1, choices=range(1, 5))
    parser.add_argument("--judge-config", type=Path)
    parser.add_argument("--solver", default="z3")
    parser.add_argument("--timeout-seconds", type=float, default=10)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args(argv)
    manifest = validate_manifest(read_json(args.manifest))
    if args.preflight_only:
        report = preflight(manifest, args.output_dir, solver=args.solver, timeout_seconds=args.timeout_seconds)
    else:
        report = run_campaign(manifest, args.output_dir, model=args.model, feedback_repairs=args.feedback_repairs,
            inventory_repairs=args.inventory_repairs, format_repairs=args.format_repairs,
            workers=args.workers, solver=args.solver, timeout_seconds=args.timeout_seconds,
            judge_config=read_json(args.judge_config) if args.judge_config else None)
    print(json.dumps({"status": report["status"], "summary": report.get("summary"), "output_dir": str(args.output_dir)}, indent=2))
    return 0 if report["status"] in {"passed", "completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
