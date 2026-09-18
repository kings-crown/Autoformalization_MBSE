#!/usr/bin/env python3
"""
Legacy generator wrapper with friendlier error reporting.

The shared CLI/GUI workflow is available through requirements_pipeline.py directly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class FailureDiagnosis:
    code: str
    title: str
    summary: str
    next_steps: List[str]


def _parse_args(argv: Optional[Sequence[str]] = None) -> Tuple[argparse.Namespace, List[str]]:
    parser = argparse.ArgumentParser(
        description=(
            "Run requirements_pipeline.py legacy with a user-friendly report layer "
            "for conflicts and common failure modes."
        )
    )
    parser.add_argument("--statement", required=True, type=Path)
    parser.add_argument("--output-prefix", required=True, type=Path)
    parser.add_argument("--sysml-output", required=True, type=Path)
    parser.add_argument("--sysml-mode", choices=["evidence", "architecture", "domain"])
    parser.add_argument("--model")
    parser.add_argument("--llm-provider", choices=["openai", "codex"])
    parser.add_argument("--sysml-context", type=Path)
    parser.add_argument("--traceability-output", type=Path)
    parser.add_argument("--skip-sysml-compile", action="store_true")
    parser.add_argument("--require-sysml-compile", action="store_true")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--semantic-strict", action="store_true",
                        help="Fail the pipeline if semantic checks do not pass.")
    parser.add_argument("--approve-weakened", action="store_true",
                        help="Allow repairs that weaken or temporally shift requirement encodings.")

    parser.add_argument(
        "--run-dir",
        type=Path,
        help=(
            "Place all artefacts under this directory. A CSV-named sub-folder"
        ),
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        help="Optional markdown report output path (default: <output-prefix>_run_report.md).",
    )
    parser.add_argument(
        "--show-raw-error",
        action="store_true",
        help="Also print raw pipeline stdout/stderr on failure.",
    )

    parser.add_argument("--codex-exec-timeout", type=int)
    parser.add_argument("--codex-reasoning-effort", choices=["low", "medium", "high", "xhigh"])
    parser.add_argument("--codex-stream", action="store_true")
    parser.add_argument("--codex-stream-json", action="store_true")
    parser.add_argument(
        "--include-full-tlr-in-smt-prompt",
        dest="include_full_tlr_in_smt_prompt",
        type=int,
        choices=[0, 1],
        help="Sets MBSE_INCLUDE_FULL_TLR_IN_SMT_PROMPT for this run.",
    )
    parser.add_argument(
        "--include-full-tlf-in-smt-prompt",
        dest="include_full_tlr_in_smt_prompt",
        type=int,
        choices=[0, 1],
        help=argparse.SUPPRESS,
    )

    args, passthrough = parser.parse_known_args(argv)
    return args, passthrough


def _pipeline_script_path() -> Path:
    return Path(__file__).with_name("requirements_pipeline.py")


def _build_pipeline_cmd(args: argparse.Namespace, passthrough: Sequence[str]) -> List[str]:
    cmd = [
        sys.executable,
        str(_pipeline_script_path()),
        "legacy",
        "--statement",
        str(args.statement),
        "--output-prefix",
        str(args.output_prefix),
        "--sysml-output",
        str(args.sysml_output),
    ]
    if args.sysml_mode:
        cmd.extend(["--sysml-mode", args.sysml_mode])
    if args.model:
        cmd.extend(["--model", args.model])
    if args.llm_provider:
        cmd.extend(["--llm-provider", args.llm_provider])
    if args.sysml_context:
        cmd.extend(["--sysml-context", str(args.sysml_context)])
    if args.traceability_output:
        cmd.extend(["--traceability-output", str(args.traceability_output)])
    if args.skip_sysml_compile:
        cmd.append("--skip-sysml-compile")
    if args.require_sysml_compile:
        cmd.append("--require-sysml-compile")
    if args.timeout is not None:
        cmd.extend(["--timeout", str(args.timeout)])
    if args.semantic_strict:
        cmd.append("--semantic-strict")
    if args.approve_weakened:
        cmd.append("--approve-weakened")
    cmd.extend(list(passthrough))
    return cmd


def _apply_env_overrides(args: argparse.Namespace) -> Dict[str, str]:
    env = os.environ.copy()
    if args.codex_exec_timeout is not None:
        env["CODEX_EXEC_TIMEOUT"] = str(args.codex_exec_timeout)
    if args.codex_reasoning_effort:
        env["CODEX_REASONING_EFFORT"] = args.codex_reasoning_effort
    if args.codex_stream:
        env["CODEX_STREAM"] = "1"
    if args.codex_stream_json:
        env["CODEX_STREAM_JSON"] = "1"
    if args.include_full_tlr_in_smt_prompt is not None:
        value = str(args.include_full_tlr_in_smt_prompt)
        env["MBSE_INCLUDE_FULL_TLR_IN_SMT_PROMPT"] = value
        # Legacy env var kept for backwards compatibility.
        env["MBSE_INCLUDE_FULL_TLF_IN_SMT_PROMPT"] = value
    return env


def _result_head(result: Any) -> str:
    text = str(result or "").strip()
    if not text:
        return "n/a"
    return text.splitlines()[0].strip()


def _expected_translate_path(output_prefix: Path) -> Path:
    base = output_prefix.with_suffix("") if output_prefix.suffix else output_prefix
    return base.with_name(base.name + "_translate.json")


def _parse_artefacts_from_stdout(stdout: str) -> Dict[str, str]:
    artefacts: Dict[str, str] = {}
    marker = "Pipeline completed. Artefacts:"
    marker_idx = stdout.find(marker)
    if marker_idx < 0:
        return artefacts
    tail = stdout[marker_idx + len(marker) :].splitlines()
    for line in tail:
        match = re.match(r"^\s{2}([A-Za-z0-9_]+):\s*(.*)$", line.rstrip())
        if match:
            artefacts[match.group(1)] = match.group(2)
    return artefacts


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _smt_snapshot_from_translate_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    snapshot = {"sat": {"status": "n/a", "result": "n/a"}, "unsat": {"status": "n/a", "result": "n/a"}}
    if not payload:
        return snapshot
    entries = payload.get("smt_files")
    if not isinstance(entries, list):
        return snapshot
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mode = str(entry.get("mode", "")).strip().lower()
        if mode not in snapshot:
            continue
        validation = entry.get("validation")
        if not isinstance(validation, dict):
            continue
        snapshot[mode] = {
            "status": str(validation.get("status", "n/a")),
            "result": _result_head(validation.get("result")),
        }
    return snapshot


def diagnose_failure(raw_log: str, smt_snapshot: Dict[str, Dict[str, str]]) -> FailureDiagnosis:
    lowered = raw_log.lower()
    sat_result = smt_snapshot.get("sat", {}).get("result", "n/a").lower()

    if "expected 'sat' validation to succeed" in lowered and ("first_line='unsat'" in lowered or sat_result == "unsat"):
        return FailureDiagnosis(
            code="requirements_conflict",
            title="Conflicting Requirement Set (SAT expected, UNSAT observed)",
            summary=(
                "The translated conjunction of requirements is inconsistent under the generated model and bounds. "
                "This is typically a real contradiction (or over-constrained encoding), not an infrastructure failure."
            ),
            next_steps=[
                "Inspect <output-prefix>_translate.json -> smt_iterations and identify mutually incompatible assertions.",
                "Temporarily drop or relax one suspect requirement at a time to isolate the conflicting pair/set.",
                "Encode assumptions explicitly (operating mode, scope, temporal window) to avoid accidental global constraints.",
            ],
        )

    if "logic does not support nonlinear arithmetic" in lowered:
        return FailureDiagnosis(
            code="nonlinear_logic_mismatch",
            title="SMT Logic Mismatch (nonlinear terms under linear logic)",
            summary=(
                "The generated fragment contains nonlinear arithmetic (e.g., x*y) while using a linear logic profile "
                "(such as QF_LIA)."
            ),
            next_steps=[
                "Linearize the expression if possible (e.g., replace product with bounded auxiliary constraints).",
                "Or switch the logic family to a nonlinear-capable one (for example QF_NIA) in generation settings.",
                "Re-run and check that SAT/UNSAT validations both report expected first-line results.",
            ],
        )

    if "sysml kernel does not accept `load <local-path>`" in lowered:
        return FailureDiagnosis(
            code="sysml_kernel_runtime_limit",
            title="SysML Compile Runtime Limitation",
            summary="The local SysML kernel runtime does not support path-based `load` compile checks in this mode.",
            next_steps=[
                "Run with --skip-sysml-compile for generation-only flow.",
                "Or run in a kernel/runtime that supports file-path load validation.",
                "Keep the generated domain and trace files; they are still useful for model review in SysIDE.",
            ],
        )

    if "openai_api_key" in lowered:
        return FailureDiagnosis(
            code="missing_openai_key",
            title="Missing OpenAI API Key",
            summary="OpenAI provider was requested, but API credentials were not available for translation.",
            next_steps=[
                "Set OPENAI_API_KEY, or use --llm-provider codex.",
            ],
        )

    return FailureDiagnosis(
        code="pipeline_error",
        title="Pipeline Execution Failed",
        summary="The run did not complete. Review the error excerpt and generated artefacts to locate the failing stage.",
        next_steps=[
            "Open the generated report and check stage outcomes plus the raw error excerpt.",
            "Re-run with --show-raw-error if you need full traceback in terminal.",
            "If failure is in SMT validation, inspect the latest sat/unsat fragment in the translate JSON.",
        ],
    )


def _load_semantic_checks(output_prefix: Path) -> Optional[Dict[str, Any]]:
    """Load the ``_semantic_checks.json`` artefact emitted by the pipeline."""
    base = output_prefix.with_suffix("") if output_prefix.suffix else output_prefix
    path = base.with_name(f"{base.name}_semantic_checks.json")
    return _load_json(path)


def _status_label(value: Any) -> str:
    if value is True:
        return "pass"
    if value is False:
        return "fail"
    return "n/a"


def _append_limited(lines: List[str], items: List[str], limit: int = 10) -> None:
    for item in items[:limit]:
        lines.append(f"  - {item}")
    if len(items) > limit:
        lines.append(f"  - ... and {len(items) - limit} more")


def _format_semantic_checks_section(sc: Dict[str, Any]) -> List[str]:
    """Return markdown lines summarising the semantic check results."""
    checks_obj = sc.get("checks")
    checks = checks_obj if isinstance(checks_obj, dict) else sc
    lines: List[str] = [
        "",
        "## Semantic Checks",
        "",
        f"- Passed: `{sc.get('passed', 'n/a')}`",
        f"- Repaired: `{sc.get('repaired', False)}`",
        f"- Repair exhausted: `{sc.get('repair_exhausted', False)}`",
        f"- Blocked on weakened: `{sc.get('blocked_on_weakened', False)}`",
        f"- Approve weakened: `{sc.get('approve_weakened', False)}`",
    ]

    named = checks.get("named_assertion_coverage")
    if isinstance(named, dict):
        lines.append(f"- named_assertion_coverage: `{_status_label(named.get('passed'))}`")
        details: List[str] = []
        missing = named.get("missing")
        if isinstance(missing, list):
            details.extend([f"missing named assertion: {rid}" for rid in missing])
        unexpected = named.get("unexpected")
        if isinstance(unexpected, list):
            details.extend([f"unexpected named assertion: {rid}" for rid in unexpected])
        _append_limited(lines, details)

    same_state = checks.get("same_state")
    if isinstance(same_state, dict):
        lines.append(f"- same_state: `{_status_label(same_state.get('passed'))}`")
        details = [
            str(issue.get("message", "")).strip()
            for issue in same_state.get("issues", [])
            if isinstance(issue, dict) and str(issue.get("message", "")).strip()
        ]
        _append_limited(lines, details)

    pairwise = checks.get("pairwise_conflict")
    if not isinstance(pairwise, dict):
        pairwise = checks.get("pairwise_conflicts")
    if isinstance(pairwise, dict):
        lines.append(f"- pairwise_conflict: `{_status_label(pairwise.get('passed'))}`")
        details = []
        if pairwise.get("skipped"):
            reason = str(pairwise.get("reason", "pairwise checks skipped")).strip()
            details.append(reason)
        for conflict in pairwise.get("conflicts", []):
            if isinstance(conflict, dict):
                msg = str(conflict.get("message", "")).strip()
                if msg:
                    details.append(msg)
        for probe in pairwise.get("solver_errors", []):
            if isinstance(probe, dict):
                msg = str(probe.get("message", "")).strip()
                if msg:
                    details.append(msg)
        _append_limited(lines, details)

    vacuity = checks.get("vacuity")
    if isinstance(vacuity, dict):
        lines.append(f"- vacuity: `{_status_label(vacuity.get('passed'))}`")
        details = []
        for key in ("vacuous_requirements", "tautological_antecedents", "solver_errors"):
            entries = vacuity.get(key, [])
            if isinstance(entries, list):
                for item in entries:
                    if isinstance(item, dict):
                        msg = str(item.get("message", "")).strip()
                        if msg:
                            details.append(msg)
        _append_limited(lines, details)

    symbol_drift = checks.get("symbol_drift")
    if isinstance(symbol_drift, dict):
        lines.append(f"- symbol_drift: `{_status_label(symbol_drift.get('passed'))}`")
        unknown = symbol_drift.get("unknown_symbols", [])
        if isinstance(unknown, list):
            details = [f"unknown symbol: {sym}" for sym in unknown]
            _append_limited(lines, details)

    return lines


def _format_repair_diff_section(sc: Dict[str, Any]) -> List[str]:
    """Return markdown lines for the repair diff classification table."""
    repair_diff = sc.get("repair_diff")
    if not isinstance(repair_diff, list) or not repair_diff:
        return []

    lines: List[str] = [
        "",
        "## Repair Diff",
        "",
        "| Requirement | Classification | Reason |",
        "|-------------|---------------|--------|",
    ]
    for entry in repair_diff:
        rid = entry.get("requirement_id", "?")
        cls = entry.get("classification", "?")
        reason = entry.get("reason", "")
        lines.append(f"| {rid} | `{cls}` | {reason} |")

    return lines


def _has_dangerous_repairs(sc: Optional[Dict[str, Any]]) -> bool:
    """Return True if any repair was classified as weakened or temporal_shifted."""
    if not sc:
        return False
    repair_diff = sc.get("repair_diff")
    if not isinstance(repair_diff, list):
        return False
    return any(
        entry.get("classification") in ("weakened", "temporal_shifted")
        for entry in repair_diff
    )


def _safe_run_label_from_statement(statement: Path) -> str:
    stem = statement.stem.strip() or "run"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return safe or "run"


def _next_available_run_dir(base_dir: Path, label: str) -> Path:
    candidate = base_dir / f"run_{label}"
    index = 1
    while candidate.exists():
        candidate = base_dir / f"run_{label}_{index}"
        index += 1
    return candidate


def _relocate_into_run_dir(args: argparse.Namespace) -> Path:
    """Create a CSV-named subdirectory under ``--run-dir`` and rewrite output paths."""
    label = _safe_run_label_from_statement(args.statement)
    run_dir = _next_available_run_dir(args.run_dir, label)
    run_dir.mkdir(parents=True, exist_ok=False)
    args.output_prefix = run_dir / args.output_prefix.name
    args.sysml_output = run_dir / args.sysml_output.name
    if args.traceability_output:
        args.traceability_output = run_dir / args.traceability_output.name
    if not args.report_path:
        args.report_path = run_dir / _default_report_path(args.output_prefix).name
    return run_dir


def _default_report_path(output_prefix: Path) -> Path:
    return output_prefix.with_name(f"{output_prefix.name}_run_report.md")


def build_report_markdown(
    *,
    args: argparse.Namespace,
    cmd: Sequence[str],
    return_code: int,
    artefacts: Dict[str, str],
    translate_path: Path,
    smt_snapshot: Dict[str, Dict[str, str]],
    diagnosis: Optional[FailureDiagnosis],
    raw_log: str,
    semantic_checks: Optional[Dict[str, Any]] = None,
) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    status = "success" if return_code == 0 else "failure"
    lines: List[str] = [
        "# MBSE Pipeline Run Report",
        "",
        f"- Timestamp (UTC): `{ts}`",
        f"- Status: `{status}`",
        f"- Return code: `{return_code}`",
        f"- Statement: `{args.statement}`",
        f"- Output prefix: `{args.output_prefix}`",
        f"- SysML mode: `{args.sysml_mode or 'default'}`",
        f"- LLM provider: `{args.llm_provider or 'default'}`",
        f"- Model: `{args.model or 'default'}`",
        f"- Command: `{' '.join(cmd)}`",
        "",
        "## Stage Outcomes",
        "",
        f"- Translate JSON: `{translate_path}` (`{'present' if translate_path.exists() else 'missing'}`)",
        f"- SAT validation result: `{smt_snapshot.get('sat', {}).get('result', 'n/a')}` (status `{smt_snapshot.get('sat', {}).get('status', 'n/a')}`)",
        f"- UNSAT validation result: `{smt_snapshot.get('unsat', {}).get('result', 'n/a')}` (status `{smt_snapshot.get('unsat', {}).get('status', 'n/a')}`)",
    ]

    if artefacts:
        lines.extend(["", "## Artefacts", ""])
        for key, value in artefacts.items():
            lines.append(f"- `{key}`: `{value}`")

    if diagnosis:
        lines.extend(
            [
                "",
                "## Diagnosis",
                "",
                f"- Code: `{diagnosis.code}`",
                f"- Title: {diagnosis.title}",
                f"- Summary: {diagnosis.summary}",
                "",
                "## Recommended Next Steps",
                "",
            ]
        )
        for step in diagnosis.next_steps:
            lines.append(f"1. {step}")

    if semantic_checks:
        lines.extend(_format_semantic_checks_section(semantic_checks))
        lines.extend(_format_repair_diff_section(semantic_checks))

    excerpt_lines = [line for line in raw_log.splitlines() if line.strip()]
    if excerpt_lines:
        excerpt_title = "Error Excerpt" if return_code != 0 else "Run Log Excerpt"
        lines.extend(["", f"## {excerpt_title}", "", "```text"])
        lines.extend(excerpt_lines[:80])
        lines.append("```")

    lines.append("")
    return "\n".join(lines)


def main() -> None:
    args, passthrough = _parse_args()

    run_dir: Optional[Path] = None
    if args.run_dir:
        run_dir = _relocate_into_run_dir(args)

    cmd = _build_pipeline_cmd(args, passthrough)
    env = _apply_env_overrides(args)

    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
    )

    artefacts = _parse_artefacts_from_stdout(completed.stdout)

    effective_prefix = args.output_prefix
    if "translate_json" in artefacts:
        tj = Path(artefacts["translate_json"])
        # translate_json is <prefix>_translate.json — strip the suffix to recover the prefix
        stem = tj.name
        if stem.endswith("_translate.json"):
            effective_prefix = tj.parent / stem[: -len("_translate.json")]
    elif "run_dir" in artefacts:
        effective_prefix = Path(artefacts["run_dir"]) / args.output_prefix.name

    translate_path = Path(artefacts.get("translate_json", _expected_translate_path(effective_prefix)))
    translate_payload = _load_json(translate_path)
    smt_snapshot = _smt_snapshot_from_translate_payload(translate_payload)

    raw_log = "\n".join([completed.stdout.strip(), completed.stderr.strip()]).strip()
    diagnosis: Optional[FailureDiagnosis] = None
    if completed.returncode != 0:
        diagnosis = diagnose_failure(raw_log, smt_snapshot)

    semantic_checks_data: Optional[Dict[str, Any]] = None
    sc_artefact = artefacts.get("semantic_checks_json")
    if sc_artefact and sc_artefact != "not_generated":
        semantic_checks_data = _load_json(Path(sc_artefact))
    if semantic_checks_data is None:
        semantic_checks_data = _load_semantic_checks(effective_prefix)

    report_path = args.report_path or _default_report_path(effective_prefix)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_text = build_report_markdown(
        args=args,
        cmd=cmd,
        return_code=completed.returncode,
        artefacts=artefacts,
        translate_path=translate_path,
        smt_snapshot=smt_snapshot,
        diagnosis=diagnosis,
        raw_log=raw_log,
        semantic_checks=semantic_checks_data,
    )
    report_path.write_text(report_text, encoding="utf-8")

    # --- Semantic gate: block on dangerous repairs unless explicitly approved ---
    if completed.returncode == 0 and _has_dangerous_repairs(semantic_checks_data) and not args.approve_weakened:
        print("Pipeline completed but semantic gate BLOCKED the result.")
        print("Repair introduced weakened or temporally-shifted requirement encodings.")
        print("Re-run with --approve-weakened to accept, or inspect the report.")
        print(f"Friendly report: {report_path}")
        raise SystemExit(2)

    if completed.returncode == 0:
        print("Pipeline run succeeded.")
        if run_dir is not None:
            print(f"Run directory: {run_dir}")
        elif "run_dir" in artefacts:
            print(f"Run directory: {artefacts['run_dir']}")
        print(f"Friendly report: {report_path}")
        if artefacts:
            print("Key artefacts:")
            for key, value in artefacts.items():
                print(f"  {key}: {value}")
        return

    if diagnosis is not None:
        print(f"Pipeline run failed: {diagnosis.title}")
        print(diagnosis.summary)
    else:
        print("Pipeline run failed.")
    print(f"Friendly report: {report_path}")
    if args.show_raw_error and raw_log:
        print(raw_log)
    raise SystemExit(completed.returncode or 1)


if __name__ == "__main__":
    main()
