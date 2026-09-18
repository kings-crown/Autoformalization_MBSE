#!/usr/bin/env python3
"""Run the same reviewed requirements workflow as the GUI, without an HTTP server.

The JSON result has two separate members: ``run`` is the complete saved run;
``exports`` describes optional copies and never changes that run's evidence.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Sequence

ROOT = Path(__file__).resolve().parent.parent
_PAYLOAD_OPTIONS = (
    "statement", "format", "name", "engine", "analysis_mode", "behavior",
    "candidate_provenance", "reviewer", "rationale", "acknowledge_design",
    "parent_run_id", "revision_rationale", "parent_source_hash", "parent_evidence_hash",
)


class RequestError(ValueError):
    """An invalid CLI request; no run should be allocated."""


class ExportError(ValueError):
    """Requested copies cannot be produced without changing existing evidence."""


class WorkflowArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if "unrecognized arguments" in message:
            message += (". This command uses the reviewed GUI workflow. For legacy generator "
                        "options, use requirements_pipeline.py legacy --help.")
        super().error(message)


def argument_parser() -> argparse.ArgumentParser:
    parser = WorkflowArgumentParser(
        description="Run the GUI's requirements-to-SysML review workflow without starting a server.",
        epilog=("Exit status: 0 = workflow completed (inspect analysis verdicts separately); "
                "1 = execution/export failure; 2 = invalid request. Output is JSON containing "
                "the complete saved run and a separate export map. No model acceptance is recorded."),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--statement", type=Path, help="Requirement file (.txt, .csv, or .json).")
    source.add_argument("--request-json", type=Path, help="Exact RunInput JSON request; cannot be mixed with input settings.")
    parser.add_argument("--format", choices=("text", "csv", "json"), default=None, help="Override the requirement file format.")
    parser.add_argument("--name", default=None, help="Model name (default: Requirements model, matching the GUI).")
    parser.add_argument("--engine", choices=("local", "pipeline"), default=None, help="Generation engine (default: local).")
    parser.add_argument("--analysis-mode", choices=("requirements", "propose_design", "check_design"), default=None,
                        help="Analysis scope (default: requirements; no separate design is invented or checked).")
    parser.add_argument("--behavior", type=Path, help="Candidate behavior JSON file; requires check_design and explicit review.")
    parser.add_argument("--candidate-provenance", type=Path, help="JSON provenance map for the reviewed candidate statements.")
    parser.add_argument("--reviewer", help="Name/role of the engineer who reviewed the candidate design.")
    parser.add_argument("--rationale", help="Engineering rationale supporting the candidate and its assumptions.")
    parser.add_argument("--acknowledge-design", action="store_true", default=None,
                        help="Explicitly acknowledge review before checking the supplied candidate; never automatic.")
    parser.add_argument("--parent-run-id", help="Saved run to revise in the same data directory.")
    parser.add_argument("--revision-rationale", help="Explain the source, interpretation, or candidate change.")
    parser.add_argument("--parent-source-hash", help="Exact reviewed parent source hash for a design-check revision.")
    parser.add_argument("--parent-evidence-hash", help="Exact reviewed parent evidence hash for a design-check revision.")
    parser.add_argument("--data-dir", type=Path,
                        default=Path(os.getenv("MBSE_REVIEW_DATA_DIR", str(ROOT / "out/review_workbench"))),
                        help="Shared saved-run directory (default: MBSE_REVIEW_DATA_DIR or out/review_workbench).")
    parser.add_argument("--output-prefix", type=Path,
                        help="Copy each recorded artifact to <prefix>_<artifact filename>; destinations must not exist.")
    parser.add_argument("--sysml-output", type=Path,
                        help="Copy the generated domain/contract SysML artifact to this new file.")
    parser.add_argument("--traceability-output", type=Path,
                        help="Copy a separately emitted traceability SysML artifact; error if none was generated.")
    return parser


def _read_text(path: Path) -> str:
    try:
        # Preserve CRLF exactly, as a GUI file upload does; source hashes depend on it.
        return path.expanduser().read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise RequestError(f"Cannot read UTF-8 input {path}: {exc}") from exc


def _read_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise RequestError(f"Invalid {label} JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RequestError(f"{label} must be a JSON object: {path}")
    return value


def request_payload(args: argparse.Namespace) -> dict:
    """Translate flags into the exact API schema; the shared core validates meaning."""
    if args.request_json is not None:
        explicit = ["--" + option.replace("_", "-") for option in _PAYLOAD_OPTIONS
                    if getattr(args, option) is not None]
        if explicit:
            raise RequestError("--request-json cannot be combined with input settings: " + ", ".join(explicit))
        return _read_object(args.request_json, "RunInput request")
    fmt = args.format or {".txt": "text", ".csv": "csv", ".json": "json"}.get(args.statement.suffix.lower())
    if fmt is None:
        raise RequestError("Cannot infer the requirement format; use .txt, .csv, .json, or an explicit --format.")
    payload = {"text": _read_text(args.statement), "format": fmt}
    for option in ("name", "engine", "analysis_mode", "parent_run_id", "revision_rationale"):
        if getattr(args, option) is not None:
            payload[option] = getattr(args, option)
    if args.behavior is not None:
        payload["behavior"] = _read_object(args.behavior, "candidate behavior")
    if args.candidate_provenance is not None:
        payload["candidate_provenance"] = _read_object(args.candidate_provenance, "candidate provenance")
    review = {field: getattr(args, option) for field, option in (
        ("reviewer", "reviewer"), ("rationale", "rationale"), ("acknowledge", "acknowledge_design"),
        ("parent_source_hash", "parent_source_hash"), ("parent_evidence_hash", "parent_evidence_hash"),
    ) if getattr(args, option) is not None}
    if review:
        payload["design_review"] = review
    return payload


def _input_paths(args: argparse.Namespace) -> set[Path]:
    return {path.expanduser().resolve() for path in
            (args.statement, args.request_json, args.behavior, args.candidate_provenance) if path is not None}


def _destination(path: Path, data_root: Path, inputs: set[Path]) -> Path:
    path = path.expanduser().absolute()
    resolved = path.resolve()
    if resolved == data_root or data_root in resolved.parents:
        raise ExportError(f"Export destinations must be outside the saved-run directory: {path}")
    if resolved in inputs:
        raise ExportError(f"An export cannot replace an input file: {path}")
    if path.exists() or path.is_symlink():
        raise ExportError(f"Export destination already exists; choose a new path: {path}")
    return path


def validate_export_settings(args: argparse.Namespace) -> None:
    """Reject unsafe known paths before executing tools or allocating a run."""
    data_root = args.data_dir.expanduser().resolve()
    inputs = _input_paths(args)
    for path in (args.sysml_output, args.traceability_output):
        if path is not None:
            _destination(path, data_root, inputs)
    if args.output_prefix is not None:
        prefix = args.output_prefix.expanduser().absolute()
        if not prefix.name:
            raise ExportError("--output-prefix must include a filename prefix.")
        # The prefix is not itself written; every generated destination is checked later.
        parent = prefix.parent.resolve()
        if parent == data_root or data_root in parent.parents:
            raise ExportError("--output-prefix must be outside the saved-run directory.")
    if args.sysml_output and args.traceability_output and args.sysml_output.resolve() == args.traceability_output.resolve():
        raise ExportError("SysML and traceability exports require distinct destinations.")


def export_artifacts(store, run: dict, args: argparse.Namespace) -> dict:
    """Copy manifest-verified bytes without changing source, run, or prior exports."""
    if not any((args.output_prefix, args.sysml_output, args.traceability_output)):
        return {}
    run_dir = store.directory(run["id"]).resolve()
    manifest = {artifact["name"]: artifact for artifact in run.get("artifacts", [])}
    requested: list[tuple[str, Path, str]] = []
    if args.output_prefix is not None:
        prefix = args.output_prefix.expanduser().absolute()
        requested.extend((name, prefix.with_name(prefix.name + "_" + name), "artifact") for name in sorted(manifest))
        if not manifest:
            raise ExportError("This run has no recorded artifacts to export.")
    for option, model_key, label in (("sysml_output", "path", "sysml"), ("traceability_output", "trace_path", "traceability")):
        destination = getattr(args, option)
        if destination is None:
            continue
        recorded = (run.get("model") or {}).get(model_key)
        if not recorded:
            raise ExportError(f"No {label} SysML artifact was emitted; the saved run and partial artifacts remain available.")
        source = Path(recorded)
        if source.resolve().parent != run_dir:
            raise ExportError(f"The recorded {label} model is outside this run's artifact directory.")
        requested.append((source.name, destination, label))
    prepared = []
    destinations: set[Path] = set()
    for name, destination, role in requested:
        if name not in manifest or Path(name).name != name:
            raise ExportError(f"Artifact is not in this run's immutable manifest: {name}")
        source = run_dir / name
        if source.is_symlink() or not source.is_file() or source.resolve().parent != run_dir:
            raise ExportError(f"Recorded artifact is missing or unsafe to export: {name}")
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != manifest[name].get("sha256"):
            raise ExportError(f"Recorded artifact changed after analysis: {name}")
        destination = _destination(destination, store.root.resolve(), _input_paths(args))
        resolved = destination.resolve()
        if resolved in destinations:
            raise ExportError(f"More than one requested export targets the same path: {destination}")
        destinations.add(resolved)
        prepared.append((name, destination, role, content, digest))
    written = []
    try:
        for name, destination, role, content, digest in prepared:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as handle:
                written.append(destination)
                handle.write(content)
    except OSError:
        # All destinations were new. Remove only copies created by this attempt.
        for path in written:
            path.unlink(missing_ok=True)
        raise
    return {str(path): {"artifact": name, "role": role, "sha256": digest}
            for name, path, role, _content, digest in prepared}


def main(argv: Sequence[str] | None = None) -> int:
    args = argument_parser().parse_args(argv)
    try:
        payload = request_payload(args)
        validate_export_settings(args)
    except (RequestError, ExportError, OSError) as exc:
        print(f"Invalid request: {exc}", file=sys.stderr)
        return 2
    # Import after parsing so --help and malformed file requests do not load tools.
    from pydantic import ValidationError
    from review_workflow import RunInput, RunStore, WorkflowError, create_run, run_job
    try:
        request = RunInput.model_validate(payload)
        store = RunStore(args.data_dir.expanduser())
        with redirect_stdout(sys.stderr):
            queued = create_run(store, request)
    except (ValidationError, WorkflowError) as exc:
        print(f"Invalid request: {getattr(exc, 'detail', str(exc))}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Cannot initialize the run store: {exc}", file=sys.stderr)
        return 1
    print(f"Run {queued['id']} started in {store.directory(queued['id'])}.", file=sys.stderr)
    try:
        with redirect_stdout(sys.stderr):
            run_job(store, queued["id"])
        run = store.get(queued["id"])
    except (WorkflowError, OSError) as exc:
        print(f"Run execution failed: {getattr(exc, 'detail', str(exc))}", file=sys.stderr)
        return 1
    exports, errors = {}, []
    try:
        exports = export_artifacts(store, run, args)
    except (ExportError, OSError) as exc:
        errors.append(str(exc))
        print(f"Export failed: {exc}", file=sys.stderr)
    result = {"run": run, "exports": exports}
    if errors:
        result["export_errors"] = errors
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"Run {run['id']}: {run['status']}. Analysis verdicts and review blockers are in the JSON result.", file=sys.stderr)
    return 0 if run["status"] == "completed" and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
