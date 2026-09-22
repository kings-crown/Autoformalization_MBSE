"""Source regeneration and explicit normalized-candidate replay for mutation studies."""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from mutation_core import check_consistency, compare_formulas, validate_context, validate_formula
from mutation_stress import (ROOT, RELATIONS, _finish, _new_output, _record, apply_mutation,
                             fields, nonempty, read_json, validate_manifest, write_json)


def same_context(candidate, reference):
    """Compare normalized vocabulary and premises directly, ignoring record order."""
    fields(candidate, {"variables", "background"}, {"variables", "background"}, "comparison context")
    normalized = validate_context(**candidate)
    expected = validate_context(**reference)
    def ordered(context):
        return {"variables": sorted(context["variables"], key=lambda row: row["name"]),
                "background": sorted(context["background"], key=lambda row: row["id"])}
    return ordered(normalized) == ordered(expected)


def source_packet(manifest, variant=None):
    packet = [{k: deepcopy(r[k]) for k in ("id", "text", "source")} for r in manifest["requirements"]]
    if variant is not None:
        for requirement in packet:
            if requirement["id"] == variant["requirement_id"]:
                requirement["text"] = variant["text"]
    return packet


def _unavailable(status, reason):
    return {"status": status, "classification": "inconclusive", "difference_detected": None, "diagnostic": reason}


def _compare_candidate(manifest, canonical, candidate, directory, timeout_seconds, solver, reason=None):
    if canonical is None or candidate is None:
        status = "not_comparable" if reason and reason.startswith("Comparison context") else "unsupported"
        return _unavailable(status, reason or "No executable formula available on both sides")
    return compare_formulas(manifest["context"], canonical, candidate, directory, timeout_seconds, solver)


def validate_candidates(manifest, payload):
    fields(payload, {"schema", "manifest_sha256", "context_sha256", "context", "samples"},
           {"schema", "samples"}, "candidate replay")
    if payload["schema"] != "mutation_candidates/1":
        raise ValueError("Candidate replay schema must be mutation_candidates/1")
    if "context" in payload and not same_context(payload["context"], manifest["context"]):
        raise ValueError("Replay vocabulary/background differs from the campaign context")
    if not isinstance(payload["samples"], list) or not payload["samples"] or len(payload["samples"]) > 5000:
        raise ValueError("Replay needs 1 to 5000 samples")
    variants = {v["id"]: v for v in manifest["variants"]}
    ids = {r["id"] for r in manifest["requirements"]}
    seen, repetitions = set(), set()
    for sample in payload["samples"]:
        fields(sample, {"variant_id", "repetition", "source_requirements", "formulas", "provenance", "unsupported", "runtime_evidence", "status", "context"},
               {"variant_id", "repetition", "source_requirements", "formulas"}, "sample")
        vid, rep = sample["variant_id"], sample["repetition"]
        if type(rep) is not int or not 1 <= rep <= 50:
            raise ValueError("Repetition must be an integer from 1 to 50")
        if vid != "baseline" and (vid not in variants or "text" not in variants[vid]):
            raise ValueError("Replay variant needs a registered source text")
        if (vid, rep) in seen:
            raise ValueError("Duplicate replay sample")
        seen.add((vid, rep))
        repetitions.add(rep)
        if sample["source_requirements"] != source_packet(manifest, variants.get(vid)):
            raise ValueError("Replay source text, IDs, order or context differs from the frozen variant packet")
        if not isinstance(sample["formulas"], dict) or set(sample["formulas"]) != ids:
            raise ValueError("Each sample must inventory every requirement formula; use null plus an unsupported reason")
        if "provenance" in sample and not isinstance(sample["provenance"], dict):
            raise ValueError("Optional replay provenance must be an object")
        if "context" in sample and not same_context(sample["context"], manifest["context"]):
            raise ValueError("Sample vocabulary/background differs from the campaign context")
        unsupported = sample.get("unsupported", {})
        if not isinstance(unsupported, dict) or set(unsupported) - ids:
            raise ValueError("Invalid unsupported formula inventory")
        for rid, formula in sample["formulas"].items():
            if formula is None:
                nonempty(unsupported.get(rid), f"{vid}/{rid} unsupported reason")
        # Formula validity is a measured output, not an import prerequisite.
    if any(("baseline", rep) not in seen for rep in repetitions):
        raise ValueError("Every replay repetition needs the unmutated baseline sample")
    return deepcopy(payload)


def _conjunction(terms):
    terms = list(terms)
    if not terms:
        return True
    while len(terms) > 1:
        terms = [{"op": "and", "args": terms[i:i+16]} if len(terms[i:i+16]) > 1 else terms[i]
                 for i in range(0, len(terms), 16)]
    return terms[0]


def _evaluate_samples(manifest, candidates, output, metadata, timeout_seconds, solver):
    candidates = validate_candidates(manifest, candidates)
    write_json(output / "candidates.json", candidates)
    samples = {(s["variant_id"], s["repetition"]): s for s in candidates["samples"]}
    repetitions = sorted({s["repetition"] for s in candidates["samples"]})
    by_id = {r["id"]: r for r in manifest["requirements"]}
    baseline_checks, eligibility, records = {}, {}, []
    canonical_consistency = check_consistency(manifest["context"],
        _conjunction(r["formula"] for r in manifest["requirements"] if r["formula"] is not None),
        output / "canonical_bundle_consistency", timeout_seconds, solver)
    for variant in manifest["variants"]:
        rid = variant["requirement_id"]
        expected = apply_mutation(by_id[rid]["formula"], variant["mutation"])
        eligibility[variant["id"]] = compare_formulas(manifest["context"], by_id[rid]["formula"], expected,
            output / "reference_checks" / variant["id"], timeout_seconds, solver)
    for rep in repetitions:
        baseline = samples[("baseline", rep)]
        baseline_checks[str(rep)] = {}
        for rid, reference in by_id.items():
            baseline_checks[str(rep)][rid] = _compare_candidate(manifest, reference["formula"], baseline["formulas"][rid],
                output / "baseline_checks" / str(rep) / rid, timeout_seconds, solver, baseline.get("unsupported", {}).get(rid))
        for variant in manifest["variants"]:
            if "text" not in variant:
                continue
            vid, rid = variant["id"], variant["requirement_id"]
            sample = samples.get((vid, rep))
            if sample is None:
                reason = "Variant has no source text" if "text" not in variant else "No generated/imported sample supplied"
                records.append(_record(variant, _unavailable("missing_candidate", reason), repetition=rep))
                continue
            directory = output / "comparisons" / str(rep) / vid
            generated = sample["formulas"][rid]
            comparison = _compare_candidate(manifest, by_id[rid]["formula"], generated, directory / "canonical",
                timeout_seconds, solver, sample.get("unsupported", {}).get(rid))
            expected = apply_mutation(by_id[rid]["formula"], variant["mutation"])
            preservation = _compare_candidate(manifest, expected, generated, directory / "expected_mutation",
                timeout_seconds, solver, sample.get("unsupported", {}).get(rid))
            baseline_difference = _compare_candidate(manifest, baseline["formulas"][rid], generated,
                directory / "generated_baseline", timeout_seconds, solver)
            collateral = []
            for sibling in by_id:
                if sibling == rid:
                    continue
                before, after = baseline["formulas"][sibling], sample["formulas"][sibling]
                if before is None or after is None or before != after:
                    collateral.append({"requirement_id": sibling, "comparison": _compare_candidate(manifest, before, after,
                        directory / "collateral" / sibling, timeout_seconds, solver)})
            terms = [generated] + [r["formula"] for r in manifest["requirements"] if r["id"] != rid and r["formula"] is not None]
            isolation = check_consistency(manifest["context"], _conjunction(terms), directory / "frozen_neighbor_consistency",
                timeout_seconds, solver) if generated is not None else {"status": "unsupported"}
            baseline_relation = baseline_checks[str(rep)][rid].get("classification")
            baseline_match = baseline_relation == "equivalent" if baseline_relation in RELATIONS else None
            relation = eligibility[vid].get("classification")
            expected_verified = relation == variant["expected_relation"] if relation in RELATIONS else None
            preservation_relation = preservation.get("classification")
            preserved = preservation_relation == "equivalent" if preservation_relation in RELATIONS else None
            records.append(_record(variant, comparison, repetition=rep,
                evidence_directory=str(directory.relative_to(output)), baseline_matches_reference=baseline_match,
                expected_reference_relation_verified=expected_verified, expected_mutation_preserved=preserved,
                attributable_preserved_change=(baseline_match is True and expected_verified is True and preserved is True)
                    if preserved is not None and expected_verified is not None and baseline_match is not None else None,
                expected_mutation_comparison=preservation, generated_baseline_comparison=baseline_difference,
                collateral_changes=collateral, frozen_neighbor_consistency=isolation,
                frozen_neighbor_unsupported_ids=[r["id"] for r in manifest["requirements"] if r["formula"] is None],
                runtime_evidence=sample.get("runtime_evidence", {}), generation_status=sample.get("status", "imported")))
            write_json(output / "progress.json", {"completed": len(records), "planned": len(repetitions) * sum("text" in v for v in manifest["variants"])})
    return _finish(output, manifest, metadata, records, baseline_checks=baseline_checks,
        reference_checks=eligibility, generation_samples=len(candidates["samples"]),
        canonical_bundle_consistency=canonical_consistency,
        canonical_bundle_unsupported_ids=[r["id"] for r in manifest["requirements"] if r["formula"] is None],
        source_eligibility=[{"variant_id": v["id"], "eligible": "text" in v,
            "reason": "Frozen source-text variant supplied" if "text" in v else "Formal-only variant: no source text"}
            for v in manifest["variants"]],
        source_metrics={"planned_variant_trials": len(records),
            "expected_mutation_preserved": sum(r.get("expected_mutation_preserved") is True for r in records),
            "attributable_preserved_mutants": sum(r["kind"] == "mutant" and r.get("attributable_preserved_change") is True for r in records),
            "baseline_reference_mismatches": sum(c.get("classification") in RELATIONS and c["classification"] != "equivalent"
                for checks in baseline_checks.values() for c in checks.values()),
            "baseline_inconclusive": sum(c.get("classification") not in RELATIONS for checks in baseline_checks.values() for c in checks.values()),
            "control_reference_disagreements": sum(r["kind"] == "control" and r["comparison"].get("difference_detected") is True for r in records),
            "control_generated_meaning_changes": sum(r["kind"] == "control" and r.get("generated_baseline_comparison", {}).get("difference_detected") is True for r in records),
            "control_changes_from_equivalent_baseline": sum(r["kind"] == "control" and r.get("baseline_matches_reference") is True
                and r.get("generated_baseline_comparison", {}).get("difference_detected") is True for r in records)})


def run_replay(manifest, candidates, output, timeout_seconds=10.0, solver="z3"):
    manifest = validate_manifest(manifest)
    candidates = validate_candidates(manifest, candidates)
    output, metadata = _new_output(output, manifest, "replay", {"solver": solver, "timeout_seconds": timeout_seconds,
        "provider_calls": 0})
    return _evaluate_samples(manifest, candidates, output, metadata, timeout_seconds, solver)


def extract_canonical_formulas(manifest, packet, run):
    """Read generated ASTs directly; no text reparsing, renaming or new premises."""
    from canonical_tlr import tlr_context, validate_tlr
    formulas = {row["id"]: None for row in manifest["requirements"]}
    unsupported = {}
    try:
        if "sources" in run and run["sources"] != packet:
            raise ValueError("Generated source IDs, wording or context differ from the supplied packet")
        tlr = validate_tlr(run.get("tlr"), sources=packet)
        if not same_context(tlr_context(tlr), manifest["context"]):
            reason = "Comparison context differs: generated variables, domains or assumptions changed the fixed vocabulary/background"
            return formulas, {rid: reason for rid in formulas}
        for row in tlr["requirements"]:
            rid = row["id"]
            if row["status"] == "supported":
                formulas[rid] = validate_formula(row["formula"], manifest["context"])
            else:
                unsupported[rid] = row["reason"]
    except (ValueError, KeyError, TypeError) as exc:
        return {rid: None for rid in formulas}, {rid: str(exc) for rid in formulas}
    return formulas, unsupported


def extract_formulas(manifest, packet, run):
    if isinstance(run.get("tlr"), dict) and run["tlr"].get("schema") == "mbse_tlr/1":
        return extract_canonical_formulas(manifest, packet, run)
    return extract_scalar_formulas(manifest, packet, run)


def extract_scalar_formulas(manifest, packet, run):
    """Read supported scalar TLR fields; never rerun a grammar on LLM text."""
    tlr = run.get("tlr") or {}
    formulas, unsupported = {}, {}
    sources = {r["id"]: r for r in packet}
    clauses = {r["id"]: r for r in tlr.get("requirements", []) if isinstance(r, dict) and "id" in r}
    symbols = {s["name"]: s for s in tlr.get("symbols", []) if isinstance(s, dict) and "name" in s}
    variables = {v["name"]: v for v in manifest["context"]["variables"]}
    relations = {"le": "<=", "lt": "<", "ge": ">=", "gt": ">", "eq": "="}
    for reference in manifest["requirements"]:
        rid = reference["id"]
        formulas[rid] = None
        try:
            if tlr.get("schema") != "review_tlr/1":
                raise ValueError("Generated native pipeline TLR has no supported static-AST extraction adapter; use reviewed replay")
            row = clauses.get(rid, {})
            binding = reference.get("binding")
            if not binding:
                raise ValueError("No frozen source-quantity-to-reference-variable binding supplied")
            if row.get("status") != "supported" or row.get("text") != sources[rid]["text"]:
                raise ValueError(row.get("reason") or "Missing supported source-linked scalar")
            expected_binding = {"kind": "quantity", "trigger": "", "response": "", **binding}
            for key in ("subject", "quantity", "kind", "trigger", "response"):
                if row.get(key, "") != expected_binding[key]:
                    raise ValueError(f"Generated scalar binding differs in {key}; no automatic remapping")
            if row.get("context") != "all operating contexts":
                raise ValueError("Generated scalar has an unreviewed applicability context")
            if row.get("kind") != "quantity":
                raise ValueError("Per-command temporal approximations require explicit reviewed replay; not promoted to scalar fidelity")
            symbol = symbols.get(row.get("symbol"), {})
            variable = variables[binding["variable"]]
            if symbol.get("type") != variable["type"] or symbol.get("unit") != variable.get("unit", "1") or row.get("unit") != symbol.get("unit"):
                raise ValueError("Generated quantity type/unit differs from the frozen vocabulary")
            if "minimum" in symbol or "bounds" in symbol:
                raise ValueError("Generated scalar adds a domain minimum; context review is required")
            formula = {"op": relations[row["relation"]], "args": [{"var": binding["variable"]}, {"value": row["value"], "unit": row["unit"]}]}
            formulas[rid] = validate_formula(formula, manifest["context"])
        except (ValueError, KeyError, TypeError) as exc:
            unsupported[rid] = str(exc)
    return formulas, unsupported


def _generate_sample(manifest, packet, vid, repetition, output, engine, timeout, model=None):
    directory = output / "generations" / str(repetition) / vid
    directory.mkdir(parents=True)
    write_json(directory / "requirements.json", packet)
    run_directory = (directory / "run").resolve()
    if engine == "pipeline":
        write_json(directory / "context.json", manifest["context"])
        command = [sys.executable, str(ROOT / "scripts" / "canonical_cli.py"), "run",
                   "--statement", str((directory / "requirements.json").resolve()),
                   "--context-file", str((directory / "context.json").resolve()),
                   "--condition", "C", "--output-dir", str(run_directory)]
        if model:
            command.extend(["--model", model])
    else:
        command = [sys.executable, str(ROOT / "scripts" / "requirements_pipeline.py"), "review",
                   "--statement", str((directory / "requirements.json").resolve()),
                   "--engine", "local", "--analysis-mode", "requirements",
                   "--data-dir", str((directory / "runs").resolve())]
    started = time.monotonic()
    status, returncode = "generation_error", None
    with (directory / "stdout.json").open("w", encoding="utf-8") as stdout, (directory / "stderr.log").open("w", encoding="utf-8") as stderr:
        try:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                returncode = process.wait(timeout=timeout)
                status = "completed" if returncode == 0 else "generation_error"
            except BaseException as exc:
                # The harness owns this new process group; do not orphan timed-out or interrupted workers.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                if not isinstance(exc, subprocess.TimeoutExpired):
                    raise
                status = "timeout"
        except OSError as exc:
            stderr.write(str(exc))
    run = {}
    try:
        response = read_json(directory / "stdout.json")
        if isinstance(response, dict):
            run = response.get("run", response)
            if not isinstance(run, dict):
                run = {}
        if engine == "pipeline":
            artifact_path = run.get("tlr")
            if not isinstance(artifact_path, dict):
                path = Path(artifact_path) if isinstance(artifact_path, str) else run_directory / "tlr.json"
                if not path.is_file() and not path.is_absolute():
                    path = run_directory / path
                if path.is_file():
                    run["tlr"] = read_json(path)
            sources_path = run_directory / "sources.json"
            if sources_path.is_file():
                run["sources"] = read_json(sources_path)
    except (ValueError, OSError):
        pass
    if status == "completed" and not isinstance(run.get("tlr"), dict):
        status = "parse_error"
    formulas, unsupported = extract_formulas(manifest, packet, run)
    elapsed = time.monotonic() - started
    invocation = {"command": command, "engine": engine, "status": status, "returncode": returncode,
                  "latency_seconds": elapsed, "generation_timeout_seconds": timeout}
    write_json(directory / "invocation.json", invocation)
    return {"variant_id": vid, "repetition": repetition, "source_requirements": packet,
            "formulas": formulas, "unsupported": unsupported, "status": status,
            "provenance": {"method": "canonical_AST_extraction" if engine == "pipeline" else "supported_scalar_TLR_extraction",
                "engine": engine, "generation_directory": str(directory.relative_to(output)),
                "execution_config": run.get("execution_config"), "latency_seconds": elapsed,
                "scope": "Actual requirements workflow; mutation comparison is a separate evaluation."},
            "runtime_evidence": {"run_id": run.get("id"), "status": run.get("status"),
                "analysis": run.get("analysis"), "compilation": run.get("compilation"),
                "model": run.get("model"), "stages": run.get("stages"),
                "scope": "Recorded runtime evidence; mutation comparator findings are separate."}}


def run_source(manifest, output, engine="pipeline", repetitions=1, max_generations=20,
               generation_timeout_seconds=600, timeout_seconds=10.0, solver="z3", model=None):
    manifest = validate_manifest(manifest)
    if engine not in {"local", "pipeline"} or type(repetitions) is not int or not 1 <= repetitions <= 50:
        raise ValueError("Choose local/pipeline and 1 to 50 repetitions")
    if not 0 < generation_timeout_seconds <= 86400:
        raise ValueError("Generation timeout must be positive and at most 86400 seconds")
    source_variants = [v for v in manifest["variants"] if "text" in v]
    if not source_variants:
        raise ValueError("Source campaign requires at least one source-text variant")
    planned = repetitions * (1 + len(source_variants))
    if type(max_generations) is not int or not 1 <= max_generations <= 5000 or planned > max_generations:
        raise ValueError(f"Campaign needs {planned} workflow invocations; exceeds --max-generations {max_generations}")
    output, metadata = _new_output(output, manifest, "source", {"solver": solver, "timeout_seconds": timeout_seconds,
        "engine": engine, "repetitions": repetitions, "planned_workflow_invocations": planned,
        "max_generations": max_generations, "generation_timeout_seconds": generation_timeout_seconds,
        "model": model,
        "provider_policy": ("Canonical CLI condition C with fixed vocabulary/background and no semantic repair."
                            if engine == "pipeline" else "Legacy fixture workflow; no provider calls.")})
    candidates = {"schema": "mutation_candidates/1", "context": manifest["context"], "samples": []}
    for repetition in range(1, repetitions + 1):
        for variant in [None] + source_variants:
            vid = variant["id"] if variant else "baseline"
            print(f"Mutation source generation {len(candidates['samples']) + 1}/{planned}: repetition {repetition}, {vid}", file=sys.stderr)
            candidates["samples"].append(_generate_sample(manifest, source_packet(manifest, variant), vid, repetition,
                                                        output, engine, generation_timeout_seconds, model=model))
            write_json(output / "candidates.json", candidates)
            write_json(output / "progress.json", {"completed_workflow_invocations": len(candidates["samples"]), "planned": planned})
    return _evaluate_samples(manifest, candidates, output, metadata, timeout_seconds, solver)
