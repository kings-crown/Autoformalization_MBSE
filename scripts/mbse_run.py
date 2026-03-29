#!/usr/bin/env python3
"""
Thin wrapper around requirements_pipeline.py with friendlier error reporting.
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
            "Run requirements_pipeline.py with a user-friendly report layer "
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
    cmd = _build_pipeline_cmd(args, passthrough)
    env = _apply_env_overrides(args)

    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
    )

    artefacts = _parse_artefacts_from_stdout(completed.stdout)
    translate_path = Path(artefacts.get("translate_json", _expected_translate_path(args.output_prefix)))
    translate_payload = _load_json(translate_path)
    smt_snapshot = _smt_snapshot_from_translate_payload(translate_payload)

    raw_log = "\n".join([completed.stdout.strip(), completed.stderr.strip()]).strip()
    diagnosis: Optional[FailureDiagnosis] = None
    if completed.returncode != 0:
        diagnosis = diagnose_failure(raw_log, smt_snapshot)

    report_path = args.report_path or _default_report_path(args.output_prefix)
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
    )
    report_path.write_text(report_text, encoding="utf-8")

    if completed.returncode == 0:
        print("Pipeline run succeeded.")
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
