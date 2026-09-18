"""The execution policy shared by the command line and review service.

This records effective settings, not a second configurable generation path.
Legacy generator options remain available only through the explicit legacy CLI.
"""
from __future__ import annotations

import requirements_pipeline as generator

SCHEMA = "review_execution/1"
COMPILER_TIMEOUT = 90
SEMANTIC_REPAIRS = 0
SMT_FIX_ATTEMPTS = 1
SYSML_MODE = "domain"


def execution_configuration(engine: str, analysis_mode: str) -> dict:
    backend = generator._SOLVER_RUNNER.name
    result = {
        "schema": SCHEMA,
        "workflow": "shared_review",
        "engine": engine,
        "analysis_mode": analysis_mode,
        "intent_formalization": {
            "enabled": False,
            "reason": "The shared review profile preserves submitted requirement identities and wording; standalone intent drafting remains an explicit utility.",
        },
        "solver": {"backend": backend, "timeout_seconds": generator.SMT_SOLVER_TIMEOUT},
        "compilation": {"enabled": True, "timeout_seconds": COMPILER_TIMEOUT,
                        "target": "final contract-projected model and combined trace when present"},
        "design_check": {"enabled": analysis_mode == "check_design",
                         "requires_supplied_candidate_and_review": True,
                         "proposal_is_checked_automatically": False},
        "contracts": True,
        "assumption_ledger": True,
        "revision_comparison": "when a parent run is supplied",
        "scope": "CLI and GUI use the same workflow. Provider responses and installed tool versions can still differ between executions.",
    }
    if engine == "pipeline":
        import os
        from review_pipeline_adapter import _selected_model, _bounded_timeout
        model, model_source = _selected_model(os.environ)
        result["generation"] = {
            "provider": "codex", "model": model, "model_source": model_source,
            "sysml_mode": SYSML_MODE, "semantic_strict": True,
            "semantic_repair_attempts": SEMANTIC_REPAIRS,
            "smt_fix_attempts": SMT_FIX_ATTEMPTS,
            "tlr_typecheck": "error", "source_prompt_coverage": "full submitted source",
            "assumption_disclosure": True, "capture_provider_calls": True,
            "timeout_seconds": _bounded_timeout(),
        }
    else:
        result["generation"] = {"provider": None, "profile": "review_tlr/1",
                                "method": "deterministic grammar and SysML renderer"}
    return result


def solver_capability() -> dict:
    """Report the actual selected runner, including both portfolio executables."""
    import shutil
    runner = generator._SOLVER_RUNNER
    members = [runner.primary, runner.secondary] if runner.name == "portfolio" else [runner]
    tools = [{"backend": member.name, "path": getattr(member, "path", member.name)} for member in members]
    available = all(shutil.which(tool["path"]) for tool in tools)
    return {"available": bool(available), "backend": runner.name, "tools": tools,
            "detail": f"Shared solver: {runner.name}. Exact query, verdict, and diagnostics are recorded per run."}
