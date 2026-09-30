"""Paired source-only versus solver-informed repair of one shared initial TLR."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys


def run_feedback_study(sources, output_dir, repetitions=5, model=None, context=None, tlr=None,
                       a_sysml=None, compile_model=True, solver="z3", timeout_seconds=10.0,
                       generator=None, feedback_repairs=2, development_scenarios=None):
    import canonical_cli as cli
    from canonical_feedback import FEEDBACK_POLICY_VERSION

    if type(repetitions) is not int or not 1 <= repetitions <= 50:
        raise ValueError("Repetitions must be from 1 to 50")
    cli._repair_budget(feedback_repairs, "Feedback repair budget")
    if not feedback_repairs:
        raise ValueError("The feedback study requires a positive review budget")
    context, model = cli._fixed_context(context), cli._model(model)
    if development_scenarios is not None:
        from canonical_scenarios import validate_scenario_suite
        development_scenarios = validate_scenario_suite(development_scenarios, sources)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    cli.write_json(directory / "sources.json", sources)
    if development_scenarios is not None:
        cli.write_json(directory / "development_suite.json", development_scenarios)
    config = {"schema": "canonical_feedback_study/1", "model": model, "repetitions": repetitions,
        "context": context, "conditions": ["A", "B", "C"], "shared_BC_candidate": False,
        "shared_initial_TLR": True, "feedback_repair_budget": feedback_repairs,
        "abstention_repair_budget": 0, "repair_policy": FEEDBACK_POLICY_VERSION,
        "feedback_modes": {"A": "none", "B": "source", "C": "solver"},
        "planned_generation_calls": repetitions * ((a_sysml is None) + (tlr is None)),
        "max_additional_transport_invocations": 2 * repetitions * feedback_repairs,
        "max_model_transport_invocations": repetitions * ((a_sysml is None) + (tlr is None) + 2 * feedback_repairs),
        "order": "Alternate A/structured generation and B/C review branch order across repetitions",
        "profile": cli.PROFILE, "abstraction_policy": cli.POLICY,
        "semantic_repairs": 0, "repair_attempts": 0,
        "semantic_repairs_scope": "Accepted candidate revisions; no source, evaluator or reference changes.",
        "budget_scope": "One source-grounded proposal call per round in each B/C branch; equal maximum opportunities, actual calls recorded.",
        "evaluation_boundary": "Only frozen final SysML and source/context enter judges; no judge or held-out mutation feedback enters review."}
    if development_scenarios is not None:
        from canonical_feedback import SCENARIO_POLICY_VERSION
        config.update(C_repair_policy=SCENARIO_POLICY_VERSION, development_scenarios="development_suite.json",
            treatment_note="C additionally receives source-grounded development scenarios, vocabulary assistance and regression gating; this is not a solver-only ablation.")
        config["feedback_modes"]["C"] = "solver_and_development_scenarios"
    cli.write_json(directory / "study_configuration.json", config)
    rows = []
    common = dict(model=model, context=context, compile_model=compile_model, solver=solver,
                  timeout_seconds=timeout_seconds, generator=generator, abstention_repairs=0)
    for rep in range(1, repetitions + 1):
        row = {"repetition": rep}
        rep_dir = directory / f"rep-{rep:03d}"
        for route in (("A", "initial") if rep % 2 else ("initial", "A")):
            print(f"Feedback study repetition {rep}/{repetitions}: {route}", file=sys.stderr)
            if route == "A":
                row["A"] = cli.run_candidate(sources, rep_dir / "A", "A", sysml_text=a_sysml, **common)
            else:
                # Generate and render once. No solver evidence is available to initial generation.
                row["initial"] = cli.run_candidate(sources, rep_dir / "initial", "B", tlr=deepcopy(tlr), **common)
        for arm in (("B", "C") if rep % 2 else ("C", "B")):
            print(f"Feedback study repetition {rep}/{repetitions}: {arm} review", file=sys.stderr)
            if row["initial"]["status"] == "completed" and row["initial"].get("tlr") is not None:
                branch = cli.run_candidate(sources, rep_dir / arm, arm,
                    tlr=deepcopy(row["initial"]["tlr"]), feedback_repairs=feedback_repairs,
                    development_scenarios=development_scenarios if arm == "C" else None, **common)
                branch["configuration"].update(generation_mode="shared_initial_TLR",
                    initial_candidate="../initial", generation_attempts=0)
                cli.write_json(rep_dir / arm / "configuration.json", branch["configuration"])
                cli.write_json(rep_dir / arm / "result.json", branch)
            else:
                # Never silently generate a different seed after the paired initial attempt fails.
                path = rep_dir / arm
                path.mkdir(parents=True)
                cli.write_json(path / "sources.json", sources)
                branch = {"schema": "canonical_run/1", "condition": arm, "status": "failed",
                    "output_dir": str(path.resolve()), "tlr": None, "admission": "withheld",
                    "analysis": {"status": "not_run", "reason": "Shared initial generation failed"},
                    "compilation": {"status": "not_run"},
                    "errors": ["Shared initial generation failed; no independent replacement generated."],
                    "configuration": {"condition": arm, "model": model, "generation_mode": "shared_initial_TLR",
                        "initial_candidate": "../initial", "generation_attempts": 0, "semantic_repairs": 0,
                        "repair_attempts": 0, "feedback_repair_budget": feedback_repairs,
                        "feedback_mode": "solver" if arm == "C" else "source",
                        "repair_policy": FEEDBACK_POLICY_VERSION, "repair_stop_reason": "initial_generation_failed",
                        "max_model_transport_invocations": feedback_repairs},
                    "source_fidelity": "unassessed"}
                cli.write_json(path / "configuration.json", branch["configuration"])
                cli.write_json(path / "result.json", branch)
            row[arm] = branch
        rows.append(row)
        cli.write_json(directory / "progress.json", {"completed_repetitions": len(rows), "planned": repetitions})
    branches = [r[a] for r in rows for a in ("A", "B", "C")]
    repairs = [r[a] for r in rows for a in ("B", "C")]
    summary = {"planned_repetitions": repetitions, "unique_candidate_slots": 3 * repetitions,
        "generated_candidates": sum(bool(r.get("model_file")) for r in branches),
        "generation_failures": sum(r["status"] != "completed" for r in branches),
        "initial_generation_failures": sum(r["initial"]["status"] != "completed" for r in rows),
        "compiled_candidates": sum(r["compilation"].get("status") == "passed" for r in branches),
        "C_admitted": sum(r["C"]["admission"] == "admitted_consistent_encoding" for r in rows),
        "accepted_repair_attempts": sum(r["configuration"].get("semantic_repairs", 0) for r in repairs),
        "repair_attempts": sum(r["configuration"].get("repair_attempts", 0) for r in repairs),
        "diagnosis_calls": 0, "fidelity": "not_assessed_until_independent_judgments",
        "B_C_candidate_identity": "Shared initial TLR; independently revised final candidates.",
        "by_condition": {arm: {
            "repair_attempts": sum(r[arm]["configuration"].get("repair_attempts", 0) for r in rows),
            "accepted_repair_attempts": sum(r[arm]["configuration"].get("semantic_repairs", 0) for r in rows),
            "representations": [r[arm].get("representation") for r in rows]}
            for arm in ("B", "C")}}
    config.update(semantic_repairs=summary["accepted_repair_attempts"], repair_attempts=summary["repair_attempts"],
        actual_model_transport_invocations=sum(r[a]["configuration"].get("generation_attempts", 0)
            for r in rows for a in ("A", "initial")) + summary["repair_attempts"])
    cli.write_json(directory / "study_configuration.json", config)
    result = {"schema": "canonical_study/2", "status": "completed", "rows": rows, "summary": summary,
        "limitations": [
            "B/C share their initial TLR and review budgets; C alone receives internal Z3 diagnostics.",
            "A has no matched TLR review loop: A versus B/C combines generation route and extra inference effort.",
            "Accepted source-grounded revisions are LLM-reviewed, not proven faithful; SAT is not the selection objective.",
            "One review may stop a branch early; equal budgets do not imply equal actual calls.",
            "Source assumptions and existing symbol meanings remain frozen; corrections requiring context changes need a new source trial.",
            "Initial/final artifacts and all attempts are retained; default independent judging assesses final candidates only.",
            "No independent SysML read-back, human approval, judge feedback or held-out mutation feedback is included."]}
    cli.write_json(directory / "study.json", result)
    if development_scenarios is not None:
        result["limitations"].append(config["treatment_note"])
        cli.write_json(directory / "study.json", result)
    lines = ["# A/B/C study with bounded semantic feedback", "",
        "B and C start from one initial TLR and have the same review budget. B uses source review; C adds Z3 query/result evidence. Final candidates can differ.", "",
        "| Repetition | Arm | Execution | Compilation | Reviews | Accepted revisions | Admission |",
        "|---|---|---|---|---:|---:|---|"]
    for row in rows:
        for arm in ("A", "B", "C"):
            r = row[arm]
            lines.append(f"| {row['repetition']} | {arm} | {r['status']} | {r['compilation']['status']} | {r['configuration'].get('repair_attempts', 0)} | {r['configuration'].get('semantic_repairs', 0)} | {r['admission']} |")
    lines += ["", "Repairs can change supported formulas. Accepted means the edit passed source-grounding, static guards and configured compilation; it is not a semantic fidelity verdict.", "",
              "The final independent assertion assessment is a separate command. Source-only B never executes Z3. Saved historical audit-only studies remain unchanged."]
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if development_scenarios is not None:
        with (directory / "report.md").open("a", encoding="utf-8") as handle:
            handle.write("\n" + config["treatment_note"] + "\n")
    return result
