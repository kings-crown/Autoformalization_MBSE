#!/usr/bin/env python3
"""
End-to-end pipeline that

1. Translates a requirements text fragment into natural language, lemma, and SMT artefacts.
2. Validates both SAT and UNSAT variants with Z3.
3. Emits a SysML v2 textual model tying the requirement to the solver artefacts.
4. Optionally compiles the SysML model with the SysML v2 kernel to ensure it loads cleanly.

Example:

    OPENAI_API_KEY=sk-... python scripts/requirements_pipeline.py \\
        --statement Requirements_examples/delivery_methods.txt \\
        --output-prefix out/delivery_methods \\
        --sysml-output SysML-v2-Release/sysml/DeliveryMethods.sysml

Environment overrides:
    * SYSML_KERNEL_JAR – path to jupyter-sysml-kernel-*.jar (defaults to sysml-0.52.0 env).
    * SYSML_EXIT_COMMAND – command used to terminate the interactive session (default ':exit!').
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from openai_toolkit import run_translate_flow  # type: ignore


DEFAULT_MODEL = os.getenv("OPENAI_TRANSLATION_MODEL", "gpt-4o")
DEFAULT_SYSML_ENV = Path.home() / "miniconda3" / "envs" / "sysml-0.52.0"
DEFAULT_SYSML_JAR = os.getenv(
    "SYSML_KERNEL_JAR",
    str(DEFAULT_SYSML_ENV / "share" / "jupyter" / "kernels" / "sysml" / "jupyter-sysml-kernel-0.52.0-all.jar"),
)
DEFAULT_EXIT_COMMAND = os.getenv("SYSML_EXIT_COMMAND", ":exit!")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate requirements → SMT (sat/unsat) → SysML model and validate with SysML kernel."
    )
    parser.add_argument(
        "--statement",
        required=True,
        type=Path,
        help="Path to the requirements text file fed into the translation flow.",
    )
    parser.add_argument(
        "--output-prefix",
        required=True,
        type=Path,
        help="Output prefix for generated artefacts (JSON + SMT files).",
    )
    parser.add_argument(
        "--sysml-output",
        required=True,
        type=Path,
        help="Destination .sysml file that will reference the generated artefacts.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenAI model identifier (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--sysml-jar",
        default=DEFAULT_SYSML_JAR,
        type=Path,
        help="Path to the SysML kernel fat JAR (default: sysml-0.52.0 install).",
    )
    parser.add_argument(
        "--sysml-context",
        type=Path,
        help="Optional SysML snippet that provides modelling patterns for the generated package.",
    )
    parser.add_argument(
        "--skip-sysml-compile",
        action="store_true",
        help="Generate the SysML model but skip invoking the SysML kernel for validation.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Timeout in seconds for the SysML kernel compilation step (default: 300s).",
    )
    return parser.parse_args()


def _ensure_success(validation: Dict[str, Any], expected: str) -> None:
    status = validation.get("status")
    result = validation.get("result")
    if status != "ok" or result != expected:
        raise RuntimeError(
            f"Expected {expected!r} validation to succeed (status=ok, result={expected}) "
            f"but saw status={status!r}, result={result!r}, diagnostics={validation.get('diagnostics')!r}"
        )


def _select_smt_entries(smt_files: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    sat_entry = unsat_entry = None
    for entry in smt_files:
        mode = entry.get("mode")
        if mode == "sat":
            sat_entry = entry
            _ensure_success(entry["validation"], "sat")
        elif mode == "unsat":
            unsat_entry = entry
            _ensure_success(entry["validation"], "unsat")
    if sat_entry is None or unsat_entry is None:
        raise RuntimeError("Missing SAT or UNSAT SMT artefact in translation output.")
    return sat_entry, unsat_entry


def _doc_block(lines: Iterable[str]) -> str:
    sanitized = []
    for line in lines:
        safe = line.replace("*/", "* /").rstrip()
        sanitized.append(safe)
    body = "\n".join(f"\t * {line}" if line else "\t *" for line in sanitized)
    return f"/*\n{body}\n\t */"


def _string_literal(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\n", "\\n")
    )


def _build_sysml_content(
    package_name: str,
    statement_path: str,
    translate_payload: Dict[str, Any],
    sat_entry: Dict[str, Any],
    unsat_entry: Dict[str, Any],
    context_snippet: Optional[str],
) -> str:
    statement_text = translate_payload.get("statement", "").strip()
    natural = translate_payload.get("natural_language", "").strip()
    informal = translate_payload.get("informal_statement", "").strip()
    proof = translate_payload.get("informal_proof", "").strip()
    sat_info = sat_entry.get("validation", {})
    unsat_info = unsat_entry.get("validation", {})
    sat_path = Path(sat_entry["path"])
    unsat_path = Path(unsat_entry["path"])
    sat_path_str = sat_path.as_posix()
    unsat_path_str = unsat_path.as_posix()

    source_label = statement_path or "N/A"
    package_doc = _doc_block(
        [
            "Auto-generated SysML v2 package derived from textual requirements.",
            f"Source requirements file: {source_label}",
            "Pipeline stages: requirements ➜ LLM translation ➜ SMT validation ➜ SysML packaging.",
        ]
    )
    requirement_doc = _doc_block(
        [
            "Requirement derived from the textual charging policy.",
            "The RequirementContext part stores the original statement and derived interpretations.",
        ]
    )
    context_doc_lines = [
        "Captures textual artefacts produced during the requirements-to-SMT translation.",
        "Attributes mirror the pipeline outputs so downstream tooling can trace provenance.",
        "",
        "Follow these steps to elaborate the generated package into a fuller SysML model:",
        "1. Introduce domain parts (e.g., ChargerController, Battery) in a sibling package and connect them via usages.",
        "2. Refine `AutoGeneratedRequirement` by decomposing it into child requirements or constraint usages tied to those parts.",
        "3. Replace the placeholder constraint with behavioural/state invariants that invoke your domain model.",
        "4. Extend `SmtValidationSummary` with additional objectives or analyses that reference simulation/test artefacts.",
        "5. Update `EvidenceOverview` (or derive new views) to expose the extended structural and behavioural elements.",
    ]
    if context_snippet:
        context_doc_lines.append("")
        context_doc_lines.append("Reference modelling snippet that guided generation:")
        context_doc_lines.extend(context_snippet.splitlines())
    context_doc = _doc_block(context_doc_lines)
    evidence_doc = _doc_block(
        [
            "Solver artefacts backing the requirement.",
            f"SAT SMT-LIB: {sat_path_str} (result={sat_info.get('result')}, exit_code={sat_info.get('exit_code')})",
            f"UNSAT SMT-LIB: {unsat_path_str} (result={unsat_info.get('result')}, exit_code={unsat_info.get('exit_code')})",
        ]
    )
    analysis_doc = _doc_block(
        [
            "SysML wrapper over the solver runs.",
            "The objective asserts the requirement and checks that the solver statuses match expectations.",
        ]
    )
    view_doc = _doc_block(
        [
            "Curated view exposing the requirement, evidence, and textual context.",
            "Useful for tooling that wants a single entry point to the generated artefacts.",
        ]
    )

    sat_path_literal = _string_literal(sat_path_str)
    unsat_path_literal = _string_literal(unsat_path_str)
    sat_status_literal = _string_literal(str(sat_info.get("result", "n/a")))
    unsat_status_literal = _string_literal(str(unsat_info.get("result", "n/a")))
    statement_literal = _string_literal(statement_text)
    natural_literal = _string_literal(natural)
    informal_literal = _string_literal(informal)
    proof_literal = _string_literal(proof)

    return (
        f"package {package_name} {{\n"
        f"\timport Requirements::*;\n"
        f"\timport ScalarValues::*;\n"
        f"\timport AnalysisCases::*;\n\n"
        f"\tdoc {package_doc}\n\n"
        f"\tpart def RequirementContext {{\n"
        f"\t\tdoc {context_doc}\n"
        f"\t\tattribute originalStatement : ScalarValues::String = \"{statement_literal}\";\n"
        f"\t\tattribute naturalInterpretation : ScalarValues::String = \"{natural_literal}\";\n"
        f"\t\tattribute informalLemma : ScalarValues::String = \"{informal_literal}\";\n"
        f"\t\tattribute reasoningSketch : ScalarValues::String = \"{proof_literal}\";\n"
        f"\t}}\n\n"
        f"\tpart def SolverEvidence {{\n"
        f"\t\tdoc {evidence_doc}\n"
        f"\t\tattribute satArtifactPath : ScalarValues::String = \"{sat_path_literal}\";\n"
        f"\t\tattribute unsatArtifactPath : ScalarValues::String = \"{unsat_path_literal}\";\n"
        f"\t\tattribute satStatus : ScalarValues::String = \"{sat_status_literal}\";\n"
        f"\t\tattribute unsatStatus : ScalarValues::String = \"{unsat_status_literal}\";\n"
        f"\t}}\n\n"
        f"\trequirement def AutoGeneratedRequirement :> RequirementCheck {{\n"
        f"\t\tdoc {requirement_doc}\n"
        f"\t\tsubject context : RequirementContext;\n"
        f"\t\tsubject evidence : SolverEvidence;\n"
        f"\t\trequire constraint {{ context.originalStatement != \"\" }}\n"
        f"\t}}\n\n"
        f"\tanalysis def SmtValidationSummary :> AnalysisCase {{\n"
        f"\t\tdoc {analysis_doc}\n"
        f"\t\tsubject proof : SolverEvidence;\n"
        f"\t\tsubject context : RequirementContext;\n"
        f"\t\tin requirement target : AutoGeneratedRequirement;\n"
        f"\t\tobjective solverConsistency {{\n"
        f"\t\t\trequire target;\n"
        f"\t\t\trequire constraint {{ proof.satStatus == \"sat\" & proof.unsatStatus == \"unsat\" }}\n"
        f"\t\t}}\n"
        f"\t}}\n\n"
        f"\tview def EvidenceOverview {{\n"
        f"\t\tdoc {view_doc}\n"
        f"\t\texpose RequirementContext::*;\n"
        f"\t\texpose SolverEvidence::*;\n"
        f"\t\texpose AutoGeneratedRequirement::*;\n"
        f"\t\texpose SmtValidationSummary::*;\n"
        f"\t}}\n"
        f"}}\n"
    )


def _write_sysml_model(
    output_path: Path,
    translate_payload: Dict[str, Any],
    sat_entry: Dict[str, Any],
    unsat_entry: Dict[str, Any],
    context_snippet: Optional[str],
) -> None:
    package_name = output_path.stem.replace("-", "_")
    model = _build_sysml_content(
        package_name=package_name,
        statement_path=str(translate_payload.get("statement_path", "") or ""),
        translate_payload=translate_payload,
        sat_entry=sat_entry,
        unsat_entry=unsat_entry,
        context_snippet=context_snippet,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(model, encoding="utf-8")


def _compile_with_sysml_kernel(
    jar_path: Path,
    sysml_path: Path,
    timeout: float,
) -> None:
    if not jar_path.exists():
        raise RuntimeError(f"SysML kernel JAR not found at {jar_path}")
    cmd = [
        "java",
        "-cp",
        str(jar_path),
        "org.omg.sysml.interactive.SysMLInteractive",
    ]
    payload = f"load {sysml_path}\n{DEFAULT_EXIT_COMMAND}\n"
    completed = subprocess.run(
        cmd,
        input=payload,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"SysML kernel reported failure (exit={completed.returncode}). Output:\n{completed.stdout}"
        )


async def _run_pipeline(args: argparse.Namespace) -> Dict[str, Any]:
    output_prefix = args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    result_json_path = output_prefix.with_suffix("") if output_prefix.suffix else output_prefix
    result_json_path = result_json_path.with_name(result_json_path.name + "_translate.json")

    translate_payload = await run_translate_flow(
        statement_path=args.statement,
        model=args.model,
        smt_prefix=output_prefix,
        unsat_extra=None,
    )
    translate_payload["statement_path"] = str(args.statement.resolve())

    with result_json_path.open("w", encoding="utf-8") as handle:
        json.dump(translate_payload, handle, indent=2)

    sat_entry, unsat_entry = _select_smt_entries(translate_payload["smt_files"])
    sat_path = Path(sat_entry["path"])
    unsat_path = Path(unsat_entry["path"])
    context_snippet = None
    if args.sysml_context:
        context_snippet = args.sysml_context.read_text(encoding="utf-8")
    _write_sysml_model(
        output_path=args.sysml_output,
        translate_payload=translate_payload,
        sat_entry=sat_entry,
        unsat_entry=unsat_entry,
        context_snippet=context_snippet,
    )

    if not args.skip_sysml_compile:
        _compile_with_sysml_kernel(
            jar_path=args.sysml_jar,
            sysml_path=args.sysml_output,
            timeout=args.timeout,
        )

    artefacts = {
        "translate_json": str(result_json_path),
        "sat_smt": str(sat_path),
        "unsat_smt": str(unsat_path),
        "sysml_model": str(args.sysml_output),
    }
    return artefacts


def main() -> None:
    args = _parse_args()
    artefacts = asyncio.run(_run_pipeline(args))
    print("Pipeline completed. Artefacts:")
    for key, value in artefacts.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
