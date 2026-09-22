#!/usr/bin/env python3
"""Requirements-to-SysML command entry point and legacy generation library.

The default command (also ``run``) uses the same review_workflow engine as the
GUI: python scripts/requirements_pipeline.py --statement requirements.csv
Select ``--engine pipeline`` for the audited Codex generation profile. The local
constraint engine and requirements-only analysis are the shared defaults.

The original generator remains available through the explicit ``legacy`` command:
    python scripts/requirements_pipeline.py legacy --statement requirements.csv \
        --output-prefix out/example --sysml-output out/example.sysml
Its optional intent formalization, provider selection, repair policy and SysML
modes are separate from the shared review profile. Utility subcommands
``translate``, ``harvest`` and ``formalize_intent`` are retained.

This module also supplies the existing generation and solver functions used by
the shared workflow. SYSML_KERNEL_JAR and MBSE_SOLVER configure installed tools.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from tlr_contracts import (
    as_requirements_tlr,
    convert_requirements_tlr_to_logical_form_tlr,
    ensure_requirements_tlr_contract,
)

# Optional runtime dependency; loaded lazily in _new_openai_client.
AsyncOpenAI = Any  # type: ignore[assignment,misc]

# ---- Core toolkit implementation (authoritative) ----
def _env_int(name: str, default: int, *, minimum: Optional[int] = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid environment variable {name}={raw!r}; expected an integer."
            ) from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(
            f"Invalid environment variable {name}={value!r}; expected >= {minimum}."
        )
    return value


def _env_float(name: str, default: float, *, minimum: Optional[float] = None) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        value = default
    else:
        try:
            value = float(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid environment variable {name}={raw!r}; expected a number."
            ) from exc
    if minimum is not None and value < minimum:
        raise RuntimeError(
            f"Invalid environment variable {name}={value!r}; expected >= {minimum}."
        )
    return value


DEFAULT_TRANSLATION_MODEL = os.getenv("OPENAI_TRANSLATION_MODEL", "gpt-4o")
DEFAULT_HARVEST_MODEL = os.getenv("OPENAI_HARVEST_MODEL", DEFAULT_TRANSLATION_MODEL)
DEFAULT_INTENT_MODEL = os.getenv("OPENAI_INTENT_MODEL", DEFAULT_TRANSLATION_MODEL)
DEFAULT_LLM_PROVIDER = os.getenv("MBSE_LLM_PROVIDER", "openai").strip().lower()
DEFAULT_CODEX_MODEL = os.getenv("CODEX_MBSE_MODEL", "gpt-5.4")
SMT_MAX_FIX_ATTEMPTS = _env_int("SMT_FIX_ATTEMPTS", 3, minimum=1)
SMT_SOLVER_TIMEOUT = _env_float("SMT_SOLVER_TIMEOUT", 10.0, minimum=0.1)
SMT_MAX_SEMANTIC_REPAIRS = _env_int("SMT_MAX_SEMANTIC_REPAIRS", 2, minimum=0)
MBSE_MAX_PAIRWISE_CHECKS = _env_int("MBSE_MAX_PAIRWISE_CHECKS", 190, minimum=0)


def ensure_api_key() -> None:
    """Raise a readable error if the OpenAI key is missing."""
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Export your key before running this script."
        )


def _new_openai_client() -> AsyncOpenAI:
    try:
        from openai import AsyncOpenAI as OpenAIAsyncClient
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "The 'openai' Python package is required for provider=openai. "
            "Install it (e.g., pip install openai) or use --provider codex."
        ) from exc
    return OpenAIAsyncClient()


def _normalise_provider(provider: str) -> str:
    resolved = (provider or DEFAULT_LLM_PROVIDER).strip().lower()
    if resolved not in {"openai", "codex"}:
        raise RuntimeError(f"Unsupported provider: {provider!r}. Expected 'openai' or 'codex'.")
    return resolved


def _env_true(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _tlf_typecheck_mode() -> str:
    raw = os.getenv("MBSE_TLR_TYPECHECK_MODE")
    if raw is None or raw.strip() == "":
        raw = os.getenv("MBSE_TLF_TYPECHECK_MODE", "error")
    raw = raw.strip().lower()
    if raw in {"off", "none", "disable", "disabled", "0", "false"}:
        return "off"
    if raw in {"warn", "warning"}:
        return "warn"
    return "error"


def _clip_text(value: str, max_chars: int) -> str:
    text = value.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]..."


def _compact_tlf_for_prompt(payload: Dict[str, Any]) -> Dict[str, Any]:
    requirements: List[Dict[str, Any]] = []
    for req in payload.get("requirements", []):
        if not isinstance(req, dict):
            continue
        requirements.append(
            {
                "id": req.get("id"),
                "category": req.get("category"),
                "temporal_kind": req.get("temporal_kind"),
                **({"formalization_status": req["formalization_status"],
                    "unresolved_ranges": req["unresolved_ranges"]}
                   if req.get("unresolved_ranges") else {}),
                "symbols": [
                    {
                        "name": sym.get("name"),
                        "type": sym.get("type"),
                        "role": sym.get("role"),
                    }
                    for sym in req.get("symbols", [])
                    if isinstance(sym, dict)
                ],
                "ranges": [
                    {
                        "symbol": rng.get("symbol"),
                        "lower": rng.get("lower"),
                        "upper": rng.get("upper"),
                        "lower_inclusive": rng.get("lower_inclusive"),
                        "upper_inclusive": rng.get("upper_inclusive"),
                    }
                    for rng in req.get("ranges", [])
                    if isinstance(rng, dict)
                ],
            }
        )
    return {
        "schema_version": payload.get("schema_version"),
        "source": {
            "statement_sha256": (
                payload.get("source", {}).get("statement_sha256")
                if isinstance(payload.get("source"), dict)
                else None
            ),
        },
        "requirements": requirements,
        "symbol_table": [
            {
                "name": sym.get("name"),
                "type": sym.get("type"),
                "role": sym.get("role"),
            }
            for sym in payload.get("symbol_table", [])
            if isinstance(sym, dict)
        ],
    }


def _resolve_codex_model(model: str) -> str:
    candidate = (model or "").strip()
    if not candidate:
        return DEFAULT_CODEX_MODEL
    if candidate == DEFAULT_TRANSLATION_MODEL:
        # Most existing calls default to OPENAI_TRANSLATION_MODEL; prefer a Codex-native default.
        return DEFAULT_CODEX_MODEL
    return candidate


def _strip_markdown_code_fences(text: str) -> str:
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned
    parts = cleaned.split("```")
    if len(parts) >= 3:
        body = parts[1]
        if "\n" in body:
            first, rest = body.split("\n", 1)
            # If first token looks like a language tag, drop it.
            if re.match(r"^[A-Za-z0-9_+-]+$", first.strip()):
                return rest.strip()
        return body.strip()
    return cleaned


def _extract_json_payload(text: str) -> Dict[str, Any]:
    cleaned = _strip_markdown_code_fences(text)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError(f"Expected JSON response but received: {cleaned[:400]!r}")
        payload = json.loads(cleaned[start : end + 1])
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object response, got {type(payload).__name__}.")
    return payload


def _run_codex_exec(prompt: str, model: str) -> str:
    codex_path = shutil.which("codex")
    if codex_path is None:
        raise RuntimeError("Codex CLI not found in PATH. Install/enable 'codex' first.")

    with tempfile.NamedTemporaryFile("r+", delete=False, encoding="utf-8") as tmp:
        tmp_path = Path(tmp.name)

    cmd = [
        codex_path,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--disable",
        "shell_tool",
        "--disable",
        "sqlite",
        "-c",
        "default_tools_enabled=false",
        "-c",
        "sandbox_permissions=[]",
        "-c",
        f'model_reasoning_effort="{os.getenv("CODEX_REASONING_EFFORT", "low")}"',
        "--output-last-message",
        str(tmp_path),
        "--cd",
        "/tmp",
    ]
    if _env_true("CODEX_STREAM_JSON", default=False):
        cmd.append("--json")
    if model:
        cmd.extend(["--model", model])
    cmd.append("-")

    timeout_seconds = _env_float("CODEX_EXEC_TIMEOUT", 180.0, minimum=1.0)
    stream_output = _env_true("CODEX_STREAM", default=False)
    try:
        if stream_output:
            result = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        else:
            result = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
    except subprocess.TimeoutExpired as exc:
        if stream_output:
            raise RuntimeError(
                f"codex exec timed out after {timeout_seconds:.0f}s. "
                "Streaming was enabled; inspect terminal output above for latest progress."
            ) from exc
        stdout = str(getattr(exc, "stdout", "") or "").strip()
        stderr = str(getattr(exc, "stderr", "") or "").strip()
        raise RuntimeError(
            f"codex exec timed out after {timeout_seconds:.0f}s. "
            "Increase CODEX_EXEC_TIMEOUT if needed. "
            f"stdout={stdout[:240]!r} stderr={stderr[:240]!r}"
        ) from exc

    try:
        answer = tmp_path.read_text(encoding="utf-8").strip()
    finally:
        tmp_path.unlink(missing_ok=True)

    if result.returncode != 0:
        if stream_output:
            raise RuntimeError(
                f"codex exec failed (exit {result.returncode}). "
                "Streaming was enabled; inspect terminal output above for details."
            )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        raise RuntimeError(
            "codex exec failed "
            f"(exit {result.returncode}). stdout={stdout[:400]!r} stderr={stderr[:400]!r}. "
            "Ensure Codex CLI is authenticated and network access is available."
        )
    if not answer:
        raise RuntimeError("codex exec returned an empty response.")
    return answer


async def _codex_chat_text(system_prompt: str, user_prompt: str, model: str) -> str:
    resolved_model = _resolve_codex_model(model)
    composed_prompt = (
        "System instructions:\n"
        f"{system_prompt.strip()}\n\n"
        "User request:\n"
        f"{user_prompt.strip()}\n"
    )
    response = await asyncio.to_thread(_run_codex_exec, composed_prompt, resolved_model)
    return _strip_markdown_code_fences(response).strip()


async def _codex_chat_json(system_prompt: str, user_prompt: str, model: str) -> Dict[str, Any]:
    json_prompt = (
        f"{user_prompt.rstrip()}\n\n"
        "Return strictly valid JSON only. Do not include markdown fences or commentary."
    )
    raw = await _codex_chat_text(system_prompt, json_prompt, model)
    return _extract_json_payload(raw)


async def _chat_text_response(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    provider: str = "openai",
    client: Optional[AsyncOpenAI] = None,
    max_tokens: int = 1024,
    temperature: float = 0.01,
) -> str:
    resolved_provider = _normalise_provider(provider)
    if resolved_provider == "codex":
        return await _codex_chat_text(system_prompt, user_prompt, model)

    llm_client = client or _new_openai_client()
    response = await llm_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    message = response.choices[0].message.content
    return (message or "").strip()


def load_text(path: Path) -> str:
    """Read UTF-8 text from disk."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(f"File not found: {path}") from exc


def _coerce_text(value: Any) -> str:
    """Best-effort coercion of model payload fields into text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "requirement", "statement", "content", "description", "value"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, (list, tuple)):
        parts = [_coerce_text(item) for item in value]
        return " ".join(part for part in parts if part)
    return str(value)


def normalise_whitespace(text: Any) -> str:
    """Collapse excessive whitespace to single spaces."""
    return re.sub(r"\s+", " ", _coerce_text(text).strip())


def _is_header_row(row: Sequence[str]) -> bool:
    known = {
        "id",
        "req_id",
        "requirement_id",
        "requirement",
        "text",
        "statement",
        "description",
    }
    lower = {normalise_whitespace(cell).lower() for cell in row if normalise_whitespace(cell)}
    return bool(lower & known)


def _extract_requirements_from_csv(source_path: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    with source_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        all_rows = [row for row in reader if any(normalise_whitespace(cell) for cell in row)]
    if not all_rows:
        return rows

    if _is_header_row(all_rows[0]):
        with source_path.open("r", encoding="utf-8", newline="") as handle:
            dict_reader = csv.DictReader(handle)
            for idx, row in enumerate(dict_reader, start=1):
                if not row:
                    continue
                lower_row = {str(k).strip().lower(): (v or "") for k, v in row.items()}
                req_id = normalise_whitespace(
                    lower_row.get("id")
                    or lower_row.get("req_id")
                    or lower_row.get("requirement_id")
                    or f"R{idx}"
                )
                text = normalise_whitespace(
                    lower_row.get("requirement")
                    or lower_row.get("text")
                    or lower_row.get("statement")
                    or lower_row.get("description")
                    or ""
                )
                if text:
                    rows.append({"id": req_id, "text": text})
        return rows

    for idx, row in enumerate(all_rows, start=1):
        candidate_id = normalise_whitespace(row[0]) if row else ""
        candidate_text = ""
        if len(row) >= 2:
            candidate_text = normalise_whitespace(" ".join(row[1:]))
        else:
            candidate_text = normalise_whitespace(row[0])
            if re.match(r"^[A-Za-z]+\d+$", candidate_id):
                candidate_text = ""
        req_id = candidate_id if re.match(r"^[A-Za-z_][A-Za-z0-9_\-]*$", candidate_id) else f"R{idx}"
        text = candidate_text or normalise_whitespace(" ".join(row))
        if text:
            rows.append({"id": req_id, "text": text})
    return rows


def load_requirements_source(source_path: Path) -> List[Dict[str, str]]:
    """
    Normalize requirements into [{id,text}, ...].

    Note: CLI entrypoints enforce a CSV-only prototype contract.
    """
    suffix = source_path.suffix.lower()
    if suffix == ".csv":
        requirements = _extract_requirements_from_csv(source_path)
    elif suffix == ".json":
        raw = json.loads(load_text(source_path))
        requirements = []
        if isinstance(raw, dict) and isinstance(raw.get("requirements"), list):
            for idx, item in enumerate(raw["requirements"], start=1):
                if not isinstance(item, dict):
                    continue
                text = normalise_whitespace(str(item.get("text", "")))
                if not text:
                    continue
                req_id = normalise_whitespace(str(item.get("id", f"R{idx}")))
                requirements.append({"id": req_id or f"R{idx}", "text": text})
        elif isinstance(raw, list):
            for idx, item in enumerate(raw, start=1):
                if isinstance(item, dict):
                    text = normalise_whitespace(str(item.get("text", "")))
                    req_id = normalise_whitespace(str(item.get("id", f"R{idx}")))
                else:
                    text = normalise_whitespace(str(item))
                    req_id = f"R{idx}"
                if text:
                    requirements.append({"id": req_id, "text": text})
        else:
            raise RuntimeError("Unsupported JSON format for requirements source.")
    else:
        lines = [normalise_whitespace(line) for line in load_text(source_path).splitlines()]
        requirements = []
        for idx, line in enumerate(lines, start=1):
            if not line:
                continue
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_\-]*)\s*[:,-]\s*(.+)$", line)
            if match:
                req_id, text = match.group(1), match.group(2)
            else:
                req_id, text = f"R{idx}", line
            requirements.append({"id": req_id, "text": normalise_whitespace(text)})

    if not requirements:
        raise RuntimeError(f"No requirements were found in {source_path}.")
    return requirements


def _infer_requirement_category(text: str) -> str:
    lower = text.lower()
    if any(token in lower for token in ("never", "shall not", "must not", "unsafe", "violate", "error")):
        return "safety"
    if any(token in lower for token in ("within", "eventually", "before", "after", "until", "latency", "seconds", "ms")):
        return "liveness"
    if any(token in lower for token in ("throughput", "performance", "memory", "cpu")):
        return "performance"
    if any(token in lower for token in ("assume", "provided", "constraint", "bounded")):
        return "constraint"
    return "functional"


def _infer_temporal_scope(text: str) -> str:
    lower = text.lower()
    if any(token in lower for token in ("within", "eventually", "before", "after", "until")):
        return "bounded_response"
    if any(token in lower for token in ("always", "never", "shall not", "must not")):
        return "invariant"
    return "event_triggered"


def _sanitize_symbol_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "symbol"
    if re.match(r"^[0-9]", cleaned):
        cleaned = f"s_{cleaned}"
    return cleaned


def _symbol_unit(symbol_type: str) -> Optional[str]:
    if symbol_type == "Bool":
        return None
    return "unspecified"


def _extract_symbols_from_text(text: str, req_id: str) -> List[Dict[str, Any]]:
    lower = text.lower()
    hints = [
        ("max acceleration", "max_acceleration", "Real", "constant"),
        ("acceleration", "acceleration", "Real", "state"),
        ("target speed", "target_speed", "Real", "state"),
        ("actual speed", "actual_speed", "Real", "state"),
        ("speed", "speed", "Real", "state"),
        ("capacity", "capacity", "Real", "state"),
        ("voltage", "voltage", "Real", "state"),
        ("current", "current", "Real", "state"),
        ("temperature", "temperature", "Real", "state"),
    ]
    symbols: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for token, name, symbol_type, role in hints:
        if token in lower and name not in seen:
            seen.add(name)
            symbols.append(
                {
                    "name": name,
                    "type": symbol_type,
                    "unit": _symbol_unit(symbol_type),
                    "role": role,
                }
            )

    for raw in re.findall(r"\b[A-Z][A-Za-z0-9_]*\.[A-Za-z0-9_]+\b", text):
        name = _sanitize_symbol_name(raw.replace(".", "_"))
        if name in seen:
            continue
        seen.add(name)
        symbols.append(
            {
                "name": name,
                "type": "Real",
                "unit": _symbol_unit("Real"),
                "role": "derived",
            }
        )

    if not symbols:
        fallback = _sanitize_symbol_name(f"{req_id}_holds")
        symbols.append(
            {
                "name": fallback,
                "type": "Bool",
                "unit": None,
                "role": "derived",
            }
        )
    return symbols


def _select_range_symbol(clause: str, symbols: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    lower = clause.lower()
    for symbol in symbols:
        name_tokens = symbol["name"].replace("_", " ")
        if name_tokens in lower:
            return symbol
    for symbol in symbols:
        if symbol.get("type") in {"Int", "Real"}:
            return symbol
    return symbols[0]


def _extract_ranges_from_text(text: str, symbols: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    clauses = [
        segment.strip()
        for segment in re.split(r"[.;\n]+", text)
        if segment and segment.strip()
    ]
    ranges: List[Dict[str, Any]] = []

    for clause in clauses:
        lower_clause = clause.lower()
        symbol = _select_range_symbol(clause, symbols)
        unit = symbol.get("unit")

        eq_match = re.search(
            r"(?:equals?|is)\s+(?:the\s+constant\s+value\s+)?(-?\d+(?:\.\d+)?)",
            lower_clause,
        )
        if eq_match:
            value = eq_match.group(1)
            ranges.append(
                {
                    "symbol": symbol["name"],
                    "lower": value,
                    "upper": value,
                    "lower_inclusive": True,
                    "upper_inclusive": True,
                    "unit": unit,
                    "origin_text": clause,
                }
            )

        if "non-negative" in lower_clause:
            ranges.append(
                {
                    "symbol": symbol["name"],
                    "lower": "0.0",
                    "upper": None,
                    "lower_inclusive": True,
                    "upper_inclusive": None,
                    "unit": unit,
                    "origin_text": clause,
                }
            )

        ge_match = re.search(r">=\s*(-?\d+(?:\.\d+)?)", lower_clause)
        if ge_match:
            ranges.append(
                {
                    "symbol": symbol["name"],
                    "lower": ge_match.group(1),
                    "upper": None,
                    "lower_inclusive": True,
                    "upper_inclusive": None,
                    "unit": unit,
                    "origin_text": clause,
                }
            )

        gt_match = re.search(r">\s*(-?\d+(?:\.\d+)?)", lower_clause)
        if gt_match and not ge_match:
            ranges.append(
                {
                    "symbol": symbol["name"],
                    "lower": gt_match.group(1),
                    "upper": None,
                    "lower_inclusive": False,
                    "upper_inclusive": None,
                    "unit": unit,
                    "origin_text": clause,
                }
            )

        le_text_match = re.search(
            r"less than or equal to\s+(-?\d+(?:\.\d+)?)",
            lower_clause,
        )
        if le_text_match:
            ranges.append(
                {
                    "symbol": symbol["name"],
                    "lower": None,
                    "upper": le_text_match.group(1),
                    "lower_inclusive": None,
                    "upper_inclusive": True,
                    "unit": unit,
                    "origin_text": clause,
                }
            )

        lt_text_match = re.search(r"less than\s+(-?\d+(?:\.\d+)?)", lower_clause)
        if lt_text_match and "less than or equal to" not in lower_clause:
            ranges.append(
                {
                    "symbol": symbol["name"],
                    "lower": None,
                    "upper": lt_text_match.group(1),
                    "lower_inclusive": None,
                    "upper_inclusive": False,
                    "unit": unit,
                    "origin_text": clause,
                }
            )

        le_match = re.search(r"<=\s*(-?\d+(?:\.\d+)?)", lower_clause)
        if le_match:
            ranges.append(
                {
                    "symbol": symbol["name"],
                    "lower": None,
                    "upper": le_match.group(1),
                    "lower_inclusive": None,
                    "upper_inclusive": True,
                    "unit": unit,
                    "origin_text": clause,
                }
            )

        lt_match = re.search(r"<\s*(-?\d+(?:\.\d+)?)", lower_clause)
        if lt_match and not le_match:
            ranges.append(
                {
                    "symbol": symbol["name"],
                    "lower": None,
                    "upper": lt_match.group(1),
                    "lower_inclusive": None,
                    "upper_inclusive": False,
                    "unit": unit,
                    "origin_text": clause,
                }
            )

    unique: List[Dict[str, Any]] = []
    seen = set()
    for item in ranges:
        key = (
            item["symbol"],
            item["lower"],
            item["upper"],
            item["lower_inclusive"],
            item["upper_inclusive"],
            item["origin_text"],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _validate_tlf_schema(payload: Dict[str, Any]) -> None:
    required_top = {
        "schema_version",
        "source",
        "requirements",
        "symbol_table",
        "traceability",
    }
    missing_top = required_top - set(payload.keys())
    if missing_top:
        raise RuntimeError(f"TLR schema missing top-level keys: {sorted(missing_top)}")
    if not isinstance(payload["requirements"], list) or not payload["requirements"]:
        raise RuntimeError("TLR schema requires a non-empty `requirements` list.")
    if not isinstance(payload["symbol_table"], list):
        raise RuntimeError("TLR schema requires `symbol_table` to be a list.")
    if not isinstance(payload["traceability"], list):
        raise RuntimeError("TLR schema requires `traceability` to be a list.")

    requirement_keys = {
        "id",
        "text",
        "category",
        "temporal_kind",
        "symbols",
        "ranges",
        "source_span",
    }
    symbol_keys = {"name", "type", "unit", "role"}
    range_keys = {
        "symbol",
        "lower",
        "upper",
        "lower_inclusive",
        "upper_inclusive",
        "unit",
        "origin_text",
    }

    for req in payload["requirements"]:
        if not isinstance(req, dict):
            raise RuntimeError("TLR schema error: each requirement entry must be an object.")
        missing_req = requirement_keys - set(req.keys())
        if missing_req:
            raise RuntimeError(f"TLR requirement missing keys: {sorted(missing_req)}")
        if not isinstance(req["id"], str) or not req["id"].strip():
            raise RuntimeError("TLR requirement id must be a non-empty string.")
        if not isinstance(req["symbols"], list):
            raise RuntimeError("TLR requirement symbols must be a list.")
        if not isinstance(req["ranges"], list):
            raise RuntimeError("TLR requirement ranges must be a list.")
        span = req["source_span"]
        if not isinstance(span, dict):
            raise RuntimeError("TLR requirement source_span must be an object.")
        if not isinstance(span.get("line_start"), int) or not isinstance(span.get("line_end"), int):
            raise RuntimeError("TLR source_span must contain integer line_start/line_end.")

        for symbol in req["symbols"]:
            if not isinstance(symbol, dict):
                raise RuntimeError("TLR symbol entry must be an object.")
            missing_symbol = symbol_keys - set(symbol.keys())
            if missing_symbol:
                raise RuntimeError(f"TLR symbol missing keys: {sorted(missing_symbol)}")

        for range_item in req["ranges"]:
            if not isinstance(range_item, dict):
                raise RuntimeError("TLR range entry must be an object.")
            missing_range = range_keys - set(range_item.keys())
            if missing_range:
                raise RuntimeError(f"TLR range missing keys: {sorted(missing_range)}")


def _native_typecheck_tlf(payload: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    allowed_symbol_types = {"Bool", "Int", "Integer", "Real"}
    symbol_types: Dict[str, str] = {}
    symbol_units: Dict[str, Optional[str]] = {}

    def _push_error(code: str, message: str, **extra: Any) -> None:
        entry = {"code": code, "message": message}
        entry.update(extra)
        errors.append(entry)

    def _push_warning(code: str, message: str, **extra: Any) -> None:
        entry = {"code": code, "message": message}
        entry.update(extra)
        warnings.append(entry)

    def _as_num(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    for idx, symbol in enumerate(payload.get("symbol_table", [])):
        if not isinstance(symbol, dict):
            _push_error("BAD_SYMBOL_ENTRY", f"symbol_table[{idx}] must be an object.")
            continue
        name = str(symbol.get("name", "")).strip()
        symbol_type = str(symbol.get("type", "")).strip()
        unit = symbol.get("unit")
        if not name:
            _push_error("EMPTY_SYMBOL_NAME", f"symbol_table[{idx}] has an empty name.")
            continue
        if not symbol_type:
            _push_error("EMPTY_SYMBOL_TYPE", f"symbol_table[{idx}] symbol {name!r} has empty type.")
            continue
        if symbol_type not in allowed_symbol_types:
            _push_warning(
                "UNKNOWN_SYMBOL_TYPE",
                f"symbol {name!r} uses non-standard type {symbol_type!r}.",
            )
        previous_type = symbol_types.get(name)
        if previous_type and previous_type != symbol_type:
            _push_error(
                "SYMBOL_TYPE_CONFLICT",
                f"symbol {name!r} has conflicting types {previous_type!r} and {symbol_type!r}.",
            )
        symbol_types[name] = symbol_type
        symbol_units[name] = None if unit is None else str(unit)

    numeric_symbol_types = {"Int", "Integer", "Real"}
    for req_index, req in enumerate(payload.get("requirements", [])):
        if not isinstance(req, dict):
            _push_error("BAD_REQUIREMENT_ENTRY", f"requirements[{req_index}] must be an object.")
            continue
        req_id = str(req.get("id", f"R{req_index + 1}"))
        if req.get("unresolved_ranges"):
            _push_warning(
                "UNRESOLVED_NUMERIC_BINDING",
                f"Requirement {req_id} has numeric observations awaiting quantity, unit, and scope interpretation; these are not executable ranges.",
                requirement_id=req_id,
            )
        local_symbol_types: Dict[str, str] = {}
        for sym in req.get("symbols", []):
            if not isinstance(sym, dict):
                continue
            sname = str(sym.get("name", "")).strip()
            stype = str(sym.get("type", "")).strip()
            if sname and stype:
                local_symbol_types[sname] = stype

        for range_index, range_item in enumerate(req.get("ranges", [])):
            if not isinstance(range_item, dict):
                _push_error(
                    "BAD_RANGE_ENTRY",
                    f"requirements[{req_index}].ranges[{range_index}] must be an object.",
                    requirement_id=req_id,
                )
                continue
            path = f"requirements[{req_index}].ranges[{range_index}]"
            symbol_name = str(range_item.get("symbol", "")).strip()
            if not symbol_name:
                _push_error(
                    "EMPTY_RANGE_SYMBOL",
                    f"{path} has empty symbol reference.",
                    requirement_id=req_id,
                )
                continue

            inferred_type = symbol_types.get(symbol_name) or local_symbol_types.get(symbol_name)
            if inferred_type is None:
                _push_error(
                    "RANGE_UNKNOWN_SYMBOL",
                    f"{path} references unknown symbol {symbol_name!r}.",
                    requirement_id=req_id,
                )
                continue
            if inferred_type not in numeric_symbol_types:
                _push_error(
                    "RANGE_NON_NUMERIC_SYMBOL",
                    f"{path} references non-numeric symbol {symbol_name!r}:{inferred_type}.",
                    requirement_id=req_id,
                )

            lower = range_item.get("lower")
            upper = range_item.get("upper")
            lower_num = _as_num(lower)
            upper_num = _as_num(upper)
            if lower is not None and lower_num is None:
                _push_error(
                    "BAD_LOWER_BOUND",
                    f"{path} lower bound {lower!r} is not numeric.",
                    requirement_id=req_id,
                )
            if upper is not None and upper_num is None:
                _push_error(
                    "BAD_UPPER_BOUND",
                    f"{path} upper bound {upper!r} is not numeric.",
                    requirement_id=req_id,
                )
            if lower_num is not None and upper_num is not None and lower_num > upper_num:
                _push_error(
                    "INVERTED_RANGE",
                    f"{path} has lower > upper for symbol {symbol_name!r}.",
                    requirement_id=req_id,
                )

            range_unit = range_item.get("unit")
            range_unit_text = None if range_unit is None else str(range_unit)
            symbol_unit = symbol_units.get(symbol_name)
            if (
                symbol_unit not in {None, "", "unspecified"}
                and range_unit_text not in {None, "", symbol_unit}
            ):
                _push_warning(
                    "UNIT_MISMATCH",
                    f"{path} unit {range_unit_text!r} differs from symbol unit {symbol_unit!r} for {symbol_name!r}.",
                    requirement_id=req_id,
                )

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


def _build_tlf_payload(
    statement_path: Path,
    statement_text: str,
    requirements: Sequence[Dict[str, str]],
) -> Dict[str, Any]:
    requirement_entries: List[Dict[str, Any]] = []
    symbol_table: Dict[str, Dict[str, Any]] = {}
    traceability: List[Dict[str, Any]] = []

    for idx, req in enumerate(requirements, start=1):
        req_id = normalise_whitespace(req.get("id", f"R{idx}")) or f"R{idx}"
        req_text = normalise_whitespace(req.get("text", ""))
        if not req_text:
            continue

        symbols = _extract_symbols_from_text(req_text, req_id)
        candidate_ranges = _extract_ranges_from_text(req_text, symbols)
        local_types = {symbol["name"]: symbol["type"] for symbol in symbols}
        ranges, unresolved_ranges = [], []
        for candidate in candidate_ranges:
            if local_types.get(candidate["symbol"]) != "Bool":
                ranges.append(candidate)
                continue
            # A fallback proposition is not a measured quantity. Preserve the
            # source and heuristic observation for later interpretation rather
            # than inventing a numeric type or an unconditional scoped bound.
            unresolved_ranges.append({
                "status": "needs_interpretation", "executable": False,
                "reason": "A numeric bound was detected, but no numeric quantity was identified. The Boolean fallback must not be used in arithmetic. Units, applicability, and population conditions require explicit interpretation.",
                "candidate": candidate, "candidate_is_complete": False,
                "original_text": req_text,
                "numeric_mentions": [
                    {"text": match.group(0), "start": match.start(), "end": match.end()}
                    for match in re.finditer(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?![\w.])", req_text)
                ],
            })
        category = _infer_requirement_category(req_text)
        temporal_kind = _infer_temporal_scope(req_text)

        for symbol in symbols:
            symbol_table.setdefault(symbol["name"], symbol)

        requirement_entries.append(
            {
                "id": req_id,
                "text": req_text,
                "category": category,
                "temporal_kind": temporal_kind,
                "symbols": symbols,
                "ranges": ranges,
                **({"formalization_status": "needs_interpretation", "unresolved_ranges": unresolved_ranges}
                   if unresolved_ranges else {}),
                "source_span": {
                    "line_start": idx,
                    "line_end": idx,
                },
            }
        )
        traceability.append(
            {
                "requirement_id": req_id,
                "tlf_requirement_index": idx - 1,
                "source_path": str(statement_path),
                "source_span": {
                    "line_start": idx,
                    "line_end": idx,
                },
            }
        )

    if not requirement_entries:
        raise RuntimeError("Unable to construct TLR payload: no requirements were extracted.")

    payload: Dict[str, Any] = {
        "schema_version": "1.0",
        "source": {
            "statement_path": str(statement_path),
            "statement_sha256": hashlib.sha256(statement_text.encode("utf-8")).hexdigest(),
        },
        "requirements": requirement_entries,
        "symbol_table": sorted(symbol_table.values(), key=lambda item: item["name"]),
        "traceability": traceability,
    }
    payload = ensure_requirements_tlr_contract(payload)
    _validate_tlf_schema(payload)
    mode = _tlf_typecheck_mode()
    if mode == "off":
        payload["typecheck"] = {
            "ok": True,
            "skipped": True,
            "errors": [],
            "warnings": [],
        }
    else:
        typecheck = _native_typecheck_tlf(payload)
        payload["typecheck"] = typecheck
        if not typecheck["ok"]:
            summary = (
                f"Native TLR typecheck found {len(typecheck['errors'])} error(s). "
                f"First error: {typecheck['errors'][0]}"
            )
            if mode == "error":
                raise RuntimeError(summary)
            print(f"Warning: {summary}", file=sys.stderr)
    return payload


def _requirements_for_tlf(statement_path: Path, statement_text: str) -> List[Dict[str, str]]:
    try:
        return load_requirements_source(statement_path)
    except Exception:  # noqa: BLE001
        normalized = [normalise_whitespace(line) for line in statement_text.splitlines()]
        requirements: List[Dict[str, str]] = []
        for idx, line in enumerate(normalized, start=1):
            if not line:
                continue
            requirements.append({"id": f"R{idx}", "text": line})
        if requirements:
            return requirements
        fallback = normalise_whitespace(statement_text)
        return [{"id": "R1", "text": fallback}]


async def formalize_intent_with_llm(
    requirements: Sequence[Dict[str, str]],
    model: str,
    set_id: str,
    title: str,
    system_name: str,
    provider: str = "openai",
    client: Optional[AsyncOpenAI] = None,
) -> Dict[str, Any]:
    requirements_text = "\n".join(
        f"- {req['id']}: {normalise_whitespace(req['text'])}" for req in requirements
    )

    system_prompt = (
        "You are a formal-methods requirements engineer. Convert each requirement into a machine-oriented "
        "intent package that supports MBSE autoformalization."
    )
    user_prompt = (
        "Given the requirements list, return strict JSON only with this shape:\n"
        "{\n"
        '  "intent_profile": {\n'
        '    "system_context": string,\n'
        '    "verification_objective": string,\n'
        '    "global_assumptions": [string, ...],\n'
        '    "non_goals": [string, ...]\n'
        "  },\n"
        '  "formalization_requirements": [\n'
        "    {\n"
        '      "id": string,\n'
        '      "text": string,\n'
        '      "intent": string,\n'
        '      "category": "safety"|"liveness"|"performance"|"functional"|"constraint",\n'
        '      "temporal_scope": "invariant"|"bounded_response"|"event_triggered",\n'
        '      "priority": "high"|"medium"|"low",\n'
        '      "signals": [string, ...],\n'
        '      "parameters": [{"name": string, "type": string, "constraint": string}, ...],\n'
        '      "formal_candidate": string,\n'
        '      "acceptance_checks": [string, ...],\n'
        '      "open_questions": [string, ...]\n'
        "    }\n"
        "  ],\n"
        '  "ambiguities": [\n'
        "    {\n"
        '      "requirementId": string,\n'
        '      "issue": string,\n'
        '      "question": string,\n'
        '      "impact": "low"|"medium"|"high"\n'
        "    }\n"
        "  ],\n"
        '  "traceability_summary": {\n'
        '    "total_requirements": number,\n'
        '    "covered_ids": [string, ...],\n'
        '    "missing_ids": [string, ...]\n'
        "  }\n"
        "}\n\n"
        f"Requirement set id: {set_id}\n"
        f"Title: {title}\n"
        f"System: {system_name}\n"
        "Requirements:\n"
        f"{requirements_text}\n\n"
        "Rules:\n"
        "- Preserve each requirement id exactly in formalization_requirements.\n"
        "- Keep acceptance checks verifiable and concrete.\n"
        "- Keep formal_candidate concise and implementation-agnostic.\n"
        "- Prefer empty lists over omitted keys."
    )

    resolved_provider = _normalise_provider(provider)
    if resolved_provider == "codex":
        return await _codex_chat_json(system_prompt, user_prompt, model)

    llm_client = client or _new_openai_client()
    response = await llm_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=2048,
        temperature=0.01,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content
    return json.loads(raw)


def _to_requirement_set(
    set_id: str,
    title: str,
    system_name: str,
    formalization_requirements: Sequence[Dict[str, Any]],
    assumptions: Sequence[str],
) -> Dict[str, Any]:
    normalized_requirements = []
    for idx, item in enumerate(formalization_requirements, start=1):
        req_id = normalise_whitespace(str(item.get("id", f"R{idx}"))) or f"R{idx}"
        text = normalise_whitespace(str(item.get("text", "")))
        if not text:
            continue
        normalized_requirements.append({"id": req_id, "text": text})
    if not normalized_requirements:
        raise RuntimeError("formalize_intent produced no usable requirements.")
    return {
        "id": set_id,
        "title": title,
        "system": system_name,
        "requirements": normalized_requirements,
        "assumptions": [normalise_whitespace(item) for item in assumptions if normalise_whitespace(item)],
    }


def chunk_text(text: str, max_chars: int = 4000) -> Sequence[str]:
    """
    Split a long document into manageable chunks without cutting sentences aggressively.

    The implementation uses a greedy word-based packer so it works even when the source
    lacks paragraph breaks (e.g. OCR-derived text).
    """
    words = text.split()
    if not words:
        return []

    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    for word in words:
        word_len = len(word) + (1 if current else 0)
        if current and current_len + word_len > max_chars:
            chunks.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += word_len

    if current:
        chunks.append(" ".join(current))

    return chunks


def strip_code_fences(fragment: str) -> str:
    """Remove Markdown code fences and leading/trailing whitespace."""
    if "```" in fragment:
        parts = fragment.split("```")
        # Prefer the first fenced body
        if len(parts) >= 2:
            fragment = parts[1]
    fragment = fragment.strip()
    # Remove language identifier tokens (e.g., smtlib)
    if "\n" in fragment:
        first_line, rest = fragment.split("\n", 1)
        if first_line.strip().lower() in {"smt", "smtlib", "smt2"}:
            fragment = rest
    else:
        lower = fragment.lower()
        if lower in {"smt", "smtlib", "smt2"}:
            fragment = ""
    return fragment.strip()


def ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def prepare_unsat_variant(sat_fragment: str, extra: str) -> str:
    lines = sat_fragment.strip().splitlines()
    while lines and lines[-1].strip().lower() in {"(check-sat)", "(get-model)"}:
        lines.pop()
    body = "\n".join(lines).strip()
    parts = [body]
    extra = extra.strip()
    if extra:
        parts.append(extra)
    parts.append("(check-sat)")
    return ensure_trailing_newline("\n\n".join(parts))


async def translate_to_natural_language(
    statement: str,
    model: str,
    provider: str = "openai",
    client: Optional[AsyncOpenAI] = None,
) -> str:
    """Translate a structured statement into descriptive natural language."""
    system_prompt = (
        "You are an expert in translating structured statements into descriptive natural language."
    )
    user_prompt = (
        "You are an expert in analyzing statements.\n"
        "Please read the following text and explain it in plain English:\n\n"
        f"{statement}"
    )
    return await _chat_text_response(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
        provider=provider,
        client=client,
        max_tokens=1024,
        temperature=0.01,
    )


async def generate_informal_statement(
    natural_language_text: str,
    model: str,
    provider: str = "openai",
    client: Optional[AsyncOpenAI] = None,
) -> str:
    """Convert a plain-English description into a mathematical-style lemma."""
    system_prompt = "You are an expert in mathematical formalisation."
    user_prompt = (
        "Convert the following plain-English description into a concise, mathematically styled statement "
        "or proposition:\n\n"
        f"{natural_language_text}\n\n"
        "Format it as if you're writing a short lemma statement in mathematical language (no proof)."
    )
    return await _chat_text_response(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
        provider=provider,
        client=client,
        max_tokens=1024,
        temperature=0.01,
    )


async def generate_informal_proof(
    informal_statement: str,
    model: str,
    provider: str = "openai",
    client: Optional[AsyncOpenAI] = None,
) -> str:
    """Produce an informal proof sketch for the lemma statement."""
    system_prompt = "You are an expert in mathematical reasoning."
    user_prompt = (
        "Provide a brief, high-level proof sketch or argument supporting the following statement:\n\n"
        f"{informal_statement}\n\n"
        "Keep it at the level of an informal mathematical proof."
    )
    return await _chat_text_response(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
        provider=provider,
        client=client,
        max_tokens=1024,
        temperature=0.01,
    )


async def generate_smt_skeleton(
    extended_statement: str,
    informal_proof: str,
    model: str,
    solver_feedback: Optional[str] = None,
    provider: str = "openai",
    client: Optional[AsyncOpenAI] = None,
    requirement_ids: Optional[List[str]] = None,
) -> str:
    """Ask the model to draft an SMT-LIB sketch that captures the requirements context."""
    system_prompt = (
        "You are an SMT engineer. Produce ONLY an SMT-LIB snippet containing declarations, "
        "assumptions, and assertions that reflect the described requirements. Finish with `(check-sat)`."
    )

    named_assertion_block = ""
    if requirement_ids:
        ids_list = ", ".join(requirement_ids)
        named_assertion_block = f"""
CRITICAL STRUCTURAL REQUIREMENT — per-requirement named assertions:
Begin the fragment with: (set-option :produce-unsat-cores true)

For EACH requirement ID listed below, emit a named, enable-guarded assertion:
  (declare-fun en_<ID_SANITIZED> () Bool)
  (assert (! (=> en_<ID_SANITIZED> <FORMULA_FOR_THIS_REQUIREMENT>) :named req_<ID_SANITIZED>))

Where <ID_SANITIZED> replaces hyphens with underscores (e.g., EIRENE_FUN7-R001 → EIRENE_FUN7_R001).
Each requirement MUST get its own named assertion — do NOT merge multiple requirements into one.

SAME-STATE RULE: If two or more requirements share the same operational context or precondition
(e.g., "during a broadcast call"), their formulas MUST reference the SAME state-step variables.
Do NOT separate co-contextual requirements into different time steps to avoid conflicts.
If two requirements genuinely contradict each other under the same context, the conjunction must be UNSAT — do not hide this.

After all per-requirement assertions, assert all enablers:
  (assert en_<ID1>)
  (assert en_<ID2>)
  ...

Requirement IDs to encode: {ids_list}
"""

    template = f"""
Create an SMT-LIB fragment that captures the following requirements context.
Include declarations for key state variables, any helper functions, and assertions for safety/progress.
Reference identifiers consistently, add brief `;` comments to aid traceability, and terminate with `(check-sat)`.
Use a small bounded horizon with explicit state variables (e.g., `b0`, `b1`, `b2`, `c0`, `c1`, `c2`).
Avoid quantifiers, higher-order functions, derivatives, or recursion; stay within quantifier-free linear integer arithmetic.
Ensure each scenario ends with exactly one `(check-sat)`.
{named_assertion_block}
Informal specification:
{extended_statement}

Informal proof sketch (use as guidance for invariants):
{informal_proof}
"""

    if solver_feedback:
        template += (
            "\nPrevious attempt triggered the following Z3 feedback:\n"
            f"{solver_feedback}\n"
            "Adjust the SMT-LIB so it resolves these issues. Ensure syntax is valid."
        )

    resolved_provider = _normalise_provider(provider)
    if resolved_provider == "codex":
        return await _codex_chat_text(system_prompt, template, model)

    llm_client = client or _new_openai_client()
    response = await llm_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": template},
        ],
        max_tokens=1024,
        temperature=0.01,
    )
    return response.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# Solver seam: pluggable SMT-LIB runners (z3 / cvc5 / portfolio cross-check).
#
# Every caller goes through run_z3_fragment(); its return contract is unchanged
# (status/exit_code/diagnostics on error, status/result/stderr on success). The
# active runner is selected by MBSE_SOLVER={z3|cvc5|portfolio}; the default (z3)
# reproduces the previous behaviour. Extra keys ("solver", "cross_check") are
# additive and ignored by existing callers.
# ---------------------------------------------------------------------------


def ensure_set_logic(fragment: str, logic: str = "QF_LIA") -> str:
    """Insert a ``(set-logic ...)`` line if absent, after any leading options.

    Idempotent. Without a logic line a fragment (a) silently skips
    ``sanitize_qf_lia_fragment`` (which is gated on the literal string), (b)
    persists as a non-portable artefact, and (c) makes cvc5 fall back to the
    full logic. ``logic`` is the hook for future theory expansion (e.g.
    ``QF_LRA``); ``QF_LIA`` matches the current generator prompt.
    """
    if re.search(r"\(set-logic\b", fragment, re.IGNORECASE):
        return fragment
    lines = fragment.splitlines()
    insert_at = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        if stripped.startswith("(set-option"):
            insert_at = index + 1  # keep set-logic after option declarations
            continue
        break  # first real declaration reached
    lines.insert(insert_at, f"(set-logic {logic})")
    return "\n".join(lines)


def _exec_solver(argv: List[str], fragment: str, *, name: str) -> Dict[str, Any]:
    """Run an SMT-LIB fragment through a solver binary over stdin.

    Shared subprocess + diagnostics handling so every SolverRunner reports
    errors identically. Preserves the original run_z3_fragment result contract.
    """
    try:
        proc = subprocess.run(  # noqa: S603, S607
            argv,
            input=fragment.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SMT_SOLVER_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        return {
            "status": "error",
            "exit_code": None,
            "diagnostics": f"{name} executable not found: {argv[0]} ({exc})",
            "solver": name,
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "exit_code": None,
            "diagnostics": f"{name} timed out after {SMT_SOLVER_TIMEOUT} seconds.",
            "solver": name,
        }

    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    lower_out = stdout.lower()
    lower_err = stderr.lower()
    if proc.returncode != 0 or "error" in lower_out or "error" in lower_err:
        diagnostics_parts = [part for part in (stdout, stderr) if part]
        diagnostics = "\n".join(diagnostics_parts) or f"{name} exited with code {proc.returncode}."
        return {
            "status": "error",
            "exit_code": proc.returncode,
            "diagnostics": diagnostics.strip(),
            "solver": name,
        }

    return {
        "status": "ok",
        "exit_code": proc.returncode,
        "result": stdout,
        "stderr": stderr or None,
        "solver": name,
    }


class SolverRunner(ABC):
    """Strategy interface for executing an SMT-LIB fragment."""

    name: str

    @abstractmethod
    def run(self, fragment: str) -> Dict[str, Any]:
        raise NotImplementedError


class Z3Runner(SolverRunner):
    """Run fragments through the Z3 binary (default, unchanged behaviour)."""

    name = "z3"

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or os.getenv("Z3_PATH", "z3")

    def run(self, fragment: str) -> Dict[str, Any]:
        return _exec_solver([self.path, "-in"], fragment, name=self.name)


class Cvc5Runner(SolverRunner):
    """Run fragments through the cvc5 binary."""

    name = "cvc5"

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or os.getenv("CVC5_PATH", "cvc5")

    def run(self, fragment: str) -> Dict[str, Any]:
        # --incremental is required for the check-sat-assuming probes used in
        # pairwise conflict detection; --produce-unsat-cores mirrors the header.
        argv = [self.path, "--lang=smt2", "--incremental", "--produce-unsat-cores"]
        return _exec_solver(argv, fragment, name=self.name)


def _solver_verdict(result: Dict[str, Any]) -> Optional[str]:
    """Extract the sat/unsat/unknown verdict from a runner result, if any."""
    if result.get("status") != "ok":
        return None
    for line in (result.get("result") or "").lower().splitlines():
        token = line.strip()
        if token in ("sat", "unsat", "unknown"):
            return token
    return None


class PortfolioRunner(SolverRunner):
    """Run two solvers and cross-check their verdicts.

    Returns the primary runner's result (preserving the caller contract) with an
    additive ``cross_check`` entry. A sat/unsat disagreement is a faithfulness
    alert: two independent solvers read the same fragment differently.
    """

    name = "portfolio"

    def __init__(self, primary: SolverRunner, secondary: SolverRunner) -> None:
        self.primary = primary
        self.secondary = secondary

    def run(self, fragment: str) -> Dict[str, Any]:
        primary_result = self.primary.run(fragment)
        secondary_result = self.secondary.run(fragment)
        primary_verdict = _solver_verdict(primary_result)
        secondary_verdict = _solver_verdict(secondary_result)

        cross_check: Dict[str, Any] = {
            self.primary.name: primary_verdict,
            self.secondary.name: secondary_verdict,
            "agree": primary_verdict is not None and primary_verdict == secondary_verdict,
        }
        if (
            primary_verdict is not None
            and secondary_verdict is not None
            and primary_verdict != secondary_verdict
        ):
            cross_check["alert"] = (
                f"Solver disagreement: {self.primary.name}={primary_verdict} vs "
                f"{self.secondary.name}={secondary_verdict} — fragment interpreted "
                f"differently by two independent solvers."
            )
        elif secondary_verdict is None:
            cross_check["note"] = (
                f"Secondary solver '{self.secondary.name}' produced no verdict "
                f"(status={secondary_result.get('status')}); cross-check skipped."
            )

        result = dict(primary_result)
        result["cross_check"] = cross_check
        return result


def build_solver_runner() -> SolverRunner:
    """Select the active runner from MBSE_SOLVER ({z3|cvc5|portfolio})."""
    choice = os.getenv("MBSE_SOLVER", "z3").strip().lower()
    if choice == "cvc5":
        return Cvc5Runner()
    if choice == "portfolio":
        return PortfolioRunner(Z3Runner(), Cvc5Runner())
    return Z3Runner()


_SOLVER_RUNNER: SolverRunner = build_solver_runner()


def run_z3_fragment(fragment: str) -> Dict[str, Any]:
    """Execute the SMT-LIB fragment with the active solver and capture diagnostics.

    Name retained for backward compatibility. The active runner is selected by
    MBSE_SOLVER; ensure_set_logic is applied (idempotent) so every probe carries
    a logic declaration regardless of which call site assembled the fragment.
    """
    return _SOLVER_RUNNER.run(ensure_set_logic(fragment))


def _is_numeric_token(token: str) -> bool:
    return bool(re.match(r"^[+-]?(?:\d+|\d+\.\d+)$", token))


def _is_qf_lia_fragment(fragment: str) -> bool:
    return "(set-logic QF_LIA)" in fragment or "(set-logic qf_lia)" in fragment.lower()


def _line_has_bilinear_product(line: str) -> bool:
    matches = re.finditer(r"\(\*\s+([^\s()]+)\s+([^\s()]+)\s*\)", line)
    for match in matches:
        lhs = match.group(1)
        rhs = match.group(2)
        if not _is_numeric_token(lhs) and not _is_numeric_token(rhs):
            return True
    return False


def sanitize_qf_lia_fragment(fragment: str) -> tuple[str, List[str]]:
    """Preserve generated assertions; report unsupported nonlinear content.

    This legacy compatibility hook must never repair a theory mismatch by
    deleting an obligation. Z3/the encoding diagnostics decide validity.
    """
    if not _is_qf_lia_fragment(fragment):
        return fragment, []
    notes = []
    for line in fragment.splitlines():
        stripped = line.strip()
        if stripped.startswith("(assert") and "(*" in stripped and _line_has_bilinear_product(stripped):
            notes.append("Non-linear assertion preserved under QF_LIA; requires a supported encoding: " + stripped)
    return fragment, notes


# ---------------------------------------------------------------------------
# Semantic verification checks (AGREE-Dog-style neuro-symbolic gate)
# ---------------------------------------------------------------------------


def _sanitize_req_id(req_id: str) -> str:
    """Convert a requirement ID to a valid SMT identifier (replace hyphens with underscores)."""
    return req_id.replace("-", "_")


def _parse_sexpr(text: str) -> Any:
    """Minimal S-expression parser for SMT-LIB fragments.

    Returns nested lists for parenthesised forms and strings for atoms.
    """
    tokens: List[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in ("(", ")"):
            tokens.append(ch)
            i += 1
        elif ch == ";":
            while i < len(text) and text[i] != "\n":
                i += 1
        elif ch in (" ", "\t", "\n", "\r"):
            i += 1
        elif ch == '"':
            j = i + 1
            while j < len(text) and text[j] != '"':
                if text[j] == "\\":
                    j += 1
                j += 1
            tokens.append(text[i : j + 1])
            i = j + 1
        else:
            j = i
            while j < len(text) and text[j] not in ("(", ")", " ", "\t", "\n", "\r", ";"):
                j += 1
            tokens.append(text[i:j])
            i = j

    stack: List[List[Any]] = [[]]
    for tok in tokens:
        if tok == "(":
            new: List[Any] = []
            stack[-1].append(new)
            stack.append(new)
        elif tok == ")":
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].append(tok)
    return stack[0]


def _extract_smt_symbols(fragment: str) -> set[str]:
    """Extract declared symbol names from declare-fun / declare-const lines."""
    symbols: set[str] = set()
    for match in re.finditer(
        r"\(\s*declare-(?:fun|const)\s+(\S+)", fragment
    ):
        symbols.add(match.group(1))
    return symbols


def _extract_named_assertions(fragment: str) -> Dict[str, str]:
    """Extract formulas from complete top-level ``:named req_*`` assertions.

    Parsing complete commands prevents a named background assertion from being
    consumed together with a later requirement. Quoted requirement names are
    accepted consistently with the semantic-context parser; comments and other
    named assertions do not become requirement formulas.
    """
    named: Dict[str, str] = {}
    for _, command in _semantic_probe_commands(fragment):
        if command[0] != "assert" or len(command) != 2:
            continue
        expression = command[1]
        if not isinstance(expression, list) or len(expression) < 2 or expression[0] != "!":
            continue
        attributes = expression[2:]
        name = next((attributes[index + 1] for index, attribute in enumerate(attributes[:-1])
                     if attribute == ":named" and isinstance(attributes[index + 1], str)), None)
        if name is None:
            continue
        name = name.strip("|")
        if name.startswith("req_"):
            named[name[4:]] = _semantic_probe_render(expression[1])
    return named


def _check_named_assertion_coverage(
    requirement_ids: List[str],
    named_assertions: Dict[str, str],
) -> Dict[str, Any]:
    """Ensure each requirement has a corresponding named assertion."""
    expected = {_sanitize_req_id(req_id) for req_id in requirement_ids if str(req_id).strip()}
    actual = set(named_assertions.keys())
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    return {
        "passed": len(missing) == 0,
        "expected_count": len(expected),
        "actual_count": len(actual),
        "missing": missing,
        "unexpected": unexpected,
    }


def _extract_state_suffixes(formula: str) -> set[str]:
    """Return the set of state-step suffixes (e.g., '0', '1', '2') referenced in a formula."""
    suffixes: set[str] = set()
    for match in re.finditer(r"_(\d+)\b", formula):
        suffixes.add(match.group(1))
    return suffixes


def _strip_check_sat(fragment: str) -> str:
    """Remove trailing (check-sat) and (get-model) from an SMT fragment."""
    lines = fragment.strip().splitlines()
    while lines and lines[-1].strip().lower() in {"(check-sat)", "(get-model)"}:
        lines.pop()
    return "\n".join(lines)


def _check_same_state(
    named_assertions: Dict[str, str],
    tlf_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Verify that requirements sharing the same operational context use the same state step.

    Group requirements by temporal_kind from the TLR. Requirements in the same group
    that reference state-indexed variables should use the same set of state suffixes.
    """
    req_temporal: Dict[str, str] = {}
    req_text: Dict[str, str] = {}
    for req in tlf_payload.get("requirements", []):
        if not isinstance(req, dict):
            continue
        sid = _sanitize_req_id(req.get("id", ""))
        req_temporal[sid] = req.get("temporal_kind", "unknown")
        req_text[sid] = req.get("text", "")

    # Build context groups — requirements with the same temporal_kind
    # AND overlapping text context (e.g., both mention "broadcast call").
    context_groups: Dict[str, List[str]] = {}
    for sid, formula in named_assertions.items():
        kind = req_temporal.get(sid, "unknown")
        context_groups.setdefault(kind, []).append(sid)

    # Also group by shared noun-phrase context in the requirement text.
    # Extract leading participial/conditional phrases.
    phrase_groups: Dict[str, List[str]] = {}
    for sid in named_assertions:
        text = req_text.get(sid, "").lower()
        # Match "during X", "if X", "when X" — shared preconditions.
        for match in re.finditer(r"(during|if|when|while)\s+(.+?)(?:,|\.|;|shall)", text):
            phrase = match.group(0).strip().rstrip(",. ;")
            phrase_groups.setdefault(phrase, []).append(sid)

    issues: List[Dict[str, Any]] = []

    for phrase, sids in phrase_groups.items():
        if len(sids) < 2:
            continue
        suffix_map: Dict[str, set[str]] = {}
        for sid in sids:
            if sid in named_assertions:
                suffix_map[sid] = _extract_state_suffixes(named_assertions[sid])
        unique_suffix_sets = [s for s in suffix_map.values() if s]
        if len(unique_suffix_sets) >= 2:
            first = unique_suffix_sets[0]
            for other in unique_suffix_sets[1:]:
                if first != other and first and other:
                    issues.append({
                        "context": phrase,
                        "requirement_ids": sids,
                        "suffix_sets": {s: sorted(suffix_map.get(s, set())) for s in sids},
                        "message": (
                            f"Requirements {', '.join(sids)} share context '{phrase}' "
                            f"but reference different state steps."
                        ),
                    })
                    break

    return {"passed": len(issues) == 0, "issues": issues}


def _semantic_probe_commands(fragment: str) -> List[Tuple[str, List[Any]]]:
    """Read complete SMT-LIB forms without losing comments, strings, or bodies.

    Unlike line filtering, this preserves multiline declarations and definitions.
    It rejects incomplete forms instead of silently constructing weaker context.
    """
    commands: List[Tuple[str, List[Any]]] = []
    stack: List[List[Any]] = []
    start, index = 0, 0
    while index < len(fragment):
        char = fragment[index]
        if char.isspace():
            index += 1
            continue
        if char == ";":
            end = fragment.find("\n", index)
            index = len(fragment) if end < 0 else end + 1
            continue
        if char == "(":
            form: List[Any] = []
            if stack:
                stack[-1].append(form)
            else:
                start = index
            stack.append(form)
            index += 1
            continue
        if char == ")":
            if not stack:
                raise ValueError("Unmatched closing parenthesis in semantic probe context.")
            form = stack.pop()
            index += 1
            if not stack:
                if not form or not isinstance(form[0], str):
                    raise ValueError("An SMT command requires an atomic command name.")
                commands.append((fragment[start:index], form))
            continue
        if not stack:
            raise ValueError("Unexpected top-level token in semantic probe context.")
        token_start = index
        if char in {'"', "|"}:
            delimiter = char
            index += 1
            while index < len(fragment):
                if fragment[index] == delimiter:
                    # SMT-LIB string literals escape a quotation mark by doubling it.
                    if delimiter == '"' and index + 1 < len(fragment) and fragment[index + 1] == '"':
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise ValueError("Unterminated string or quoted symbol in semantic probe context.")
        else:
            while index < len(fragment) and not fragment[index].isspace() and fragment[index] not in "();":
                index += 1
        stack[-1].append(fragment[token_start:index])
    if stack:
        raise ValueError("Unclosed SMT-LIB form in semantic probe context.")
    return commands


def _semantic_probe_render(form: Any) -> str:
    if isinstance(form, list):
        return "(" + " ".join(_semantic_probe_render(item) for item in form) + ")"
    return str(form)


def _semantic_probe_context(
    fragment: str,
    enablers: set[str],
    *,
    drop_requirements: bool,
) -> Tuple[str, Dict[str, Any]]:
    """Retain background logic while removing only probe/selection commands.

    Direct positive enabler assertions are administrative requirement selection.
    Other assertions, including mixed expressions and named background premises,
    retain their original meaning. Stateful contexts are rejected, not flattened.
    """
    context_commands = {
        "set-logic", "set-option", "set-info", "declare-sort", "define-sort",
        "declare-fun", "declare-const", "define-fun", "define-fun-rec",
        "define-funs-rec", "declare-datatype", "declare-datatypes", "assert",
    }
    observational_commands = {
        "check-sat", "check-sat-assuming", "get-model", "get-value",
        "get-unsat-core", "get-proof", "get-info", "get-option",
        "get-assertions", "get-assignment", "get-unsat-assumptions", "echo", "exit",
    }
    retained: List[str] = []
    omitted_enablers: List[str] = []
    omitted_requirements: List[str] = []
    for raw, form in _semantic_probe_commands(fragment):
        head = form[0]
        if head in observational_commands:
            continue
        if head not in context_commands:
            raise ValueError(f"Unsupported stateful or unknown semantic probe command: {head!r}.")
        if head == "assert":
            if len(form) != 2:
                raise ValueError("Malformed assertion in semantic probe context.")
            expression = form[1]
            if isinstance(expression, str) and expression in enablers:
                omitted_enablers.append(expression)
                continue
            if drop_requirements and isinstance(expression, list) and expression and expression[0] == "!":
                attributes = expression[2:]
                requirement_name = next((attributes[index + 1] for index, item in enumerate(attributes[:-1])
                                         if item == ":named" and isinstance(attributes[index + 1], str)
                                         and attributes[index + 1].strip("|").startswith("req_")), None)
                if requirement_name is not None:
                    omitted_requirements.append(requirement_name)
                    continue
        retained.append(raw)
    return "\n".join(retained), {
        "removed_direct_enable_assertions": omitted_enablers,
        "removed_requirement_assertions": omitted_requirements,
        "background_scope": "Complete declarations/definitions and substantive background assertions; direct positive requirement-selection assertions and observational commands are excluded.",
    }


def _semantic_probe_verdict(result: Dict[str, Any]) -> Optional[str]:
    if result.get("cross_check") and not result["cross_check"].get("agree"):
        return None
    return _solver_verdict(result)


def _check_pairwise_conflicts(
    fragment: str,
    requirement_ids: List[str],
) -> Dict[str, Any]:
    """Select each requirement pair in the preserved background context.

    Other requirement enablers are disabled; direct positive activation commands
    from the original all-requirements query are removed, not substantive premises.
    """
    n = len(requirement_ids)
    total_pairs = n * (n - 1) // 2
    if total_pairs > MBSE_MAX_PAIRWISE_CHECKS:
        return {
            "passed": True,
            "skipped": True,
            "reason": f"Too many pairs ({total_pairs} > {MBSE_MAX_PAIRWISE_CHECKS}); skipped.",
            "conflicts": [],
        }

    enablers = {f"en_{_sanitize_req_id(rid)}" for rid in requirement_ids}
    try:
        base, context_scope = _semantic_probe_context(fragment, enablers, drop_requirements=False)
    except ValueError as exc:
        return {"passed": False, "conflicts": [], "solver_errors": [{
            "status": "context_error", "diagnostics": str(exc),
            "message": "Pairwise logical context could not be reconstructed without changing its meaning.",
        }]}
    conflicts: List[Dict[str, Any]] = []
    solver_errors: List[Dict[str, Any]] = []

    for i in range(n):
        for j in range(i + 1, n):
            id_a = _sanitize_req_id(requirement_ids[i])
            id_b = _sanitize_req_id(requirement_ids[j])
            selection = [f"en_{id_a}", f"en_{id_b}"]
            selection.extend(f"(not {enabler})" for enabler in sorted(enablers - set(selection)))
            probe = f"{base}\n\n(check-sat-assuming ({' '.join(selection)}))\n"
            result = run_z3_fragment(probe)
            status = result.get("status")
            result_token = _semantic_probe_verdict(result)

            if status != "ok":
                solver_errors.append(
                    {
                        "id_a": requirement_ids[i],
                        "id_b": requirement_ids[j],
                        "status": status,
                        "diagnostics": result.get("diagnostics"),
                        "message": (
                            f"Pairwise probe for {requirement_ids[i]} and {requirement_ids[j]} "
                            "did not complete successfully."
                        ),
                    }
                )
                continue

            if result_token == "unsat":
                conflicts.append({
                    "id_a": requirement_ids[i],
                    "id_b": requirement_ids[j],
                    "result": "unsat",
                    "message": (
                        f"Requirements {requirement_ids[i]} and {requirement_ids[j]} "
                        f"are unsatisfiable under the retained background with other requirement enablers disabled."
                    ),
                })
                continue

            if result_token not in {"sat"}:
                solver_errors.append(
                    {
                        "id_a": requirement_ids[i],
                        "id_b": requirement_ids[j],
                        "status": "inconclusive",
                        "diagnostics": result.get("diagnostics"),
                        "result": result.get("result"),
                        "message": (
                            f"Pairwise probe for {requirement_ids[i]} and {requirement_ids[j]} "
                            f"returned inconclusive result: {result_token or 'empty'}."
                        ),
                    }
                )

    return {
        "passed": len(conflicts) == 0 and len(solver_errors) == 0,
        "conflicts": conflicts,
        "solver_errors": solver_errors,
        "scope": context_scope,
    }


def _check_vacuity(
    fragment: str,
    named_assertions: Dict[str, str],
) -> Dict[str, Any]:
    """Check for vacuously true implications.

    For top-level named implications, check antecedent satisfiability relative
    to substantive background assumptions with named requirements excluded.
    Administrative enable guards are explicitly skipped. Helper bodies and nested
    operational triggers are not traversed, so this is not a general vacuity proof.
    """
    enablers = {f"en_{req_key}" for req_key in named_assertions}
    try:
        declarations, context_scope = _semantic_probe_context(fragment, enablers, drop_requirements=True)
    except ValueError as exc:
        return {"passed": False, "vacuous_requirements": [], "tautological_antecedents": [],
                "solver_errors": [{"status": "context_error", "diagnostics": str(exc),
                                   "message": "Vacuity logical context could not be reconstructed without changing its meaning."}],
                "skipped_administrative_guards": []}
    skipped_administrative_guards: List[Dict[str, Any]] = []
    checked_implications = 0
    vacuous: List[Dict[str, Any]] = []
    tautological_antecedents: List[Dict[str, Any]] = []
    solver_errors: List[Dict[str, Any]] = []

    for req_key, formula in named_assertions.items():
        if not formula.lstrip().startswith("("):
            continue
        try:
            parsed = _semantic_probe_commands(formula)
        except ValueError as exc:
            solver_errors.append({"requirement_id": req_key, "status": "context_error", "diagnostics": str(exc)})
            continue
        if len(parsed) != 1 or parsed[0][1][0] != "=>":
            continue
        implication = parsed[0][1]
        if len(implication) != 3:
            solver_errors.append({"requirement_id": req_key, "status": "context_error", "diagnostics": "Malformed implication."})
            continue
        antecedent = _semantic_probe_render(implication[1])
        if antecedent == f"en_{req_key}":
            skipped_administrative_guards.append({
                "requirement_id": req_key, "antecedent": antecedent,
                "reason": "Administrative requirement-selection flag, not a stakeholder trigger. Underlying conditional triggers and helper bodies were not analyzed; this is not evidence of their non-vacuity.",
            })
            continue
        checked_implications += 1
        # Build a probe: declarations + (assert antecedent) + (check-sat)
        probe = f"{declarations}\n\n(assert {antecedent})\n(check-sat)\n"
        result = run_z3_fragment(probe)

        if result.get("status") != "ok":
            solver_errors.append(
                {
                    "requirement_id": req_key,
                    "antecedent": antecedent,
                    "status": result.get("status"),
                    "diagnostics": result.get("diagnostics"),
                    "message": (
                        f"Vacuity probe failed for requirement {req_key}; "
                        "antecedent satisfiability could not be established."
                    ),
                }
            )
            continue

        status = _semantic_probe_verdict(result)
        if status == "unsat":
            vacuous.append({
                "requirement_id": req_key,
                "antecedent": antecedent,
                "message": (
                    f"Requirement {req_key}: implication antecedent is unsatisfiable — "
                    f"the requirement is vacuously true."
                ),
            })
            continue

        if status == "sat":
            # Probe whether NOT antecedent is satisfiable. If UNSAT, the antecedent is a tautology.
            neg_probe = f"{declarations}\n\n(assert (not {antecedent}))\n(check-sat)\n"
            neg_result = run_z3_fragment(neg_probe)
            if neg_result.get("status") != "ok":
                solver_errors.append(
                    {
                        "requirement_id": req_key,
                        "antecedent": antecedent,
                        "status": neg_result.get("status"),
                        "diagnostics": neg_result.get("diagnostics"),
                        "message": (
                            f"Tautology probe failed for requirement {req_key}; "
                            "antecedent polarity check is inconclusive."
                        ),
                    }
                )
                continue

            neg_status = _semantic_probe_verdict(neg_result)
            if neg_status == "unsat":
                tautological_antecedents.append(
                    {
                        "requirement_id": req_key,
                        "antecedent": antecedent,
                        "message": (
                            f"Requirement {req_key}: implication antecedent is forced by the retained background "
                            "assumptions, so the trigger is not discriminative within this context."
                        ),
                    }
                )
                continue
            if neg_status not in {"sat"}:
                solver_errors.append(
                    {
                        "requirement_id": req_key,
                        "antecedent": antecedent,
                        "status": "inconclusive",
                        "result": neg_result.get("result"),
                        "message": (
                            f"Tautology probe for requirement {req_key} returned "
                            f"inconclusive result: {neg_status or 'empty'}."
                        ),
                    }
                )
            continue

        solver_errors.append(
            {
                "requirement_id": req_key,
                "antecedent": antecedent,
                "status": "inconclusive",
                "result": result.get("result"),
                "message": (
                    f"Antecedent satisfiability probe for requirement {req_key} returned "
                    f"inconclusive result: {status or 'empty'}."
                ),
            }
        )

    return {
        "passed": (
            len(vacuous) == 0
            and len(tautological_antecedents) == 0
            and len(solver_errors) == 0
        ),
        "vacuous_requirements": vacuous,
        "tautological_antecedents": tautological_antecedents,
        "solver_errors": solver_errors,
        "skipped_administrative_guards": skipped_administrative_guards,
        "checked_implication_count": checked_implications,
        "scope": {**context_scope,
                  "checked": "Top-level named implication antecedents against background assumptions; named requirement assertions are excluded.",
                  "underlying_conditional_triggers_checked": False,
                  "limitation": "Helper definitions are preserved as logical context but their bodies and nested stakeholder triggers are not traversed. A skipped administrative guard provides no non-vacuity evidence."},
    }


def _check_symbol_drift(
    fragment: str,
    tlf_payload: Dict[str, Any],
    requirement_ids: List[str],
) -> Dict[str, Any]:
    """Flag SMT symbols not traceable to the TLR symbol table.

    Enabler variables (en_*), state-indexed variants (name_0, name_1, ...),
    and standard SMT built-ins are excluded from drift checks.
    """
    smt_symbols = _extract_smt_symbols(fragment)
    tlf_symbols: set[str] = set()
    for sym in tlf_payload.get("symbol_table", []):
        if isinstance(sym, dict) and sym.get("name"):
            tlf_symbols.add(sym["name"])

    # Build a set of expected enabler names.
    enablers = {f"en_{_sanitize_req_id(rid)}" for rid in requirement_ids}

    # Build base names from TLR symbols (stripping _holds suffix for matching variants).
    base_names: set[str] = set()
    for s in tlf_symbols:
        base_names.add(s)
        if s.endswith("_holds"):
            base_names.add(s[:-6])

    unknown: List[str] = []
    for sym in sorted(smt_symbols):
        if sym in enablers:
            continue
        if sym in tlf_symbols:
            continue
        # Check state-indexed variant: sym might be "voice_service_0" → base "voice_service".
        base = re.sub(r"_\d+$", "", sym)
        if base in tlf_symbols or base in base_names:
            continue
        # Skip if it looks like a helper/infrastructure variable.
        unknown.append(sym)

    return {"passed": len(unknown) == 0, "unknown_symbols": unknown}


def run_semantic_checks(
    sat_fragment: str,
    requirement_ids: List[str],
    tlf_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Run all semantic verification checks on a SAT fragment.

    Returns a JSON-serialisable result dict with per-check details and an
    aggregate ``passed`` flag.
    """
    named_assertions = _extract_named_assertions(sat_fragment)
    named_coverage = _check_named_assertion_coverage(requirement_ids, named_assertions)

    same_state = _check_same_state(named_assertions, tlf_payload)
    pairwise = _check_pairwise_conflicts(sat_fragment, requirement_ids)
    vacuity = _check_vacuity(sat_fragment, named_assertions)
    symbol_drift = _check_symbol_drift(sat_fragment, tlf_payload, requirement_ids)

    passed = all([
        named_coverage["passed"],
        same_state["passed"],
        pairwise["passed"],
        vacuity["passed"],
        # Symbol drift is informational, not blocking.
    ])

    return {
        "passed": passed,
        "checks": {
            "named_assertion_coverage": named_coverage,
            "same_state": same_state,
            "pairwise_conflict": pairwise,
            "vacuity": vacuity,
            "symbol_drift": symbol_drift,
        },
    }


def _merge_repaired_clauses(original: str, patch: str, target_ids: List[str]) -> str:
    """Replace named assertions in *original* with repaired versions from *patch*.

    For each target ID, find the ``(assert (! ... :named req_<ID>))`` block in *patch*
    and substitute it into *original*. Non-targeted assertions are kept unchanged.
    """
    result = original
    for rid in target_ids:
        sid = _sanitize_req_id(rid)
        pattern = re.compile(
            r"\(assert\s+\(!\s+.*?\s+:named\s+req_" + re.escape(sid) + r"\s*\)\s*\)",
            flags=re.DOTALL,
        )
        patch_match = pattern.search(patch)
        if patch_match:
            orig_match = pattern.search(result)
            if orig_match:
                result = result[:orig_match.start()] + patch_match.group(0) + result[orig_match.end():]
    return result


def _collect_failed_ids(semantic_results: Dict[str, Any]) -> List[str]:
    """Extract the set of requirement IDs implicated by failed semantic checks."""
    failed: set[str] = set()
    checks = semantic_results.get("checks", {})
    for rid in checks.get("named_assertion_coverage", {}).get("missing", []):
        failed.add(rid)
    for issue in checks.get("same_state", {}).get("issues", []):
        for rid in issue.get("requirement_ids", []):
            failed.add(rid)
    for conflict in checks.get("pairwise_conflict", {}).get("conflicts", []):
        failed.add(conflict.get("id_a", ""))
        failed.add(conflict.get("id_b", ""))
    for probe in checks.get("pairwise_conflict", {}).get("solver_errors", []):
        failed.add(probe.get("id_a", ""))
        failed.add(probe.get("id_b", ""))
    for vac in checks.get("vacuity", {}).get("vacuous_requirements", []):
        failed.add(vac.get("requirement_id", ""))
    for vac in checks.get("vacuity", {}).get("tautological_antecedents", []):
        failed.add(vac.get("requirement_id", ""))
    for vac in checks.get("vacuity", {}).get("solver_errors", []):
        failed.add(vac.get("requirement_id", ""))
    failed.discard("")
    return sorted(failed)


def classify_repair_diff(
    original_fragment: str,
    repaired_fragment: str,
    requirement_ids: List[str],
) -> List[Dict[str, str]]:
    """Compare original and repaired SMT fragments, classifying each change.

    Returns a list of per-requirement diffs with classification:
      - ``strengthened`` — repair added constraints (fewer models). Safe.
      - ``weakened`` — repair relaxed constraints (more models). Dangerous.
      - ``temporal_shifted`` — repair moved requirement to a different state step.
      - ``unchanged`` — formula identical.
    """
    orig_named = _extract_named_assertions(original_fragment)
    new_named = _extract_named_assertions(repaired_fragment)
    diffs: List[Dict[str, str]] = []
    for rid in requirement_ids:
        sid = _sanitize_req_id(rid)
        orig_formula = orig_named.get(sid, "")
        new_formula = new_named.get(sid, "")
        if orig_formula == new_formula:
            diffs.append({"requirement_id": rid, "classification": "unchanged"})
            continue
        if not orig_formula and new_formula:
            diffs.append({
                "requirement_id": rid,
                "classification": "strengthened",
                "reason": "Named assertion added where none existed.",
            })
            continue
        if orig_formula and not new_formula:
            diffs.append({
                "requirement_id": rid,
                "classification": "weakened",
                "reason": "Named assertion removed entirely.",
            })
            continue
        # Check for temporal shift: state suffixes changed.
        orig_suffixes = _extract_state_suffixes(orig_formula)
        new_suffixes = _extract_state_suffixes(new_formula)
        if orig_suffixes != new_suffixes and orig_suffixes and new_suffixes:
            diffs.append({
                "requirement_id": rid,
                "classification": "temporal_shifted",
                "reason": f"State steps changed: {sorted(orig_suffixes)} → {sorted(new_suffixes)}",
                "original": orig_formula,
                "repaired": new_formula,
            })
            continue
        # Heuristic: if the new formula is strictly shorter, likely weakened.
        # If longer or has more conjuncts, likely strengthened.
        orig_conjuncts = orig_formula.count("(and") + orig_formula.count("(=>")
        new_conjuncts = new_formula.count("(and") + new_formula.count("(=>")
        if new_conjuncts < orig_conjuncts or len(new_formula) < len(orig_formula) * 0.8:
            classification = "weakened"
            reason = "Formula has fewer constraints or is significantly shorter."
        elif new_conjuncts > orig_conjuncts or len(new_formula) > len(orig_formula) * 1.2:
            classification = "strengthened"
            reason = "Formula has more constraints or is significantly longer."
        else:
            classification = "modified"
            reason = "Formula changed but direction unclear."
        diffs.append({
            "requirement_id": rid,
            "classification": classification,
            "reason": reason,
            "original": orig_formula,
            "repaired": new_formula,
        })
    return diffs


async def repair_failed_requirements(
    sat_fragment: str,
    semantic_results: Dict[str, Any],
    extended_statement: str,
    informal_proof: str,
    requirement_ids: List[str],
    tlf_payload: Dict[str, Any],
    model: str,
    provider: str = "openai",
    client: Optional[AsyncOpenAI] = None,
) -> tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
    """Targeted repair loop: regenerate only the named assertions that failed semantic checks.

    Returns ``(repaired_fragment, final_semantic_results, repair_iterations)``.
    """
    max_repairs = max(0, SMT_MAX_SEMANTIC_REPAIRS)
    current_fragment = sat_fragment
    current_results = semantic_results
    repair_iterations: List[Dict[str, Any]] = []

    for attempt in range(max_repairs):
        failed_ids = _collect_failed_ids(current_results)
        if not failed_ids:
            break

        # Build a targeted repair prompt.
        checks = current_results.get("checks", {})
        failure_summary_parts: List[str] = []
        for issue in checks.get("same_state", {}).get("issues", []):
            failure_summary_parts.append(f"Same-state violation: {issue.get('message', '')}")
        for conflict in checks.get("pairwise_conflict", {}).get("conflicts", []):
            failure_summary_parts.append(f"Pairwise conflict: {conflict.get('message', '')}")
        for probe in checks.get("pairwise_conflict", {}).get("solver_errors", []):
            failure_summary_parts.append(f"Pairwise probe error: {probe.get('message', '')}")
        for vac in checks.get("vacuity", {}).get("vacuous_requirements", []):
            failure_summary_parts.append(f"Vacuity: {vac.get('message', '')}")
        for vac in checks.get("vacuity", {}).get("tautological_antecedents", []):
            failure_summary_parts.append(f"Tautological antecedent: {vac.get('message', '')}")
        for vac in checks.get("vacuity", {}).get("solver_errors", []):
            failure_summary_parts.append(f"Vacuity probe error: {vac.get('message', '')}")
        failure_summary = "\n".join(failure_summary_parts)

        ids_str = ", ".join(failed_ids)
        repair_prompt = f"""
The following SMT-LIB fragment has semantic check failures that must be corrected.

FAILED REQUIREMENT IDS: {ids_str}

FAILURE DETAILS:
{failure_summary}

CURRENT FRAGMENT:
{current_fragment}

INSTRUCTIONS:
- Regenerate ONLY the named assertions for the listed failed requirement IDs.
- Keep the same (set-option :produce-unsat-cores true) header and all declarations.
- Keep all other named assertions EXACTLY as they are.
- For requirements sharing the same operational context, use the SAME state-step variables.
- If two requirements genuinely contradict, let the conjunction be UNSAT — do not hide conflicts.
- For vacuously true implications, strengthen the antecedent so it is satisfiable.
- Emit the COMPLETE repaired fragment (not just the changed parts).
- Finish with (check-sat).
"""
        repaired_raw = await _chat_text_response(
            system_prompt=(
                "You are an SMT engineer repairing formal requirement encodings. "
                "Fix ONLY the identified issues. Do not alter requirements that passed checks."
            ),
            user_prompt=repair_prompt,
            model=model,
            provider=provider,
            client=client,
            max_tokens=4096,
            temperature=0.01,
        )
        repaired_clean = strip_code_fences(repaired_raw)
        repaired_clean, _ = sanitize_qf_lia_fragment(repaired_clean)

        # Validate the repaired fragment with Z3.
        z3_result = run_z3_fragment(repaired_clean)
        if z3_result.get("status") != "ok":
            # Z3 rejected the repair; try merging individual clauses instead.
            repaired_clean = _merge_repaired_clauses(current_fragment, repaired_clean, failed_ids)
            z3_result = run_z3_fragment(repaired_clean)

        # Re-run semantic checks.
        new_results = run_semantic_checks(repaired_clean, requirement_ids, tlf_payload)

        repair_iterations.append({
            "attempt": attempt + 1,
            "targeted_ids": failed_ids,
            "z3_status": z3_result.get("status"),
            "z3_result": z3_result.get("result"),
            "semantic_passed": new_results["passed"],
        })

        if z3_result.get("status") == "ok":
            current_fragment = repaired_clean
            current_results = new_results
        if new_results["passed"]:
            break

    return current_fragment, current_results, repair_iterations


async def generate_validated_smt_fragment(
    extended_statement: str,
    informal_proof: str,
    model: str,
    provider: str = "openai",
    client: Optional[AsyncOpenAI] = None,
    requirement_ids: Optional[List[str]] = None,
) -> tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
    """Iteratively refine the SMT fragment until Z3 accepts it or attempts are exhausted."""
    feedback: Optional[str] = None
    validation: Dict[str, Any] = {}
    fragment = ""
    clean_fragment = ""
    iterations: List[Dict[str, Any]] = []

    attempts = max(1, SMT_MAX_FIX_ATTEMPTS)
    for _ in range(attempts):
        fragment = await generate_smt_skeleton(
            extended_statement=extended_statement,
            informal_proof=informal_proof,
            model=model,
            solver_feedback=feedback,
            provider=provider,
            client=client,
            requirement_ids=requirement_ids,
        )
        clean_fragment = strip_code_fences(fragment)
        clean_fragment = ensure_set_logic(clean_fragment)
        clean_fragment, sanitation_notes = sanitize_qf_lia_fragment(clean_fragment)
        validation = await asyncio.to_thread(run_z3_fragment, clean_fragment)
        iterations.append({
            "raw_fragment": fragment,
            "clean_fragment": clean_fragment,
            "validation": validation,
            "sanitation_notes": sanitation_notes,
        })
        if validation.get("status") == "ok":
            fragment = clean_fragment
            break

        feedback_raw = validation.get("diagnostics")
        if not feedback_raw:
            break
        feedback = feedback_raw if len(feedback_raw) <= 1800 else feedback_raw[:1800] + "\n[truncated]"

    final_fragment = clean_fragment or strip_code_fences(fragment)
    return final_fragment, validation, iterations


async def write_smt_outputs(
    prefix: Path,
    sat_fragment: str,
    sat_validation: Dict[str, Any],
    unsat_extra: Optional[str],
) -> List[Dict[str, Any]]:
    base = prefix
    if base.suffix:
        try:
            base = base.with_suffix("")
        except ValueError:
            base = Path(base.parent, base.stem)
    parent = base.parent
    parent.mkdir(parents=True, exist_ok=True)
    base_name = base.name

    sat_path = parent / f"{base_name}_sat.smt2"
    sat_text = ensure_trailing_newline(sat_fragment.strip())
    sat_path.write_text(sat_text, encoding="utf-8")

    files: List[Dict[str, Any]] = [
        {
            "path": str(sat_path),
            "mode": "sat",
            "validation": sat_validation,
        }
    ]

    if unsat_extra is not None:
        unsat_extra = unsat_extra.strip()
        if not unsat_extra:
            raise ValueError("A negative query requires explicit additional assertions; no artificial contradiction is generated.")
        unsat_text = prepare_unsat_variant(sat_text, unsat_extra)
        unsat_path = parent / f"{base_name}_unsat.smt2"
        unsat_path.write_text(unsat_text, encoding="utf-8")
        unsat_validation = run_z3_fragment(unsat_text)
        files.append(
            {
                "path": str(unsat_path),
                "mode": "unsat",
                "extra_assertions": unsat_extra,
                "validation": unsat_validation,
            }
        )

    return files


def write_tlf_output(prefix: Path, tlf_payload: Dict[str, Any]) -> str:
    base = prefix
    if base.suffix:
        try:
            base = base.with_suffix("")
        except ValueError:
            base = Path(base.parent, base.stem)
    base.parent.mkdir(parents=True, exist_ok=True)
    tlf_path = base.parent / f"{base.name}_tlr.json"
    tlf_path.write_text(
        json.dumps(tlf_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return str(tlf_path)


async def extract_requirements_from_chunk(
    chunk_text: str,
    model: str,
    id_prefix: str,
    start_index: int,
    provider: str = "openai",
    client: Optional[AsyncOpenAI] = None,
) -> Dict[str, Any]:
    """
    Extract functional requirements and assumptions from a chunk of source material.

    Returns a structure with `requirements` and `assumptions` so the caller can merge them.
    """
    system_prompt = (
        "You are a systems engineer preparing material for an MBSE pipeline. "
        "Read the provided excerpt and extract concrete, verifiable functional requirements. "
        "Each requirement must describe observable behaviour, thresholds, or invariants that "
        "could later be mapped to typed logical representations and SMT constraints."
    )

    user_prompt = (
        "Source excerpt:\n"
        f"{chunk_text}\n\n"
        "Instructions:\n"
        f"- Create up to 5 requirements. Use incremental identifiers starting with {id_prefix}{start_index:03d}.\n"
        "- Prefer requirements that constrain behaviour, safety, or performance.\n"
        "- Include supportive assumptions only when necessary to interpret the requirement.\n"
        "- Provide rationale strings to help downstream engineers understand intent.\n"
        "- Return strict JSON with keys: requirements (list), assumptions (list).\n"
        "- Each requirement object must contain: id, text, rationale, category, priority.\n"
        "- Priority should be one of: high, medium, low.\n"
        "- Category should be one of: safety, performance, functional, constraint, other.\n"
    )

    resolved_provider = _normalise_provider(provider)
    if resolved_provider == "codex":
        payload = await _codex_chat_json(system_prompt, user_prompt, model)
    else:
        llm_client = client or _new_openai_client()
        response = await llm_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=1024,
            temperature=0.01,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content
        payload = json.loads(raw)

    requirements: List[Dict[str, Any]] = []
    assumptions: List[str] = []

    raw_requirements: Any = payload.get("requirements", [])
    if isinstance(raw_requirements, dict):
        raw_requirements = list(raw_requirements.values())
    elif not isinstance(raw_requirements, list):
        raw_requirements = [raw_requirements]

    allowed_categories = {"safety", "performance", "functional", "constraint", "other"}
    allowed_priorities = {"high", "medium", "low"}

    for idx, raw_item in enumerate(raw_requirements, start=start_index):
        if isinstance(raw_item, dict):
            item = raw_item
        elif isinstance(raw_item, str):
            item = {"text": raw_item}
        else:
            item = {"text": _coerce_text(raw_item)}

        category = normalise_whitespace(item.get("category", "functional")).lower()
        if category not in allowed_categories:
            category = "other"
        priority = normalise_whitespace(item.get("priority", "medium")).lower()
        if priority not in allowed_priorities:
            priority = "medium"

        req = {
            "id": normalise_whitespace(item.get("id")) or f"{id_prefix}{idx:03d}",
            "text": normalise_whitespace(item.get("text", "")),
            "rationale": normalise_whitespace(item.get("rationale", "")),
            "category": category,
            "priority": priority,
        }
        if req["text"]:
            requirements.append(req)

    raw_assumptions: Any = payload.get("assumptions", [])
    if isinstance(raw_assumptions, dict):
        raw_assumptions = list(raw_assumptions.values())
    elif not isinstance(raw_assumptions, list):
        raw_assumptions = [raw_assumptions]

    for assumption in raw_assumptions:
        cleaned = normalise_whitespace(assumption)
        if cleaned:
            assumptions.append(cleaned)

    return {"requirements": requirements, "assumptions": assumptions}


async def run_translate_flow(
    statement_path: Path,
    model: str,
    smt_prefix: Optional[Path] = None,
    unsat_extra: Optional[str] = None,
    provider: str = "openai",
) -> Dict[str, Any]:
    """Execute the natural language → informal → SMT sketch chain."""
    statement = load_text(statement_path)
    tlf_requirements = _requirements_for_tlf(statement_path, statement)
    typed_requirements_form = _build_tlf_payload(
        statement_path=statement_path,
        statement_text=statement,
        requirements=tlf_requirements,
    )
    logical_form_tlf = convert_requirements_tlr_to_logical_form_tlr(typed_requirements_form)
    resolved_provider = _normalise_provider(provider)
    client = _new_openai_client() if resolved_provider == "openai" else None
    natural = await translate_to_natural_language(
        statement=statement,
        model=model,
        provider=resolved_provider,
        client=client,
    )
    informal_stmt = await generate_informal_statement(
        natural_language_text=natural,
        model=model,
        provider=resolved_provider,
        client=client,
    )
    informal_proof = await generate_informal_proof(
        informal_statement=informal_stmt,
        model=model,
        provider=resolved_provider,
        client=client,
    )
    compact_tlf = _compact_tlf_for_prompt(typed_requirements_form)
    statement_for_prompt = _clip_text(
        statement,
        _env_int("MBSE_STATEMENT_PROMPT_MAX_CHARS", 12000, minimum=1),
    )
    natural_for_prompt = _clip_text(
        natural,
        _env_int("MBSE_NATURAL_PROMPT_MAX_CHARS", 6000, minimum=1),
    )
    proof_for_prompt = _clip_text(
        informal_proof,
        _env_int("MBSE_PROOF_PROMPT_MAX_CHARS", 5000, minimum=1),
    )
    include_full_tlr = _env_true(
        "MBSE_INCLUDE_FULL_TLR_IN_SMT_PROMPT",
        default=_env_true("MBSE_INCLUDE_FULL_TLF_IN_SMT_PROMPT", default=False),
    )
    if include_full_tlr:
        tlf_for_prompt = typed_requirements_form
    else:
        tlf_for_prompt = compact_tlf
    extended = (
        "Requirements source text:\n"
        f"{statement_for_prompt}\n\n"
        "Typed logical representation (JSON):\n"
        f"{json.dumps(tlf_for_prompt, indent=2, ensure_ascii=False)}\n\n"
        "Natural language interpretation:\n"
        f"{natural_for_prompt}"
    )
    req_ids = [
        req["id"]
        for req in typed_requirements_form.get("requirements", [])
        if isinstance(req, dict) and req.get("id")
    ]
    smt_fragment, validation, iterations = await generate_validated_smt_fragment(
        extended_statement=extended,
        informal_proof=proof_for_prompt,
        model=model,
        provider=resolved_provider,
        client=client,
        requirement_ids=req_ids or None,
    )
    smt_files: List[Dict[str, Any]] = []
    tlr_file: Optional[str] = None
    if smt_prefix:
        default_unsat = unsat_extra
        tlr_file = write_tlf_output(smt_prefix, typed_requirements_form)
        smt_files = await write_smt_outputs(
            prefix=smt_prefix,
            sat_fragment=smt_fragment,
            sat_validation=validation,
            unsat_extra=default_unsat,
        )
    return {
        "statement": statement,
        "natural_language": natural,
        "informal_statement": informal_stmt,
        "informal_proof": informal_proof,
        "smt_fragment": smt_fragment,
        "smt_validation": validation,
        "smt_iterations": iterations,
        "smt_files": smt_files,
        "typed_requirements_form": typed_requirements_form,
        # Legacy alias kept for backwards compatibility with existing consumers.
        "typed_logical_form": typed_requirements_form,
        "logical_form_tlr": logical_form_tlf,
        # Legacy alias kept for backwards compatibility with existing consumers.
        "logical_form_tlf": logical_form_tlf,
        "tlr_file": tlr_file,
        # Legacy alias kept for backwards compatibility with existing consumers.
        "tlf_file": tlr_file,
        "mode": resolved_provider,
    }


async def run_harvest_flow(
    source_path: Path,
    model: str,
    set_id: str,
    title: str,
    system_name: str,
    chunk_chars: int,
    provider: str = "openai",
) -> Dict[str, Any]:
    """
    Convert a long-form document into a structured requirement set.

    The result matches the structured requirement-set JSON envelope consumed by this toolkit.
    """
    source_text = load_text(source_path)

    aggregated_requirements: List[Dict[str, Any]] = []
    aggregated_assumptions: List[str] = []

    resolved_provider = _normalise_provider(provider)
    client = _new_openai_client() if resolved_provider == "openai" else None
    source_chunks = chunk_text(source_text, max_chars=chunk_chars)
    if not source_chunks:
        raise RuntimeError("Source document is empty after preprocessing.")

    next_index = 1
    id_prefix = f"{set_id.upper()}-R"

    for chunk in source_chunks:
        result = await extract_requirements_from_chunk(
            chunk_text=chunk,
            model=model,
            id_prefix=id_prefix,
            start_index=next_index,
            provider=resolved_provider,
            client=client,
        )
        requirements = result["requirements"]
        assumptions = result["assumptions"]
        aggregated_requirements.extend(requirements)
        aggregated_assumptions.extend(assumptions)
        next_index += len(requirements)

    # Deduplicate assumptions while preserving order.
    seen = set()
    unique_assumptions: List[str] = []
    for assumption in aggregated_assumptions:
        if assumption not in seen:
            seen.add(assumption)
            unique_assumptions.append(assumption)

    if not aggregated_requirements:
        raise RuntimeError("No requirements were extracted. Consider reducing chunk size or revising the source.")

    return {
        "id": set_id,
        "title": title,
        "system": system_name,
        "requirements": aggregated_requirements,
        "assumptions": unique_assumptions,
        "source_file": str(source_path),
        "metadata": {
            "model": model,
            "chunk_char_limit": chunk_chars,
            "total_chunks": len(source_chunks),
            "mode": resolved_provider,
        },
    }


async def run_formalize_intent_flow(
    source_path: Path,
    model: str,
    set_id: str,
    title: str,
    system_name: str,
    provider: str = "openai",
) -> Dict[str, Any]:
    requirements = load_requirements_source(source_path)
    resolved_provider = _normalise_provider(provider)
    client = _new_openai_client() if resolved_provider == "openai" else None
    payload = await formalize_intent_with_llm(
        requirements=requirements,
        model=model,
        set_id=set_id,
        title=title,
        system_name=system_name,
        provider=resolved_provider,
        client=client,
    )
    mode = resolved_provider

    intent_profile = payload.get("intent_profile", {})
    formalization_requirements = payload.get("formalization_requirements", [])
    ambiguities = payload.get("ambiguities", [])
    traceability_summary = payload.get("traceability_summary", {})

    requirement_set = payload.get("requirement_set")
    if not isinstance(requirement_set, dict):
        requirement_set = _to_requirement_set(
            set_id=set_id,
            title=title,
            system_name=system_name,
            formalization_requirements=formalization_requirements,
            assumptions=intent_profile.get("global_assumptions", []),
        )

    return {
        "stage": "formalize_intent",
        "mode": mode,
        "source_file": str(source_path),
        "set_id": set_id,
        "title": title,
        "system": system_name,
        "raw_requirements": requirements,
        "intent_profile": intent_profile,
        "formalization_requirements": formalization_requirements,
        "ambiguities": ambiguities,
        "traceability_summary": traceability_summary,
        "requirement_set": requirement_set,
        "metadata": {
            "model": model,
            "requirement_count": len(requirements),
        },
    }


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Utility CLI for MBSE workflows (OpenAI API or Codex CLI).")
    subparsers = parser.add_subparsers(dest="command", required=True)

    translate_parser = subparsers.add_parser("translate", help="Translate a requirements statement.")
    translate_parser.add_argument(
        "--statement",
        type=Path,
        required=True,
        help=(
            "Path to the requirements CSV file. Prototype is CSV-only; "
            "PDF/TXT/JSON are not supported."
        ),
    )
    translate_parser.add_argument(
        "--model",
        default=DEFAULT_TRANSLATION_MODEL,
        help="Chat completion model to use for translation (default: %(default)s).",
    )
    translate_parser.add_argument(
        "--provider",
        default=DEFAULT_LLM_PROVIDER,
        choices=["openai", "codex"],
        help="LLM backend provider (default: %(default)s).",
    )
    translate_parser.add_argument(
        "--write-smt-prefix",
        type=Path,
        help="If provided, write SAT/UNSAT SMT-LIB files using this prefix (suffix _sat/_unsat.smt2).",
    )
    translate_parser.add_argument(
        "--unsat-extra",
        help="Additional SMT-LIB assertions appended to generate the UNSAT variant (default: assert false).",
    )
    translate_parser.add_argument(
        "--output-json",
        type=Path,
        help="Write the translate result JSON to the specified path instead of stdout.",
    )

    harvest_parser = subparsers.add_parser(
        "harvest",
        help="Extract functional requirements from a long-form document.",
    )
    harvest_parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help=(
            "Path to the requirements CSV file. Prototype is CSV-only; "
            "PDF/TXT/JSON are not supported."
        ),
    )
    harvest_parser.add_argument(
        "--set-id",
        default="REQSET",
        help="Identifier for the requirement set (default: %(default)s).",
    )
    harvest_parser.add_argument(
        "--title",
        default="Auto-extracted Requirement Set",
        help="Human-readable title for the requirement set.",
    )
    harvest_parser.add_argument(
        "--system",
        default="Unknown System",
        help="Name/description of the system these requirements describe.",
    )
    harvest_parser.add_argument(
        "--model",
        default=DEFAULT_HARVEST_MODEL,
        help="Chat completion model for requirement extraction (default: %(default)s).",
    )
    harvest_parser.add_argument(
        "--provider",
        default=DEFAULT_LLM_PROVIDER,
        choices=["openai", "codex"],
        help="LLM backend provider (default: %(default)s).",
    )
    harvest_parser.add_argument(
        "--chunk-chars",
        type=int,
        default=3200,
        help="Maximum number of characters per chunk sent to the LLM (default: %(default)s).",
    )
    harvest_parser.add_argument(
        "--output-json",
        type=Path,
        help="Write harvest output JSON to this path instead of stdout.",
    )

    formalize_parser = subparsers.add_parser(
        "formalize_intent",
        help="Convert source requirements into explicit intent formalization artifacts.",
    )
    formalize_parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help=(
            "Path to a requirements source CSV file. Prototype is CSV-only; "
            "PDF/TXT/JSON are not supported."
        ),
    )
    formalize_parser.add_argument(
        "--set-id",
        default="AGREEDOG-PRELIM",
        help="Identifier for the generated requirement set (default: %(default)s).",
    )
    formalize_parser.add_argument(
        "--title",
        default="AGREE-Dog Preliminary Intent Formalization",
        help="Title for the generated requirement set.",
    )
    formalize_parser.add_argument(
        "--system",
        default="AGREE-Dog Inspired Verification Workflow",
        help="System description used in the formalization context.",
    )
    formalize_parser.add_argument(
        "--model",
        default=DEFAULT_INTENT_MODEL,
        help="Chat completion model for intent formalization (default: %(default)s).",
    )
    formalize_parser.add_argument(
        "--provider",
        default=DEFAULT_LLM_PROVIDER,
        choices=["openai", "codex"],
        help="LLM backend provider (default: %(default)s).",
    )
    formalize_parser.add_argument(
        "--output-json",
        type=Path,
        help="Write formalize_intent output JSON to this path instead of stdout.",
    )

    return parser.parse_args(argv)


async def dispatch(args: argparse.Namespace) -> Any:
    provider = _normalise_provider(getattr(args, "provider", DEFAULT_LLM_PROVIDER))
    model = getattr(args, "model", DEFAULT_TRANSLATION_MODEL)
    if provider == "codex":
        model = _resolve_codex_model(model)

    if args.command == "translate":
        return await run_translate_flow(
            statement_path=args.statement,
            model=model,
            smt_prefix=args.write_smt_prefix,
            unsat_extra=args.unsat_extra,
            provider=provider,
        )
    if args.command == "harvest":
        return await run_harvest_flow(
            source_path=args.source,
            model=model,
            set_id=args.set_id,
            title=args.title,
            system_name=args.system,
            chunk_chars=args.chunk_chars,
            provider=provider,
        )
    if args.command == "formalize_intent":
        return await run_formalize_intent_flow(
            source_path=args.source,
            model=model,
            set_id=args.set_id,
            title=args.title,
            system_name=args.system,
            provider=provider,
        )
    raise ValueError(f"Unsupported command: {args.command}")


def _enforce_csv_input(path: Path, *, arg_name: str) -> None:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return
    rendered_suffix = suffix if suffix else "<none>"
    raise RuntimeError(
        f"{arg_name} must reference a .csv requirements file in the current prototype. "
        f"Got {path} (extension: {rendered_suffix}). "
        "PDF/TXT/JSON inputs are not supported at this stage."
    )


def _validate_csv_only_contract(args: argparse.Namespace) -> None:
    command = getattr(args, "command", None)
    if command == "translate":
        _enforce_csv_input(Path(args.statement), arg_name="--statement")
        return
    if command in {"harvest", "formalize_intent"}:
        _enforce_csv_input(Path(args.source), arg_name="--source")
        return
    if hasattr(args, "statement"):
        _enforce_csv_input(Path(args.statement), arg_name="--statement")


DEFAULT_MODEL = os.getenv("OPENAI_TRANSLATION_MODEL", "gpt-4o")
DEFAULT_LLM_PROVIDER = os.getenv("MBSE_LLM_PROVIDER", "openai").strip().lower()
DEFAULT_EXIT_COMMAND = os.getenv("SYSML_EXIT_COMMAND", "%exit")
DEFAULT_SYSML_MODE = os.getenv("MBSE_SYSML_MODE", "evidence").strip().lower()
DEFAULT_ARCHITECTURE_TEMPLATE_PATH = (
    Path(__file__).resolve().parent / "architecture_domain_ir_template.json"
)


def _candidate_sysml_prefixes() -> List[Path]:
    home = Path.home()
    return [
        home / "miniforge3" / "envs" / "sysml-0.57.0",
        home / "miniforge3" / "envs" / "sysml-0.52.0",
        home / "miniforge3",
        home / "miniconda3" / "envs" / "sysml-0.57.0",
        home / "miniconda3" / "envs" / "sysml-0.52.0",
        home / "miniconda3",
    ]


def _discover_default_sysml_jar() -> Path:
    override = os.getenv("SYSML_KERNEL_JAR")
    if override:
        return Path(override)

    discovered: List[Path] = []
    for prefix in _candidate_sysml_prefixes():
        kernel_dir = prefix / "share" / "jupyter" / "kernels" / "sysml"
        if not kernel_dir.exists():
            continue
        discovered.extend(sorted(kernel_dir.glob("jupyter-sysml-kernel-*-all.jar"), reverse=True))
    for candidate in discovered:
        if candidate.exists():
            return candidate

    # Fallback path for error messages when no installation is present.
    fallback = (
        Path.home()
        / "miniforge3"
        / "share"
        / "jupyter"
        / "kernels"
        / "sysml"
        / "jupyter-sysml-kernel-0.57.0-all.jar"
    )
    return fallback


DEFAULT_SYSML_JAR = _discover_default_sysml_jar()


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate requirements → SMT (sat/unsat) → SysML model and validate with SysML kernel."
    )
    parser.add_argument(
        "--statement",
        required=True,
        type=Path,
        help=(
            "Path to the requirements CSV file fed into the translation flow. "
            "Prototype is CSV-only; PDF/TXT/JSON are not supported."
        ),
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
        "--sysml-mode",
        default=DEFAULT_SYSML_MODE,
        choices=["evidence", "architecture", "domain"],
        help="SysML emission mode: evidence scaffold, workflow architecture, or domain+trace split (default: %(default)s).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"LLM model identifier (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--llm-provider",
        default=DEFAULT_LLM_PROVIDER,
        choices=["openai", "codex"],
        help="LLM backend provider for translation (default: %(default)s).",
    )
    parser.add_argument(
        "--sysml-jar",
        default=DEFAULT_SYSML_JAR,
        type=Path,
        help="Path to the SysML kernel fat JAR (default: auto-discovered from local conda/miniforge installs).",
    )
    parser.add_argument(
        "--sysml-context",
        type=Path,
        help="Optional SysML snippet that provides modelling patterns for the generated package.",
    )
    parser.add_argument(
        "--architecture-template",
        type=Path,
        help=(
            "Optional JSON template for architecture-mode Domain IR scaffold. "
            f"Defaults to {DEFAULT_ARCHITECTURE_TEMPLATE_PATH} or MBSE_ARCH_TEMPLATE."
        ),
    )
    parser.add_argument(
        "--traceability-output",
        type=Path,
        help="Optional output path for a separate traceability SysML module (used by --sysml-mode domain).",
    )
    parser.add_argument(
        "--skip-sysml-compile",
        action="store_true",
        help="Generate the SysML model but skip invoking the SysML kernel for validation.",
    )
    parser.add_argument(
        "--require-sysml-compile",
        action="store_true",
        help="Fail if SysML kernel validation fails (default: warn and continue).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Timeout in seconds for the SysML kernel compilation step (default: 300s).",
    )
    parser.add_argument(
        "--skip-intent-formalization",
        action="store_true",
        help="Skip the default intent-formalization pre-stage and translate directly from --statement.",
    )
    parser.add_argument(
        "--require-intent-formalization",
        action="store_true",
        help="Fail the run if intent formalization fails (default: warn and continue with raw statement).",
    )
    parser.add_argument(
        "--intent-set-id",
        help="Optional set-id passed to the intent-formalization stage.",
    )
    parser.add_argument(
        "--intent-title",
        help="Optional title passed to the intent-formalization stage.",
    )
    parser.add_argument(
        "--intent-system",
        help="Optional system label passed to the intent-formalization stage.",
    )
    parser.add_argument(
        "--semantic-strict",
        action="store_true",
        help="Fail the run when semantic SMT checks detect requirement-level issues.",
    )
    parser.add_argument(
        "--approve-weakened",
        action="store_true",
        help="Allow the repair loop to weaken or temporally shift requirement encodings without blocking.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        help=(
            "Collect all output artefacts under a CSV-named subdirectory "
        ),
    )
    return parser.parse_args(argv)


def _validation_result_head(validation: Dict[str, Any]) -> str:
    result_text = str(validation.get("result") or "").strip()
    if not result_text:
        return "n/a"
    return result_text.splitlines()[0].strip()


def _ensure_success(validation: Dict[str, Any], expected: str) -> None:
    status = validation.get("status")
    result = validation.get("result")
    result_head = _validation_result_head(validation)
    if status != "ok" or result_head != expected:
        raise RuntimeError(
            f"Expected {expected!r} validation to succeed (status=ok, first_line={expected}) "
            f"but saw status={status!r}, first_line={result_head!r}, full_result={result!r}, "
            f"diagnostics={validation.get('diagnostics')!r}"
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


def _semantic_issue_counts(semantic_results: Dict[str, Any]) -> Dict[str, int]:
    checks = semantic_results.get("checks", {})
    named = checks.get("named_assertion_coverage", {})
    same_state = checks.get("same_state", {})
    pairwise = checks.get("pairwise_conflict", {})
    vacuity = checks.get("vacuity", {})
    symbol_drift = checks.get("symbol_drift", {})
    return {
        "missing_named_assertions": len(named.get("missing", [])),
        "same_state_issues": len(same_state.get("issues", [])),
        "pairwise_conflicts": len(pairwise.get("conflicts", [])),
        "pairwise_solver_errors": len(pairwise.get("solver_errors", [])),
        "vacuous_requirements": len(vacuity.get("vacuous_requirements", [])),
        "tautological_antecedents": len(vacuity.get("tautological_antecedents", [])),
        "vacuity_solver_errors": len(vacuity.get("solver_errors", [])),
        "unknown_symbols": len(symbol_drift.get("unknown_symbols", [])),
    }


def _semantic_summary(semantic_results: Dict[str, Any]) -> str:
    counts = _semantic_issue_counts(semantic_results)
    return ", ".join(f"{key}={value}" for key, value in counts.items())


def _load_architecture_template(template_path: Optional[Path]) -> Dict[str, Any]:
    override = os.getenv("MBSE_ARCH_TEMPLATE", "").strip()
    if template_path is not None:
        candidate = template_path
    elif override:
        candidate = Path(override)
    else:
        candidate = DEFAULT_ARCHITECTURE_TEMPLATE_PATH

    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Architecture template not found at "
            f"{candidate}. Provide --architecture-template or set MBSE_ARCH_TEMPLATE."
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Architecture template is not valid JSON: {candidate} ({exc})") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Architecture template must be a JSON object, got {type(payload).__name__}: {candidate}"
        )

    required_list_keys = (
        "components",
        "ports",
        "messages",
        "states",
        "transitions",
        "base_quantities",
        "base_constraints",
        "connections",
    )
    missing = [key for key in required_list_keys if key not in payload]
    if "requirement_map" not in payload:
        missing.append("requirement_map")
    if missing:
        raise RuntimeError(
            f"Architecture template missing required keys {missing}: {candidate}"
        )

    for key in required_list_keys:
        value = payload.get(key)
        if not isinstance(value, list):
            raise RuntimeError(
                f"Architecture template key {key!r} must be a list (got {type(value).__name__}): {candidate}"
            )
        bad_indexes = [idx for idx, item in enumerate(value) if not isinstance(item, dict)]
        if bad_indexes:
            raise RuntimeError(
                f"Architecture template key {key!r} contains non-object entries at indexes "
                f"{bad_indexes}: {candidate}"
            )

    requirement_map = payload.get("requirement_map")
    if not isinstance(requirement_map, dict):
        raise RuntimeError(
            f"Architecture template key 'requirement_map' must be an object: {candidate}"
        )
    bad_map_values = [
        key
        for key, value in requirement_map.items()
        if not isinstance(key, str) or not isinstance(value, str)
    ]
    if bad_map_values:
        raise RuntimeError(
            "Architecture template requirement_map must be string-to-string. "
            f"Invalid entries: {bad_map_values} ({candidate})"
        )

    return payload


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


def _sanitize_identifier(value: str, prefix: str = "id") -> str:
    safe_prefix = str(prefix or "").strip() or "id"
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in value.strip())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    cleaned = cleaned.strip("_")
    if not cleaned:
        cleaned = safe_prefix
    if cleaned[0].isdigit():
        cleaned = f"{safe_prefix}_{cleaned}"
    return cleaned


def _scalar_type_name(type_name: str) -> str:
    normalized = (type_name or "").strip().lower()
    if normalized in {"int", "integer"}:
        return "ScalarValues::Integer"
    if normalized in {"real", "float", "double", "number"}:
        return "ScalarValues::Real"
    return "ScalarValues::String"


def _resolve_requirements_tlf(translate_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return requirements-centric TLR regardless of legacy/new key naming."""
    candidates = [
        translate_payload.get("typed_requirements_form"),
        translate_payload.get("typed_logical_form"),  # legacy alias in older payloads
    ]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        try:
            statement_path = str(translate_payload.get("statement_path", "") or "<unknown>")
            return as_requirements_tlr(candidate, statement_path=statement_path)
        except RuntimeError:
            continue
    return None


def _fallback_tlf_requirements(translate_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    fallback_text = str(translate_payload.get("statement", "")).strip()
    return [
        {
            "id": "R1",
            "text": fallback_text if fallback_text else "Auto-generated requirement placeholder.",
            "category": "functional",
            "temporal_kind": "event_triggered",
        }
    ]


def _extract_tlf_requirements_context(
    translate_payload: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
    tlf_payload = _resolve_requirements_tlf(translate_payload)
    tlf_reqs: List[Dict[str, Any]] = []
    tlf_traceability: List[Dict[str, Any]] = []
    source_sha = "unknown"
    if isinstance(tlf_payload, dict):
        reqs_raw = tlf_payload.get("requirements")
        if isinstance(reqs_raw, list):
            tlf_reqs = [item for item in reqs_raw if isinstance(item, dict)]
        trace_raw = tlf_payload.get("traceability")
        if isinstance(trace_raw, list):
            tlf_traceability = [item for item in trace_raw if isinstance(item, dict)]
        source = tlf_payload.get("source")
        if isinstance(source, dict):
            source_sha = str(source.get("statement_sha256", "unknown"))
    if not tlf_reqs:
        tlf_reqs = _fallback_tlf_requirements(translate_payload)
    return tlf_reqs, tlf_traceability, source_sha


def _tlf_requirement_ids(translate_payload: Dict[str, Any]) -> List[str]:
    tlf_payload = _resolve_requirements_tlf(translate_payload)
    if not isinstance(tlf_payload, dict):
        return []
    reqs_raw = tlf_payload.get("requirements")
    if not isinstance(reqs_raw, list):
        return []
    ids = [str(item.get("id", "")).strip() for item in reqs_raw if isinstance(item, dict)]
    return [item for item in ids if item]


def _validate_requirement_coverage(
    produced_ids: Sequence[str],
    translate_payload: Dict[str, Any],
    mode_label: str,
) -> None:
    expected = set(_tlf_requirement_ids(translate_payload))
    if not expected:
        return
    produced = {item.strip() for item in produced_ids if item and item.strip()}
    missing = sorted(expected - produced)
    if missing:
        raise RuntimeError(f"{mode_label} mode dropped requirements from TLR: {missing}")


def _build_solver_evidence(
    sat_entry: Dict[str, Any],
    unsat_entry: Dict[str, Any],
) -> Dict[str, Any]:
    sat_info = sat_entry.get("validation", {})
    unsat_info = unsat_entry.get("validation", {})
    return {
        "sat_path": str(Path(sat_entry["path"]).as_posix()),
        "unsat_path": str(Path(unsat_entry["path"]).as_posix()),
        "sat_status": _validation_result_head(sat_info),
        "unsat_status": _validation_result_head(unsat_info),
        "sat_exit_code": sat_info.get("exit_code"),
        "unsat_exit_code": unsat_info.get("exit_code"),
    }


def _validate_required_keys(payload: Dict[str, Any], required_top: Sequence[str], label: str) -> None:
    missing = sorted(set(required_top) - set(payload.keys()))
    if missing:
        raise RuntimeError(f"{label} missing required keys: {missing}")


def _validate_list_keys(payload: Dict[str, Any], list_keys: Sequence[str], label: str) -> None:
    for key in list_keys:
        if not isinstance(payload.get(key), list):
            raise RuntimeError(f"{label} key {key!r} must be a list.")


def _validate_dict_list(
    payload: Dict[str, Any],
    key: str,
    label: str,
    *,
    allow_empty: bool = False,
) -> List[Dict[str, Any]]:
    raw = payload.get(key)
    if not isinstance(raw, list):
        raise RuntimeError(f"{label} key {key!r} must be a list.")
    bad_indexes = [index for index, item in enumerate(raw) if not isinstance(item, dict)]
    if bad_indexes:
        raise RuntimeError(f"{label} key {key!r} contains non-object entries at indexes {bad_indexes}.")
    items = [item for item in raw if isinstance(item, dict)]
    if not allow_empty and not items:
        raise RuntimeError(f"{label} key {key!r} must include at least one object.")
    return items


def _require_non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{label} must be a string, got {type(value).__name__}.")
    normalized = value.strip()
    if not normalized:
        raise RuntimeError(f"{label} cannot be empty.")
    return normalized


def _build_domain_ir(
    package_name: str,
    statement_path: str,
    translate_payload: Dict[str, Any],
    sat_entry: Dict[str, Any],
    unsat_entry: Dict[str, Any],
    architecture_template: Dict[str, Any],
) -> Dict[str, Any]:
    tlf_reqs, tlf_traceability, source_sha = _extract_tlf_requirements_context(translate_payload)

    components = copy.deepcopy(architecture_template["components"])
    ports = copy.deepcopy(architecture_template["ports"])
    messages = copy.deepcopy(architecture_template["messages"])
    states = copy.deepcopy(architecture_template["states"])
    transitions = copy.deepcopy(architecture_template["transitions"])
    quantities: List[Dict[str, Any]] = copy.deepcopy(architecture_template["base_quantities"])
    constraints: List[Dict[str, Any]] = copy.deepcopy(architecture_template["base_constraints"])
    requirement_map = copy.deepcopy(architecture_template["requirement_map"])

    requirement_entries: List[Dict[str, Any]] = []
    traceability: List[Dict[str, Any]] = []
    for idx, req in enumerate(tlf_reqs, start=1):
        req_id = str(req.get("id", f"R{idx}")).strip() or f"R{idx}"
        req_text = str(req.get("text", "")).strip() or f"Requirement {req_id}"
        req_category = str(req.get("category", "functional")).strip() or "functional"
        temporal_kind = str(req.get("temporal_kind", "event_triggered")).strip() or "event_triggered"
        status_symbol = f"req_{_sanitize_identifier(req_id, prefix='r')}_satisfied"
        req_name = f"Req_{_sanitize_identifier(req_id, prefix='R')}"
        mapped_component = requirement_map.get(req_category, "controller")

        quantities.append(
            {"owner": "controller", "name": status_symbol, "type": "Integer", "default": 0}
        )
        constraints.append(
            {
                "owner": "controller",
                "name": f"{status_symbol}_range",
                "expr": f"{status_symbol} >= 0 & {status_symbol} <= 1",
            }
        )

        requirement_entries.append(
            {
                "id": req_id,
                "sysml_name": req_name,
                "text": req_text,
                "category": req_category,
                "temporal_kind": temporal_kind,
                "status_symbol": status_symbol,
                "mapped_component": mapped_component,
            }
        )
        traceability.append(
            {
                "requirement_id": req_id,
                "sysml_requirement": req_name,
                "mapped_component": mapped_component,
                "status_symbol": status_symbol,
                "tlf_requirement_index": idx - 1,
            }
        )

    for item in tlf_traceability:
        req_id = str(item.get("requirement_id", "")).strip()
        if not req_id:
            continue
        matches = [entry for entry in traceability if entry["requirement_id"] == req_id]
        if not matches:
            continue
        span = item.get("source_span", {})
        if isinstance(span, dict):
            matches[0]["source_span"] = {
                "line_start": int(span.get("line_start", 0) or 0),
                "line_end": int(span.get("line_end", 0) or 0),
            }

    connections = copy.deepcopy(architecture_template["connections"])

    return {
        "schema_version": "1.0",
        "mode": "architecture",
        "package_name": package_name,
        "source": {
            "statement_path": statement_path,
            "statement_sha256": source_sha,
        },
        "components": components,
        "ports": ports,
        "messages": messages,
        "states": states,
        "transitions": transitions,
        "quantities": quantities,
        "constraints": constraints,
        "connections": connections,
        "requirements": requirement_entries,
        "traceability": traceability,
        "solver_evidence": _build_solver_evidence(sat_entry, unsat_entry),
    }


def _validate_domain_ir_schema(domain_ir: Dict[str, Any]) -> None:
    required_top = [
        "schema_version",
        "mode",
        "package_name",
        "source",
        "components",
        "ports",
        "messages",
        "states",
        "transitions",
        "quantities",
        "constraints",
        "connections",
        "requirements",
        "traceability",
        "solver_evidence",
    ]
    _validate_required_keys(domain_ir, required_top, "Domain IR")

    _validate_list_keys(
        domain_ir,
        (
            "components",
            "ports",
            "messages",
            "states",
            "transitions",
            "quantities",
            "constraints",
            "connections",
            "requirements",
            "traceability",
        ),
        "Domain IR",
    )

    components = domain_ir["components"]
    component_ids = set()
    for component in components:
        if not isinstance(component, dict):
            raise RuntimeError("Every component entry must be an object.")
        for key in ("id", "name", "description"):
            if key not in component:
                raise RuntimeError(f"Component entry missing key {key!r}: {component!r}")
        component_ids.add(str(component["id"]))
    if not component_ids:
        raise RuntimeError("Domain IR must define at least one component.")

    state_names = {str(state.get("name", "")) for state in domain_ir["states"] if isinstance(state, dict)}
    if not state_names:
        raise RuntimeError("Domain IR must define at least one state.")

    for port in domain_ir["ports"]:
        if not isinstance(port, dict):
            raise RuntimeError("Every port entry must be an object.")
        for key in ("owner", "name", "direction", "data_type", "signal"):
            if key not in port:
                raise RuntimeError(f"Port entry missing key {key!r}: {port!r}")
        owner = str(port["owner"])
        if owner not in component_ids:
            raise RuntimeError(f"Port owner {owner!r} does not match any component id.")
        direction = str(port["direction"])
        if direction not in {"in", "out"}:
            raise RuntimeError(f"Port direction must be 'in' or 'out', got {direction!r}.")

    for transition in domain_ir["transitions"]:
        if not isinstance(transition, dict):
            raise RuntimeError("Every transition entry must be an object.")
        for key in ("name", "from", "trigger", "to"):
            if key not in transition:
                raise RuntimeError(f"Transition entry missing key {key!r}: {transition!r}")
        if str(transition["from"]) not in state_names or str(transition["to"]) not in state_names:
            raise RuntimeError(
                f"Transition references unknown states: {transition!r}; known states={sorted(state_names)}"
            )

    requirements = domain_ir["requirements"]
    requirement_ids = []
    for req in requirements:
        if not isinstance(req, dict):
            raise RuntimeError("Every requirement entry must be an object.")
        for key in ("id", "sysml_name", "text", "category", "temporal_kind", "status_symbol", "mapped_component"):
            if key not in req:
                raise RuntimeError(f"Requirement entry missing key {key!r}: {req!r}")
        requirement_ids.append(str(req["id"]))
        if str(req["mapped_component"]) not in component_ids:
            raise RuntimeError(
                f"Requirement {req['id']!r} mapped to unknown component {req['mapped_component']!r}."
            )
    if len(requirement_ids) != len(set(requirement_ids)):
        raise RuntimeError("Requirement IDs in Domain IR must be unique.")

    quantity_names = {str(quantity.get("name", "")) for quantity in domain_ir["quantities"] if isinstance(quantity, dict)}
    for req in requirements:
        if str(req["status_symbol"]) not in quantity_names:
            raise RuntimeError(
                f"Requirement {req['id']!r} status symbol {req['status_symbol']!r} is missing from quantities."
            )

    traceability_ids = {
        str(item.get("requirement_id", ""))
        for item in domain_ir["traceability"]
        if isinstance(item, dict)
    }
    missing_traceability = sorted(set(requirement_ids) - traceability_ids)
    if missing_traceability:
        raise RuntimeError(f"Traceability missing requirement IDs: {missing_traceability}")


def _validate_domain_ir_coverage(
    domain_ir: Dict[str, Any],
    translate_payload: Dict[str, Any],
) -> None:
    produced = [
        str(item.get("id", "")).strip()
        for item in domain_ir.get("requirements", [])
        if isinstance(item, dict)
    ]
    _validate_requirement_coverage(produced, translate_payload, "Architecture")


def _normalize_requirement_category(value: str) -> str:
    normalized = (value or "").strip().lower()
    allowed = {"safety", "liveness", "functional", "performance", "constraint"}
    if normalized in allowed:
        return normalized
    return "functional"


def _to_pascal_identifier(value: Any, prefix: str = "Entity") -> str:
    safe_prefix = str(prefix or "").strip() or "Entity"
    if value is None:
        return safe_prefix
    raw_value = str(value).strip()
    if not raw_value:
        return safe_prefix
    cleaned = re.sub(r"[^A-Za-z0-9]+", " ", raw_value)
    words = [word for word in cleaned.split() if word]
    if not words:
        return safe_prefix
    candidate = "".join(word[:1].upper() + word[1:] for word in words)
    if not candidate:
        return safe_prefix
    if candidate and candidate[0].isdigit():
        candidate = f"{safe_prefix}{candidate}"
    return _sanitize_identifier(candidate, prefix=safe_prefix)


def _to_lower_camel(value: Any, prefix: str = "node") -> str:
    safe_prefix = str(prefix or "").strip() or "node"
    pascal = _to_pascal_identifier(value, prefix=safe_prefix)
    if not pascal:
        return safe_prefix
    if len(pascal) == 1:
        candidate = pascal.lower()
    else:
        candidate = pascal[0].lower() + pascal[1:]
    if not candidate:
        return safe_prefix
    return _sanitize_identifier(candidate, prefix=safe_prefix)


_DOMAIN_ENTITY_ONTOLOGY: Dict[str, Dict[str, Any]] = {
    "CoreSystem": {
        "keywords": (
            "system",
            "workflow",
            "controller",
            "platform",
            "assistant",
        ),
        "description": "Primary system under specification and control context.",
    },
    "Network": {
        "keywords": (
            "network",
            "infrastructure",
            "interoperability",
            "protocol",
            "gsm-r",
            "eirene",
        ),
        "description": "Network and infrastructure capabilities required by the domain.",
    },
    "Radio": {
        "keywords": (
            "radio",
            "telephony",
            "cab",
            "train",
            "railway",
            "mobile",
            "ground-train",
        ),
        "description": "Radio and telephony subsystem responsibilities.",
    },
    "Service": {
        "keywords": (
            "service",
            "services",
            "monitoring",
            "function",
            "feature",
            "interface",
            "addressing",
        ),
        "description": "Operational service capabilities exposed by the system.",
    },
    "CallType": {
        "keywords": (
            "call",
            "voice",
            "broadcast",
            "group",
            "point-to-point",
            "emergency",
            "communication",
            "message",
            "duplex",
        ),
        "description": "Supported call/communication interaction types and policies.",
    },
}

_ENTITY_PHRASE_OVERRIDES: Dict[str, str] = {
    "mining frigate": "MiningFrigate",
    "mining corporation": "MiningCorporation",
    "fleet commander": "FleetCommander",
    "pilot pod": "PilotPod",
    "rorqual": "Rorqual",
}

_GENERIC_ENTITY_TOKENS = {
    "system",
    "workflow",
    "platform",
    "assistant",
    "service",
    "services",
    "process",
    "application",
    "software",
    "module",
    "component",
    "controller",
}


def _is_clause_like_phrase(phrase: str) -> bool:
    lowered = re.sub(r"\s+", " ", phrase.strip().lower())
    if not lowered:
        return True
    clause_prefixes = (
        "for each",
        "during",
        "such that",
        "when",
        "if",
        "while",
        "after",
        "before",
        "on ",
        "in ",
        "where ",
    )
    if any(lowered.startswith(prefix) for prefix in clause_prefixes):
        return True
    if re.search(r"\b(?:shall|must|should)\b", lowered):
        return True
    tokens = [token for token in lowered.split(" ") if token]
    if len(tokens) > 5:
        return True
    return False


def _normalize_domain_entity_phrase(phrase: Any) -> Optional[str]:
    if phrase is None:
        return None
    raw_phrase = str(phrase).strip(" ,.;:")
    if not raw_phrase:
        return None
    normalized = re.sub(r"\s+", " ", raw_phrase)
    normalized = re.sub(r"^(?:the|a|an)\s+", "", normalized, flags=re.IGNORECASE)
    normalized = re.split(r"\b(?:that|which|who)\b", normalized, maxsplit=1, flags=re.IGNORECASE)[0].strip(
        " ,.;:"
    )
    if not normalized:
        return None
    if _is_clause_like_phrase(normalized):
        return None
    return normalized


def _rank_ontology_entities(text: str, phrases: Sequence[str]) -> List[str]:
    lowered_text = text.lower()
    lowered_phrases = [item.lower() for item in phrases if item]
    scored: List[Tuple[str, int]] = []
    for priority, (entity_name, spec) in enumerate(_DOMAIN_ENTITY_ONTOLOGY.items()):
        score = 0
        for keyword in spec["keywords"]:
            if keyword in lowered_text:
                score += 2
            score += sum(1 for phrase in lowered_phrases if keyword in phrase)
        if score > 0:
            # Keep ontology declaration order stable for ties.
            scored.append((entity_name, score * 10 - priority))
    if not scored:
        return ["CoreSystem"]
    ranked = [name for name, _ in sorted(scored, key=lambda item: item[1], reverse=True)]
    if "CoreSystem" in ranked:
        ranked = ["CoreSystem", *[item for item in ranked if item != "CoreSystem"]]
    return ranked[:3]


def _entity_name_from_phrase(phrase: str) -> Optional[str]:
    normalized = _normalize_domain_entity_phrase(phrase)
    if not normalized:
        return None

    canonical_phrase = _canonical_entity_key(normalized)
    if not canonical_phrase:
        return None

    override = _ENTITY_PHRASE_OVERRIDES.get(canonical_phrase)
    if override:
        return override

    tokens = [token for token in canonical_phrase.split(" ") if token]
    if not tokens or len(tokens) > 4:
        return None
    if any(token in {"shall", "must"} for token in tokens):
        return None

    filtered_tokens = [token for token in tokens if token not in _GENERIC_ENTITY_TOKENS]
    if not filtered_tokens:
        return None
    if len(filtered_tokens) == 1 and filtered_tokens[0] in {"requirement", "requirements", "operations"}:
        return None

    return _to_pascal_identifier(" ".join(filtered_tokens), prefix="DomainEntity")


def _map_requirement_entities(text: str, phrases: Sequence[str]) -> List[str]:
    entities: List[str] = []

    for phrase in phrases:
        entity = _entity_name_from_phrase(phrase)
        if entity and entity not in entities:
            entities.append(entity)

    lowered = text.lower()
    for phrase_key, entity_name in _ENTITY_PHRASE_OVERRIDES.items():
        if phrase_key in lowered and entity_name not in entities:
            entities.append(entity_name)

    if entities:
        return entities[:3]
    return _rank_ontology_entities(text, phrases)


def _ontology_entry(entity_name: str, source_phrase: Optional[str] = None) -> Dict[str, str]:
    spec = _DOMAIN_ENTITY_ONTOLOGY.get(entity_name, {})
    description = str(spec.get("description", f"Domain entity representing {entity_name}."))
    return {
        "name": entity_name,
        "source_phrase": source_phrase or entity_name,
        "description": description,
    }


def _extract_subject_phrase(text: Any) -> Optional[str]:
    if text is None:
        return None
    source_text = str(text).strip()
    if not source_text:
        return None
    subject_patterns = [
        r"^\s*(?:the|a|an)\s+(.+?)\s+shall\b",
        r"^\s*(.+?)\s+shall\b",
        r"^\s*(?:the|a|an)\s+(.+?)\s+must\b",
    ]
    for pattern in subject_patterns:
        match = re.search(pattern, source_text, flags=re.IGNORECASE)
        if not match:
            continue
        subject = re.sub(r"\s+", " ", match.group(1)).strip(" ,.;:")
        lowered = subject.lower()
        if "," in subject and lowered.startswith(("when ", "if ", "after ", "before ", "while ")):
            subject = subject.split(",", 1)[1].strip(" ,.;:")
            if not subject:
                continue
        subject = re.split(r"\b(?:that|which|who)\b", subject, maxsplit=1, flags=re.IGNORECASE)[0].strip(" ,.;:")
        if not subject:
            continue
        normalized = _normalize_domain_entity_phrase(subject)
        if normalized:
            return normalized
    return None


def _extract_service_phrases(text: str) -> List[str]:
    pattern = re.compile(
        r"\b([A-Za-z0-9][A-Za-z0-9\-\s]{2,}?)\s+"
        r"(services?|calls?|communications?|messages?)\b",
        flags=re.IGNORECASE,
    )
    phrases: List[str] = []
    for match in pattern.finditer(text):
        phrase = f"{match.group(1)} {match.group(2)}"
        phrase = re.sub(r"\s+", " ", phrase).strip(" ,.;:")
        phrase = re.sub(r"^(?:the|a|an)\s+", "", phrase, flags=re.IGNORECASE)
        tokens = [token for token in re.split(r"\s+", phrase) if token]
        if len(tokens) > 4:
            continue
        if any(token.lower() in {"shall", "must"} for token in tokens):
            continue
        if tokens and tokens[0].lower() in {
            "and",
            "or",
            "if",
            "when",
            "during",
            "in",
            "to",
            "for",
            "of",
            "with",
            "without",
            "while",
        }:
            continue
        normalized = _normalize_domain_entity_phrase(phrase)
        if normalized and normalized.lower() not in {item.lower() for item in phrases}:
            phrases.append(normalized)
    return phrases


def _extract_explicit_individual_names(text: str) -> List[str]:
    names: List[str] = []

    quoted = re.findall(r"[\"“”']([A-Za-z][A-Za-z0-9_\-]{1,})[\"“”']", text)
    for value in quoted:
        if value not in names:
            names.append(value)

    named_matches = re.findall(
        r"\b(?:named|identifier|id)\s+([A-Za-z][A-Za-z0-9_\-]{1,})\b",
        text,
        flags=re.IGNORECASE,
    )
    for value in named_matches:
        if value.lower() in {"to", "from", "the", "a", "an"}:
            continue
        if value == value.lower() and not any(ch.isdigit() or ch in {"_", "-"} for ch in value):
            continue
        if value not in names:
            names.append(value)

    return names


def _infer_state_names(text: str) -> List[str]:
    keyword_map = {
        "idle": "Idle",
        "active": "Active",
        "fault": "Fault",
        "failed": "Failed",
        "establish": "Established",
        "terminate": "Terminated",
        "manage": "Managed",
        "connect": "Connected",
        "disconnect": "Disconnected",
        "available": "Available",
        "unavailable": "Unavailable",
        "charging": "Charging",
    }
    lowered = text.lower()
    states = []
    for key, state_name in keyword_map.items():
        if key in lowered and state_name not in states:
            states.append(state_name)
    return states


def _canonical_entity_key(phrase: str) -> str:
    normalized = re.sub(r"^(?:the|a|an)\s+", "", phrase.strip(), flags=re.IGNORECASE)
    normalized = re.sub(r"[^A-Za-z0-9]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip().lower()
    return normalized


def _extract_action_phrases(text: str) -> List[str]:
    phrases: List[str] = []
    patterns = [
        r"\bshall\s+([a-z][a-z0-9\-]*(?:\s+[a-z][a-z0-9\-]*){0,2})\b",
        r"\bmust\s+([a-z][a-z0-9\-]*(?:\s+[a-z][a-z0-9\-]*){0,2})\b",
    ]
    skip_tokens = {
        "be",
        "have",
        "has",
        "is",
        "are",
        "been",
        "being",
    }
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            phrase = re.sub(r"\s+", " ", match.group(1)).strip().lower()
            if not phrase:
                continue
            first_token = phrase.split(" ", 1)[0]
            if first_token in skip_tokens:
                continue
            if phrase not in phrases:
                phrases.append(phrase)
    return phrases


def _extract_quantity_hints(text: str) -> List[Dict[str, Any]]:
    hints: List[Dict[str, Any]] = []
    pattern = re.compile(r"(\d+(?:\.\d+)?)\s*([A-Za-z%/][A-Za-z0-9%/\-]*)")
    for idx, match in enumerate(pattern.finditer(text), start=1):
        raw_value = match.group(1)
        unit = match.group(2)
        is_int = "." not in raw_value
        scalar_type = "Integer" if is_int else "Real"
        attr_name = f"metric_{idx}"
        hints.append(
            {
                "name": attr_name,
                "type": scalar_type,
                "unit": unit,
                "value": raw_value,
            }
        )
    return hints


def _sanitize_path_expression(path: str) -> str:
    segments = [segment.strip() for segment in str(path).split(".") if segment.strip()]
    if not segments:
        return ""
    cleaned = [_sanitize_identifier(segment, prefix="node") for segment in segments]
    return ".".join(cleaned)


def _normalize_list_phrase(raw: str) -> List[str]:
    phrase = re.sub(r"\bparts?\b", "", raw, flags=re.IGNORECASE)
    phrase = re.sub(r"\bthe\b", "", phrase, flags=re.IGNORECASE)
    phrase = phrase.replace(" and ", ", ")
    chunks = [chunk.strip(" ,.;:") for chunk in phrase.split(",") if chunk.strip(" ,.;:")]
    values: List[str] = []
    for chunk in chunks:
        name = _sanitize_identifier(chunk, prefix="Part")
        if name and name not in values:
            values.append(name)
    return values


def _extract_structural_fact(text: str) -> Dict[str, Any]:
    raw = str(text).strip()
    if not raw:
        return {"kind": None}

    contain_match = re.match(
        r"^\s*The\s+([A-Za-z][A-Za-z0-9_.]*)\s+(?:part\s+)?shall\s+contain\s+(.+?)\s*\.?\s*$",
        raw,
        flags=re.IGNORECASE,
    )
    if contain_match:
        parent = _sanitize_identifier(contain_match.group(1), prefix="Part")
        children = _normalize_list_phrase(contain_match.group(2))
        if parent and children:
            return {"kind": "contain", "parent": parent, "children": children}

    connect_match = re.match(
        r"^\s*The\s+([A-Za-z][A-Za-z0-9_.]*)\s+shall\s+connect\s+([A-Za-z][A-Za-z0-9_.]*)\s+to\s+([A-Za-z][A-Za-z0-9_.]*)\s*\.?\s*$",
        raw,
        flags=re.IGNORECASE,
    )
    if connect_match:
        owner = _sanitize_identifier(connect_match.group(1), prefix="Part")
        source = _sanitize_path_expression(connect_match.group(2))
        target = _sanitize_path_expression(connect_match.group(3))
        if owner and source and target:
            return {"kind": "connect", "owner": owner, "source": source, "target": target}

    port_match = re.match(
        r"^\s*The\s+([A-Za-z][A-Za-z0-9_.]*)\s+part\s+shall\s+have\s+an?\s+(input|output)\s+port\s+([A-Za-z][A-Za-z0-9_]*)\s+typed\s+by\s+([A-Za-z][A-Za-z0-9_]*)\s*\.?\s*$",
        raw,
        flags=re.IGNORECASE,
    )
    if port_match:
        owner = _sanitize_identifier(port_match.group(1), prefix="Part")
        direction = "in" if port_match.group(2).strip().lower() == "input" else "out"
        port_name = _sanitize_identifier(port_match.group(3), prefix="port")
        port_def = _sanitize_identifier(port_match.group(4), prefix="PortDef")
        if owner and port_name and port_def:
            return {
                "kind": "port",
                "owner": owner,
                "direction": direction,
                "port_name": port_name,
                "port_def": port_def,
            }

    return {"kind": None}


def _build_structural_domain_model(requirements: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    contain_edges: List[Dict[str, str]] = []
    contain_pairs: set[Tuple[str, str]] = set()
    ports_by_owner: Dict[str, List[Dict[str, str]]] = {}
    port_keys: set[Tuple[str, str, str]] = set()
    connections_by_owner: Dict[str, List[Dict[str, str]]] = {}
    connection_keys: set[Tuple[str, str, str]] = set()

    part_names: List[str] = []
    seen_parts: set[str] = set()

    def register_part(part_name: str) -> None:
        if not part_name:
            return
        if part_name in seen_parts:
            return
        seen_parts.add(part_name)
        part_names.append(part_name)

    parsed_count = 0
    for req in requirements:
        text = str(req.get("text", "")).strip()
        fact = _extract_structural_fact(text)
        kind = fact.get("kind")
        if kind is None:
            continue
        parsed_count += 1

        if kind == "contain":
            parent = str(fact["parent"])
            register_part(parent)
            for child in fact["children"]:
                child_name = str(child)
                register_part(child_name)
                edge_key = (parent, child_name)
                if edge_key in contain_pairs:
                    continue
                contain_pairs.add(edge_key)
                contain_edges.append({"parent": parent, "child": child_name})
            continue

        if kind == "port":
            owner = str(fact["owner"])
            register_part(owner)
            direction = str(fact["direction"])
            port_name = str(fact["port_name"])
            port_def = str(fact["port_def"])
            key = (owner, port_name, port_def)
            if key in port_keys:
                continue
            port_keys.add(key)
            ports_by_owner.setdefault(owner, []).append(
                {"name": port_name, "direction": direction, "port_def": port_def}
            )
            continue

        if kind == "connect":
            owner = str(fact["owner"])
            source = str(fact["source"])
            target = str(fact["target"])
            register_part(owner)
            for expr in (source, target):
                segments = [segment for segment in expr.split(".") if segment]
                if len(segments) > 1:
                    for part_segment in segments[:-1]:
                        register_part(part_segment)
            key = (owner, source, target)
            if key in connection_keys:
                continue
            connection_keys.add(key)
            connections_by_owner.setdefault(owner, []).append({"source": source, "target": target})

    if parsed_count == 0:
        return None

    if not contain_edges and not ports_by_owner and not connections_by_owner:
        return None

    children_by_parent: Dict[str, List[str]] = {}
    child_set = set()
    for edge in contain_edges:
        parent = edge["parent"]
        child = edge["child"]
        children_by_parent.setdefault(parent, [])
        if child not in children_by_parent[parent]:
            children_by_parent[parent].append(child)
        child_set.add(child)

    roots = [name for name in part_names if name not in child_set]
    if not roots and part_names:
        roots = [part_names[0]]

    port_defs: List[Dict[str, str]] = []
    seen_port_defs: set[str] = set()
    item_types: List[Dict[str, str]] = []
    seen_item_types: set[str] = set()
    for owner_ports in ports_by_owner.values():
        for port in owner_ports:
            port_def = str(port["port_def"])
            if port_def not in seen_port_defs:
                seen_port_defs.add(port_def)
                stem = re.sub(r"PortDef$", "", port_def)
                item_type = _sanitize_identifier(stem or "DomainDatum", prefix="DomainDatum")
                if item_type == port_def:
                    item_type = _sanitize_identifier(f"{item_type}Datum", prefix="DomainDatum")
                attr_name = _to_lower_camel(item_type, prefix="payload")
                port_defs.append({"name": port_def, "item_type": item_type, "attribute_name": attr_name})
                if item_type not in seen_item_types:
                    seen_item_types.add(item_type)
                    item_types.append(
                        {
                            "name": item_type,
                            "description": f"Payload type inferred from structural port definition {port_def}.",
                        }
                    )

    return {
        "part_names": part_names,
        "root_parts": roots,
        "children_by_parent": children_by_parent,
        "ports_by_owner": ports_by_owner,
        "port_defs": port_defs,
        "item_types": item_types,
        "connections_by_owner": connections_by_owner,
    }


def _build_domain_ir_v2(
    package_name: str,
    statement_path: str,
    translate_payload: Dict[str, Any],
    sat_entry: Dict[str, Any],
    unsat_entry: Dict[str, Any],
) -> Dict[str, Any]:
    tlf_reqs, tlf_traceability, source_sha = _extract_tlf_requirements_context(translate_payload)

    span_by_req: Dict[str, Dict[str, int]] = {}
    for item in tlf_traceability:
        req_id = str(item.get("requirement_id", "")).strip()
        span = item.get("source_span")
        if not req_id or not isinstance(span, dict):
            continue
        span_by_req[req_id] = {
            "line_start": int(span.get("line_start", 0) or 0),
            "line_end": int(span.get("line_end", 0) or 0),
        }

    categories = sorted(
        {
            _normalize_requirement_category(str(req.get("category", "functional")))
            for req in tlf_reqs
        }
    )
    if not categories:
        categories = ["functional"]

    entity_catalog: Dict[str, Dict[str, Any]] = {}
    entity_mentions: Dict[str, int] = {}
    subject_mentions: Dict[str, int] = {}

    action_def_map: Dict[str, Dict[str, Any]] = {}
    actions_by_entity: Dict[str, set[str]] = {}
    quantities_by_entity: Dict[str, Dict[str, Dict[str, Any]]] = {}
    inferred_states: List[str] = []
    requirements: List[Dict[str, Any]] = []
    individual_map: Dict[str, Dict[str, Any]] = {}

    item_type_names: set[str] = {"RequirementDatum"}
    signal_names: set[str] = set()

    for idx, req in enumerate(tlf_reqs, start=1):
        req_id = str(req.get("id", f"R{idx}")).strip() or f"R{idx}"
        req_slug = _sanitize_identifier(req_id, prefix="R")
        category = _normalize_requirement_category(str(req.get("category", "functional")))
        text = str(req.get("text", "")).strip() or f"Requirement {req_id}"
        temporal_kind = str(req.get("temporal_kind", "event_triggered")).strip() or "event_triggered"
        span = span_by_req.get(req_id, {"line_start": idx, "line_end": idx})

        subject_phrase = _extract_subject_phrase(text)
        service_phrases = _extract_service_phrases(text)
        candidate_phrases: List[str] = []
        if subject_phrase:
            candidate_phrases.append(subject_phrase)
        candidate_phrases.extend(service_phrases)

        mapped_entities = _map_requirement_entities(text, candidate_phrases)
        for entity_name in mapped_entities:
            entity_key = _canonical_entity_key(entity_name)
            if entity_key not in entity_catalog:
                source_hint = subject_phrase or (candidate_phrases[0] if candidate_phrases else entity_name)
                entity_catalog[entity_key] = _ontology_entry(entity_name, source_hint)
            entity_mentions[entity_name] = entity_mentions.get(entity_name, 0) + 1
        if subject_phrase:
            for entity_name in _rank_ontology_entities(subject_phrase, [subject_phrase]):
                if entity_name in mapped_entities:
                    subject_mentions[entity_name] = subject_mentions.get(entity_name, 0) + 1

        for state_name in _infer_state_names(text):
            if state_name not in inferred_states:
                inferred_states.append(state_name)

        explicit_names = _extract_explicit_individual_names(text)
        for explicit_name in explicit_names:
            individual_name = _sanitize_identifier(explicit_name, prefix="instance")
            if individual_name in individual_map:
                continue
            individual_map[individual_name] = {
                "name": individual_name,
                "display_name": explicit_name,
                "type": mapped_entities[0],
                "source_requirement_id": req_id,
            }

        action_names: List[str] = []
        for action_phrase in _extract_action_phrases(text):
            action_name = _to_lower_camel(action_phrase, prefix="act")
            if action_name not in action_def_map:
                action_def_map[action_name] = {
                    "name": action_name,
                    "description": f"Derived from requirement verb phrase: {action_phrase}",
                }
            action_names.append(action_name)
            for entity_name in mapped_entities:
                actions_by_entity.setdefault(entity_name, set()).add(action_name)

        quantity_hints = _extract_quantity_hints(text)
        for entity_name in mapped_entities:
            quantity_map = quantities_by_entity.setdefault(entity_name, {})
            for q_idx, hint in enumerate(quantity_hints, start=1):
                attr_name = _sanitize_identifier(
                    f"{req_slug.lower()}_metric_{q_idx}",
                    prefix="metric",
                )
                if attr_name in quantity_map:
                    continue
                quantity_map[attr_name] = {
                    "name": attr_name,
                    "type": hint["type"],
                    "unit": hint["unit"],
                }

        lowered = text.lower()
        if "voice" in lowered:
            item_type_names.add("VoiceCommunicationDatum")
        if "data" in lowered:
            item_type_names.add("DataCommunicationDatum")
        if any(keyword in lowered for keyword in ("call", "message", "broadcast")):
            item_type_names.add("CallMessageDatum")

        requirements.append(
            {
                "id": req_id,
                "sysml_name": f"Req_{req_slug}",
                "category": category,
                "text": text,
                "temporal_kind": temporal_kind,
                "source_span": span,
                "maps_to": mapped_entities,
                "actions": action_names,
                "explicit_individuals": explicit_names,
            }
        )

    if not entity_catalog:
        entity_catalog["coresystem"] = _ontology_entry("CoreSystem", "CoreSystem")
        entity_mentions["CoreSystem"] = 1

    if subject_mentions:
        primary_entity = max(subject_mentions.items(), key=lambda item: item[1])[0]
    else:
        primary_entity = max(entity_mentions.items(), key=lambda item: item[1])[0]

    sorted_entities = sorted(
        {str(item["name"]) for item in entity_catalog.values()},
        key=lambda name: (-entity_mentions.get(name, 0), name.lower()),
    )

    port_defs: List[Dict[str, Any]] = []
    part_types: List[Dict[str, Any]] = []
    for entity_name in sorted_entities:
        in_port_name = f"{entity_name}InPort"
        out_port_name = f"{entity_name}OutPort"
        entity_metadata = entity_catalog.get(entity_name.lower(), {})
        port_defs.append(
            {
                "name": in_port_name,
                "direction": "in",
                "attribute_name": "incomingDatum",
                "item_type": "RequirementDatum",
            }
        )
        port_defs.append(
            {
                "name": out_port_name,
                "direction": "out",
                "attribute_name": "outgoingDatum",
                "item_type": "RequirementDatum",
            }
        )

        quantity_attributes = [
            value for _, value in sorted(quantities_by_entity.get(entity_name, {}).items())
        ]
        part_types.append(
            {
                "name": entity_name,
                "description": str(
                    entity_metadata.get(
                        "description",
                        f"Domain entity inferred from requirement coverage for {entity_name}.",
                    )
                ),
                "in_port_def": in_port_name,
                "out_port_def": out_port_name,
                "actions": sorted(actions_by_entity.get(entity_name, set())),
                "attributes": [
                    {"name": "label", "type": "String", "unit": None},
                    *quantity_attributes,
                ],
            }
        )

    part_instance_by_type: Dict[str, str] = {}
    system_parts: List[Dict[str, Any]] = []
    for entity_name in sorted_entities:
        instance_name = _to_lower_camel(entity_name, prefix="part")
        part_instance_by_type[entity_name] = instance_name
        system_parts.append({"name": instance_name, "type": entity_name})

    connections: List[Dict[str, Any]] = []
    seen_connections: set[Tuple[str, str]] = set()
    for req in requirements:
        mapped = [item for item in req.get("maps_to", []) if isinstance(item, str)]
        if len(mapped) < 2:
            continue
        for map_idx in range(1, len(mapped)):
            src_type = mapped[map_idx - 1]
            dst_type = mapped[map_idx]
            src_part = part_instance_by_type[src_type]
            dst_part = part_instance_by_type[dst_type]
            key = (src_part, dst_part)
            if key in seen_connections:
                continue
            seen_connections.add(key)
            connections.append(
                {
                    "name": f"{src_part}To{dst_part}",
                    "from_part": src_part,
                    "from_port": "output",
                    "to_part": dst_part,
                    "to_port": "input",
                    "from_type": src_type,
                    "to_type": dst_type,
                }
            )

    if not connections and len(system_parts) > 1:
        src_part = part_instance_by_type[primary_entity]
        src_type = primary_entity
        for entity_name in sorted_entities:
            if entity_name == primary_entity:
                continue
            dst_part = part_instance_by_type[entity_name]
            key = (src_part, dst_part)
            if key in seen_connections:
                continue
            seen_connections.add(key)
            connections.append(
                {
                    "name": f"{src_part}To{dst_part}",
                    "from_part": src_part,
                    "from_port": "output",
                    "to_part": dst_part,
                    "to_port": "input",
                    "from_type": src_type,
                    "to_type": entity_name,
                }
            )

    interface_def_map: Dict[str, Dict[str, Any]] = {}
    for connection in connections:
        interface_name = f"{connection['from_type']}To{connection['to_type']}Interface"
        connection["interface_def"] = interface_name
        if interface_name not in interface_def_map:
            interface_def_map[interface_name] = {
                "name": interface_name,
                "end_a_port_def": f"{connection['from_type']}OutPort",
                "end_b_port_def": f"{connection['to_type']}InPort",
            }

    messages: List[Dict[str, Any]] = []
    for req in requirements:
        lowered = str(req.get("text", "")).lower()
        mapped = [item for item in req.get("maps_to", []) if isinstance(item, str)]
        if len(mapped) < 2:
            continue
        if not any(keyword in lowered for keyword in ("communication", "message", "call", "data", "voice", "broadcast")):
            continue
        req_slug = _sanitize_identifier(str(req.get("id", "Req")), prefix="Req")
        signal_name = f"Sig{_to_pascal_identifier(req_slug, prefix='ReqSignal')}"
        signal_names.add(signal_name)
        if "voice" in lowered:
            item_type = "VoiceCommunicationDatum"
        elif "data" in lowered:
            item_type = "DataCommunicationDatum"
        elif any(keyword in lowered for keyword in ("call", "message", "broadcast")):
            item_type = "CallMessageDatum"
        else:
            item_type = "RequirementDatum"
        item_type_names.add(item_type)
        messages.append(
            {
                "name": _to_lower_camel(f"{req_slug}_message", prefix="msg"),
                "signal": signal_name,
                "item_type": item_type,
                "from_part": part_instance_by_type[mapped[0]],
                "to_part": part_instance_by_type[mapped[1]],
            }
        )

    system_name = _to_pascal_identifier(f"{primary_entity} system", prefix="DomainSystem")
    state_machine: Optional[Dict[str, Any]] = None
    if inferred_states:
        normalized_states = list(dict.fromkeys(inferred_states))
        if "Operational" not in normalized_states:
            normalized_states.insert(0, "Operational")
        transitions: List[Dict[str, Any]] = []
        for state_idx in range(1, len(normalized_states)):
            target_state = normalized_states[state_idx]
            trigger = f"SigTo{target_state}"
            signal_names.add(trigger)
            transitions.append(
                {
                    "name": f"to{target_state}",
                    "from": normalized_states[state_idx - 1],
                    "trigger": trigger,
                    "to": target_state,
                }
            )
        state_machine = {
            "name": f"{system_name}States",
            "states": normalized_states,
            "transitions": transitions,
        }

    item_types = [{"name": "RequirementDatum", "description": "Base payload type for requirement-derived exchanges."}]
    for item_name in sorted(item_type_names):
        if item_name == "RequirementDatum":
            continue
        item_types.append(
            {
                "name": item_name,
                "description": f"Specialized payload inferred from requirement language: {item_name}.",
            }
        )

    solver_evidence = _build_solver_evidence(sat_entry, unsat_entry)
    domain_ir: Dict[str, Any] = {
        "schema_version": "2.2",
        "mode": "domain",
        "package_name": package_name,
        "source": {
            "statement_path": statement_path,
            "statement_sha256": source_sha,
        },
        "categories": categories,
        "item_types": item_types,
        "port_defs": port_defs,
        "interface_defs": [interface_def_map[key] for key in sorted(interface_def_map.keys())],
        "action_defs": [action_def_map[key] for key in sorted(action_def_map.keys())],
        "part_types": part_types,
        "signals": sorted(signal_names),
        "system": {
            "name": system_name,
            "parts": system_parts,
            "connections": connections,
            "messages": messages,
            "state_machine": state_machine,
        },
        "individuals": list(individual_map.values()),
        "requirements": requirements,
        "solver_evidence": solver_evidence,
    }
    structural_model = _build_structural_domain_model(requirements)
    if structural_model:
        domain_ir["render_style"] = "structural_curated"
        domain_ir["structural_model"] = structural_model
    return domain_ir


def _validate_domain_ir_v2_schema(domain_ir: Dict[str, Any]) -> None:
    if not isinstance(domain_ir, dict):
        raise RuntimeError(
            f"Domain IR v2 payload must be an object, got {type(domain_ir).__name__}."
        )
    required_top = [
        "schema_version",
        "mode",
        "package_name",
        "source",
        "categories",
        "item_types",
        "port_defs",
        "interface_defs",
        "action_defs",
        "part_types",
        "signals",
        "system",
        "individuals",
        "requirements",
        "solver_evidence",
    ]
    _validate_required_keys(domain_ir, required_top, "Domain IR v2")
    _validate_list_keys(
        domain_ir,
        (
            "categories",
            "item_types",
            "port_defs",
            "interface_defs",
            "action_defs",
            "part_types",
            "signals",
            "individuals",
            "requirements",
        ),
        "Domain IR v2",
    )

    if not domain_ir["categories"]:
        raise RuntimeError("Domain IR v2 must include at least one requirement category.")
    categories = {
        _require_non_empty_string(category, "Domain IR v2 category")
        for category in domain_ir["categories"]
    }

    item_types = _validate_dict_list(domain_ir, "item_types", "Domain IR v2")
    item_type_names = set()
    for item_type in item_types:
        for key in ("name", "description"):
            if key not in item_type:
                raise RuntimeError(f"Item type missing key {key!r}: {item_type!r}")
        item_name = _require_non_empty_string(item_type["name"], "Item type name")
        item_type_names.add(item_name)
    if "RequirementDatum" not in item_type_names:
        raise RuntimeError("Domain IR v2 must include base item type 'RequirementDatum'.")

    port_defs = _validate_dict_list(domain_ir, "port_defs", "Domain IR v2")
    port_def_by_name: Dict[str, Dict[str, Any]] = {}
    for port_def in port_defs:
        for key in ("name", "direction", "attribute_name", "item_type"):
            if key not in port_def:
                raise RuntimeError(f"Port definition missing key {key!r}: {port_def!r}")
        direction = _require_non_empty_string(port_def["direction"], "Port definition direction")
        if direction not in {"in", "out"}:
            raise RuntimeError(f"Port definition direction must be 'in' or 'out': {port_def!r}")
        item_type_name = _require_non_empty_string(port_def["item_type"], "Port definition item_type")
        if item_type_name not in item_type_names:
            raise RuntimeError(
                f"Port definition {port_def['name']!r} references unknown item type {port_def['item_type']!r}."
            )
        port_name = _require_non_empty_string(port_def["name"], "Port definition name")
        port_def_by_name[port_name] = port_def

    action_defs = _validate_dict_list(domain_ir, "action_defs", "Domain IR v2", allow_empty=True)
    action_names = set()
    for action_def in action_defs:
        for key in ("name", "description"):
            if key not in action_def:
                raise RuntimeError(f"Action definition missing key {key!r}: {action_def!r}")
        action_names.add(_require_non_empty_string(action_def["name"], "Action definition name"))

    part_types = _validate_dict_list(domain_ir, "part_types", "Domain IR v2")
    part_type_names = set()
    for part_type in part_types:
        for key in ("name", "description", "in_port_def", "out_port_def", "actions", "attributes"):
            if key not in part_type:
                raise RuntimeError(f"Part type missing key {key!r}: {part_type!r}")
        part_name = _require_non_empty_string(part_type["name"], "Part type name")
        part_type_names.add(part_name)
        in_port_def = _require_non_empty_string(part_type["in_port_def"], f"Part type {part_name!r} in_port_def")
        out_port_def = _require_non_empty_string(part_type["out_port_def"], f"Part type {part_name!r} out_port_def")
        if in_port_def not in port_def_by_name or out_port_def not in port_def_by_name:
            raise RuntimeError(
                f"Part type {part_name!r} references unknown port defs: "
                f"{in_port_def!r}, {out_port_def!r}."
            )
        if str(port_def_by_name[in_port_def]["direction"]) != "in":
            raise RuntimeError(f"Part type {part_name!r} in_port_def is not 'in': {in_port_def!r}")
        if str(port_def_by_name[out_port_def]["direction"]) != "out":
            raise RuntimeError(f"Part type {part_name!r} out_port_def is not 'out': {out_port_def!r}")
        actions = part_type.get("actions")
        if not isinstance(actions, list):
            raise RuntimeError(f"Part type actions must be a list: {part_type!r}")
        for action_index, action_name in enumerate(actions):
            normalized_action = _require_non_empty_string(
                action_name,
                f"Part type {part_name!r} action at index {action_index}",
            )
            if normalized_action not in action_names:
                raise RuntimeError(
                    f"Part type {part_name!r} references unknown action {normalized_action!r}."
                )
        attributes = part_type.get("attributes")
        if not isinstance(attributes, list) or not attributes:
            raise RuntimeError(f"Part type attributes must be a non-empty list: {part_type!r}")
        for attribute_index, attribute in enumerate(attributes):
            if not isinstance(attribute, dict):
                raise RuntimeError(f"Part type attribute entries must be objects: {attribute!r}")
            for key in ("name", "type", "unit"):
                if key not in attribute:
                    raise RuntimeError(f"Part type attribute missing key {key!r}: {attribute!r}")
            _require_non_empty_string(
                attribute["name"],
                f"Part type {part_name!r} attribute[{attribute_index}] name",
            )
            _require_non_empty_string(
                attribute["type"],
                f"Part type {part_name!r} attribute[{attribute_index}] type",
            )
            unit_value = attribute["unit"]
            if unit_value is not None and not isinstance(unit_value, str):
                raise RuntimeError(
                    f"Part type {part_name!r} attribute[{attribute_index}] unit "
                    f"must be a string or null, got {type(unit_value).__name__}."
                )

    interface_defs = _validate_dict_list(domain_ir, "interface_defs", "Domain IR v2", allow_empty=True)
    interface_names = set()
    for interface_def in interface_defs:
        for key in ("name", "end_a_port_def", "end_b_port_def"):
            if key not in interface_def:
                raise RuntimeError(f"Interface definition missing key {key!r}: {interface_def!r}")
        interface_name = _require_non_empty_string(interface_def["name"], "Interface definition name")
        end_a_port_def = _require_non_empty_string(interface_def["end_a_port_def"], "Interface end_a_port_def")
        end_b_port_def = _require_non_empty_string(interface_def["end_b_port_def"], "Interface end_b_port_def")
        if end_a_port_def not in port_def_by_name:
            raise RuntimeError(
                f"Interface {interface_name!r} references unknown end_a port def {end_a_port_def!r}."
            )
        if end_b_port_def not in port_def_by_name:
            raise RuntimeError(
                f"Interface {interface_name!r} references unknown end_b port def {end_b_port_def!r}."
            )
        interface_names.add(interface_name)

    signal_values = [_require_non_empty_string(item, "Domain IR v2 signal") for item in domain_ir["signals"]]
    signals = set(signal_values)
    if any(not signal for signal in signals):
        raise RuntimeError("Domain IR v2 signals cannot contain empty values.")

    system = domain_ir["system"]
    if not isinstance(system, dict):
        raise RuntimeError("Domain IR v2 system must be an object.")
    for key in ("name", "parts", "connections", "messages", "state_machine"):
        if key not in system:
            raise RuntimeError(f"Domain IR v2 system missing key {key!r}.")
    system_name = _require_non_empty_string(system["name"], "Domain IR v2 system.name")

    parts = system["parts"]
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("Domain IR v2 system.parts must be a non-empty list.")
    part_names = set()
    for part in parts:
        if not isinstance(part, dict):
            raise RuntimeError(f"System part entries must be objects: {part!r}")
        for key in ("name", "type"):
            if key not in part:
                raise RuntimeError(f"System part missing key {key!r}: {part!r}")
        part_name = _require_non_empty_string(part["name"], "System part name")
        part_type = _require_non_empty_string(part["type"], f"System part {part_name!r} type")
        if part_type not in part_type_names:
            raise RuntimeError(f"System part references unknown type {part_type!r}.")
        part_names.add(part_name)

    connections = system["connections"]
    if not isinstance(connections, list):
        raise RuntimeError("Domain IR v2 system.connections must be a list.")
    for connection in connections:
        if not isinstance(connection, dict):
            raise RuntimeError(f"Connection entries must be objects: {connection!r}")
        for key in ("name", "from_part", "from_port", "to_part", "to_port", "interface_def"):
            if key not in connection:
                raise RuntimeError(f"Connection missing key {key!r}: {connection!r}")
        from_part = _require_non_empty_string(connection["from_part"], "Connection from_part")
        to_part = _require_non_empty_string(connection["to_part"], "Connection to_part")
        interface_def = _require_non_empty_string(connection["interface_def"], "Connection interface_def")
        if from_part not in part_names or to_part not in part_names:
            raise RuntimeError(f"Connection references unknown system parts: {connection!r}")
        if interface_def not in interface_names:
            raise RuntimeError(
                f"Connection references unknown interface definition {interface_def!r}."
            )

    messages = system["messages"]
    if not isinstance(messages, list):
        raise RuntimeError("Domain IR v2 system.messages must be a list.")
    for message in messages:
        if not isinstance(message, dict):
            raise RuntimeError(f"Message entries must be objects: {message!r}")
        for key in ("name", "signal", "item_type", "from_part", "to_part"):
            if key not in message:
                raise RuntimeError(f"Message missing key {key!r}: {message!r}")
        from_part = _require_non_empty_string(message["from_part"], "Message from_part")
        to_part = _require_non_empty_string(message["to_part"], "Message to_part")
        signal_name = _require_non_empty_string(message["signal"], "Message signal")
        item_type_name = _require_non_empty_string(message["item_type"], "Message item_type")
        if from_part not in part_names or to_part not in part_names:
            raise RuntimeError(f"Message references unknown system parts: {message!r}")
        if item_type_name not in item_type_names:
            raise RuntimeError(f"Message references unknown item type: {message!r}")
        if signal_name not in signals:
            raise RuntimeError(f"Message references unknown signal {signal_name!r}.")

    state_machine = system["state_machine"]
    if state_machine is not None:
        if not isinstance(state_machine, dict):
            raise RuntimeError("Domain IR v2 system.state_machine must be an object or null.")
        for key in ("name", "states", "transitions"):
            if key not in state_machine:
                raise RuntimeError(f"State machine missing key {key!r}: {state_machine!r}")
        states = state_machine["states"]
        transitions = state_machine["transitions"]
        if not isinstance(states, list) or not states:
            raise RuntimeError("State machine states must be a non-empty list.")
        if not isinstance(transitions, list):
            raise RuntimeError("State machine transitions must be a list.")
        state_names = {
            _require_non_empty_string(state, f"State machine state[{index}]")
            for index, state in enumerate(states)
        }
        for transition in transitions:
            if not isinstance(transition, dict):
                raise RuntimeError(f"State machine transition entries must be objects: {transition!r}")
            for key in ("name", "from", "trigger", "to"):
                if key not in transition:
                    raise RuntimeError(f"State machine transition missing key {key!r}: {transition!r}")
            from_state = _require_non_empty_string(transition["from"], "State machine transition from")
            to_state = _require_non_empty_string(transition["to"], "State machine transition to")
            trigger = _require_non_empty_string(transition["trigger"], "State machine transition trigger")
            if from_state not in state_names or to_state not in state_names:
                raise RuntimeError(f"State machine transition references unknown states: {transition!r}")
            if trigger not in signals:
                raise RuntimeError(f"State machine transition references unknown trigger: {transition!r}")

    requirements = _validate_dict_list(domain_ir, "requirements", "Domain IR v2")
    requirement_ids = [_require_non_empty_string(req.get("id"), "Requirement id") for req in requirements]
    if len(requirement_ids) != len(set(requirement_ids)):
        raise RuntimeError("Domain IR v2 requirement IDs must be unique.")
    known_categories = categories
    for req in requirements:
        for key in ("id", "category", "text", "temporal_kind", "maps_to", "actions", "explicit_individuals"):
            if key not in req:
                raise RuntimeError(f"Requirement entry missing key {key!r}: {req!r}")
        req_id = _require_non_empty_string(req["id"], "Requirement id")
        category = _require_non_empty_string(req["category"], f"Requirement {req_id!r} category")
        if category not in known_categories:
            raise RuntimeError(f"Requirement {req['id']!r} has unknown category {req['category']!r}.")
        maps_to = req.get("maps_to")
        if not isinstance(maps_to, list) or not maps_to:
            raise RuntimeError(f"Requirement {req['id']!r} must map to at least one domain part type.")
        for mapped_type in maps_to:
            mapped_type_name = _require_non_empty_string(
                mapped_type,
                f"Requirement {req_id!r} maps_to entry",
            )
            if mapped_type_name not in part_type_names:
                raise RuntimeError(
                    f"Requirement {req['id']!r} maps to unknown part type {mapped_type!r}."
                )
        if not isinstance(req.get("actions"), list):
            raise RuntimeError(f"Requirement {req_id!r} actions must be a list.")
        if not isinstance(req.get("explicit_individuals"), list):
            raise RuntimeError(f"Requirement {req_id!r} explicit_individuals must be a list.")

    individuals = _validate_dict_list(domain_ir, "individuals", "Domain IR v2", allow_empty=True)
    for individual in individuals:
        for key in ("name", "display_name", "type", "source_requirement_id"):
            if key not in individual:
                raise RuntimeError(f"Individual missing key {key!r}: {individual!r}")
        individual_type = _require_non_empty_string(individual["type"], "Individual type")
        if individual_type not in part_type_names:
            raise RuntimeError(
                f"Individual {individual['name']!r} references unknown type {individual['type']!r}."
            )


def _validate_domain_ir_v2_coverage(
    domain_ir: Dict[str, Any],
    translate_payload: Dict[str, Any],
) -> None:
    produced = [
        str(item.get("id", "")).strip()
        for item in domain_ir.get("requirements", [])
        if isinstance(item, dict)
    ]
    _validate_requirement_coverage(produced, translate_payload, "Domain")


def _build_traceability_ir(
    trace_package_name: str,
    domain_ir: Dict[str, Any],
) -> Dict[str, Any]:
    requirements = [item for item in domain_ir.get("requirements", []) if isinstance(item, dict)]
    system_parts = [item for item in domain_ir.get("system", {}).get("parts", []) if isinstance(item, dict)]
    part_names_by_type: Dict[str, List[str]] = {}
    for part in system_parts:
        part_names_by_type.setdefault(str(part.get("type", "")), []).append(str(part.get("name", "")))

    rows: List[Dict[str, Any]] = []
    for req in requirements:
        row_name = f"trace_{_sanitize_identifier(str(req['id']), prefix='R')}"
        mapped_part_types = [str(item) for item in req.get("maps_to", [])]
        mapped_system_parts: List[str] = []
        for mapped_type in mapped_part_types:
            mapped_system_parts.extend(part_names_by_type.get(mapped_type, []))
        rows.append(
            {
                "row_name": row_name,
                "requirement_id": str(req["id"]),
                "sysml_requirement_name": str(req.get("sysml_name", "")),
                "requirement_text": str(req.get("text", "")),
                "mapped_part_types": mapped_part_types,
                "mapped_system_parts": mapped_system_parts,
                "explicit_individuals": [str(item) for item in req.get("explicit_individuals", [])],
                "source_line_start": int(req.get("source_span", {}).get("line_start", 0) or 0),
                "source_line_end": int(req.get("source_span", {}).get("line_end", 0) or 0),
                "category": str(req.get("category", "functional")),
                "temporal_kind": str(req.get("temporal_kind", "event_triggered")),
            }
        )

    solver = domain_ir.get("solver_evidence", {})
    return {
        "schema_version": "1.0",
        "mode": "traceability",
        "package_name": trace_package_name,
        "domain_package_name": str(domain_ir.get("package_name", "DomainModel")),
        "source": domain_ir.get("source", {}),
        "solver_evidence": {
            "sat_path": str(solver.get("sat_path", "")),
            "unsat_path": str(solver.get("unsat_path", "")),
            "sat_status": str(solver.get("sat_status", "n/a")),
            "unsat_status": str(solver.get("unsat_status", "n/a")),
        },
        "rows": rows,
        "summary": {
            "total_requirements": len(rows),
            "covered_requirements": len(rows),
        },
    }


def _validate_traceability_ir(trace_ir: Dict[str, Any], domain_ir: Dict[str, Any]) -> None:
    required_top = {
        "schema_version",
        "mode",
        "package_name",
        "domain_package_name",
        "rows",
        "summary",
        "solver_evidence",
    }
    missing = sorted(required_top - set(trace_ir.keys()))
    if missing:
        raise RuntimeError(f"Traceability IR missing required keys: {missing}")

    rows = trace_ir.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError("Traceability IR rows must be a list.")
    if not rows:
        raise RuntimeError("Traceability IR rows cannot be empty.")

    requirement_ids = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Traceability rows must be objects.")
        for key in ("row_name", "requirement_id", "mapped_part_types", "mapped_system_parts"):
            if key not in row:
                raise RuntimeError(f"Traceability row missing key {key!r}: {row!r}")
        mapped_types = row.get("mapped_part_types")
        if not isinstance(mapped_types, list) or not mapped_types:
            raise RuntimeError(f"Traceability row must include non-empty mapped_part_types: {row!r}")
        mapped_parts = row.get("mapped_system_parts")
        if not isinstance(mapped_parts, list) or not mapped_parts:
            raise RuntimeError(f"Traceability row must include non-empty mapped_system_parts: {row!r}")
        requirement_ids.append(str(row["requirement_id"]))

    if len(requirement_ids) != len(set(requirement_ids)):
        raise RuntimeError("Traceability IR requirement IDs must be unique.")

    expected = {
        str(req.get("id", ""))
        for req in domain_ir.get("requirements", [])
        if isinstance(req, dict)
    }
    expected.discard("")
    produced = set(requirement_ids)
    missing_ids = sorted(expected - produced)
    if missing_ids:
        raise RuntimeError(f"Traceability IR missing requirement mappings: {missing_ids}")

    summary = trace_ir.get("summary")
    if not isinstance(summary, dict):
        raise RuntimeError("Traceability IR summary must be an object.")
    total = int(summary.get("total_requirements", 0) or 0)
    covered = int(summary.get("covered_requirements", 0) or 0)
    if total != len(rows) or covered != len(rows):
        raise RuntimeError(
            "Traceability IR summary counts do not match row cardinality."
        )


def _build_sysml_domain_content_structural(
    package_name: str,
    domain_ir: Dict[str, Any],
    context_snippet: Optional[str],
) -> str:
    source = domain_ir.get("source", {})
    structural = domain_ir.get("structural_model", {})
    part_names = [str(item) for item in structural.get("part_names", []) if str(item).strip()]
    root_parts = [str(item) for item in structural.get("root_parts", []) if str(item).strip()]
    children_by_parent = {
        str(key): [str(item) for item in value]
        for key, value in (structural.get("children_by_parent", {}) or {}).items()
        if isinstance(value, list)
    }
    ports_by_owner = {
        str(key): [item for item in value if isinstance(item, dict)]
        for key, value in (structural.get("ports_by_owner", {}) or {}).items()
        if isinstance(value, list)
    }
    connections_by_owner = {
        str(key): [item for item in value if isinstance(item, dict)]
        for key, value in (structural.get("connections_by_owner", {}) or {}).items()
        if isinstance(value, list)
    }
    item_types = [item for item in structural.get("item_types", []) if isinstance(item, dict)]
    port_defs = [item for item in structural.get("port_defs", []) if isinstance(item, dict)]

    package_doc_lines = [
        "Auto-generated domain-first SysML package.",
        f"Source requirements file: {source.get('statement_path', 'N/A')}",
        f"Source statement digest: {source.get('statement_sha256', 'N/A')}",
        "Structural rendering mode inferred from containment/connectivity requirement statements.",
        "Requirement traceability and solver evidence are emitted in a separate trace package.",
    ]
    if context_snippet:
        package_doc_lines.append("")
        package_doc_lines.append("Reference modelling snippet that guided generation:")
        package_doc_lines.extend(context_snippet.splitlines())
    package_doc = _doc_block(package_doc_lines)

    lines: List[str] = []
    lines.append(f"package {package_name} {{")
    lines.append("\tprivate import ScalarValues::*;")
    lines.append("")
    lines.append(f"\tdoc {package_doc}")
    lines.append("")

    for item_type in item_types:
        item_name = _sanitize_identifier(str(item_type.get("name", "DomainDatum")), prefix="DomainDatum")
        lines.append(f"\titem def {item_name};")
    if item_types:
        lines.append("")

    for port_def in port_defs:
        port_name = _sanitize_identifier(str(port_def.get("name", "PortDef")), prefix="PortDef")
        item_type = _sanitize_identifier(str(port_def.get("item_type", "DomainDatum")), prefix="DomainDatum")
        attr_name = _sanitize_identifier(str(port_def.get("attribute_name", "payload")), prefix="payload")
        lines.append(f"\tport def {port_name} {{")
        lines.append(f"\t\tout item {attr_name} : {item_type};")
        lines.append("\t}")
    if port_defs:
        lines.append("")

    for part_name_raw in part_names:
        part_name = _sanitize_identifier(part_name_raw, prefix="DomainPart")
        lines.append(f"\tpart def {part_name} {{")
        owner_ports = ports_by_owner.get(part_name_raw, [])
        for port in owner_ports:
            port_name = _sanitize_identifier(str(port.get("name", "port")), prefix="port")
            direction = str(port.get("direction", "in"))
            port_def = _sanitize_identifier(str(port.get("port_def", "PortDef")), prefix="PortDef")
            if direction == "in":
                lines.append(f"\t\tin port {port_name} : ~{port_def};")
            else:
                lines.append(f"\t\tout port {port_name} : {port_def};")

        for child_name_raw in children_by_parent.get(part_name_raw, []):
            child_name = _sanitize_identifier(child_name_raw, prefix="DomainPart")
            lines.append(f"\t\tpart {child_name} : {child_name};")

        for connection in connections_by_owner.get(part_name_raw, []):
            source = _sanitize_path_expression(str(connection.get("source", "")))
            target = _sanitize_path_expression(str(connection.get("target", "")))
            if source and target:
                lines.append(f"\t\tconnect {source} to {target};")

        lines.append("\t}")
        lines.append("")

    lines.append("\tpart generatedDomainSystem {")
    roots = root_parts or part_names[:1]
    for root_name_raw in roots:
        root_name = _sanitize_identifier(root_name_raw, prefix="DomainPart")
        lines.append(f"\t\tpart {root_name} : {root_name};")
    lines.append("\t}")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def attach_review_contracts(sysml_text: str, contract_bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the deterministic shared-contract projection to a domain candidate.

    The appended package exposes a finite constraint representation. It does not
    assert that heuristically generated domain components implement that behavior.
    """
    from review_contract_sysml import render_contract_sysml
    projection = render_contract_sysml(contract_bundle)
    return {**projection, "text": sysml_text.rstrip() + "\n\n" + projection["text"],
            "scope": "Shared contract projection; allocation to domain behavior remains an engineering decision."}


def _build_sysml_domain_content(
    package_name: str,
    domain_ir: Dict[str, Any],
    context_snippet: Optional[str],
) -> str:
    if str(domain_ir.get("render_style", "")).strip().lower() == "structural_curated":
        structural_model = domain_ir.get("structural_model")
        if isinstance(structural_model, dict) and structural_model:
            content = _build_sysml_domain_content_structural(package_name, domain_ir, context_snippet)
            return attach_review_contracts(content, domain_ir["review_contracts"])["text"] if domain_ir.get("review_contracts") else content

    source = domain_ir.get("source", {})
    item_types = [item for item in domain_ir.get("item_types", []) if isinstance(item, dict)]
    port_defs = [item for item in domain_ir.get("port_defs", []) if isinstance(item, dict)]
    interface_defs = [item for item in domain_ir.get("interface_defs", []) if isinstance(item, dict)]
    action_defs = [item for item in domain_ir.get("action_defs", []) if isinstance(item, dict)]
    part_types = [item for item in domain_ir.get("part_types", []) if isinstance(item, dict)]
    signals = [str(item) for item in domain_ir.get("signals", [])]
    system = domain_ir.get("system", {})
    system_name = _sanitize_identifier(str(system.get("name", "DomainSystem")), prefix="DomainSystem")
    system_parts = [item for item in system.get("parts", []) if isinstance(item, dict)]
    connections = [item for item in system.get("connections", []) if isinstance(item, dict)]
    messages = [item for item in system.get("messages", []) if isinstance(item, dict)]
    state_machine = system.get("state_machine")
    individuals = [item for item in domain_ir.get("individuals", []) if isinstance(item, dict)]

    package_doc_lines = [
        "Auto-generated domain-first SysML package.",
        f"Source requirements file: {source.get('statement_path', 'N/A')}",
        f"Source statement digest: {source.get('statement_sha256', 'N/A')}",
        "This package models domain structure/behavior only.",
        "Requirement traceability and solver evidence are emitted in a separate trace package.",
    ]
    if context_snippet:
        package_doc_lines.append("")
        package_doc_lines.append("Reference modelling snippet that guided generation:")
        package_doc_lines.extend(context_snippet.splitlines())
    package_doc = _doc_block(package_doc_lines)

    lines: List[str] = []
    lines.append(f"package {package_name} {{")
    lines.append("\tprivate import ScalarValues::*;")
    lines.append("")
    lines.append(f"\tdoc {package_doc}")
    lines.append("")
    lines.append("\tpackage Definitions {")
    lines.append("\t\tprivate import ScalarValues::*;")
    lines.append("")
    for signal in signals:
        lines.append(f"\t\tattribute def {signal};")
    if signals:
        lines.append("")

    for item_type in item_types:
        item_name = _sanitize_identifier(str(item_type.get("name", "RequirementDatum")), prefix="ItemType")
        if item_name == "RequirementDatum":
            lines.append("\t\titem def RequirementDatum;")
        else:
            lines.append(f"\t\titem def {item_name} :> RequirementDatum;")
    lines.append("")

    for port_def in port_defs:
        port_name = _sanitize_identifier(str(port_def.get("name", "PortDef")), prefix="PortDef")
        direction = str(port_def.get("direction", "in"))
        attr_name = _sanitize_identifier(str(port_def.get("attribute_name", "payload")), prefix="payload")
        item_type = _sanitize_identifier(str(port_def.get("item_type", "RequirementDatum")), prefix="RequirementDatum")
        lines.append(f"\t\tport def {port_name} {{")
        lines.append(f"\t\t\t{direction} item {attr_name} : {item_type};")
        lines.append("\t\t}")
    if port_defs:
        lines.append("")

    for interface_def in interface_defs:
        interface_name = _sanitize_identifier(str(interface_def.get("name", "Interface")), prefix="Interface")
        end_a = _sanitize_identifier(str(interface_def.get("end_a_port_def", "PortA")), prefix="PortA")
        end_b = _sanitize_identifier(str(interface_def.get("end_b_port_def", "PortB")), prefix="PortB")
        lines.append(f"\t\tinterface def {interface_name} {{")
        lines.append(f"\t\t\tend source : {end_a};")
        lines.append(f"\t\t\tend target : ~{end_b};")
        lines.append("\t\t}")
    if interface_defs:
        lines.append("")

    for action_def in action_defs:
        action_name = _sanitize_identifier(str(action_def.get("name", "act")), prefix="act")
        lines.append(f"\t\taction def {action_name};")
    if action_defs:
        lines.append("")

    for part_type in part_types:
        part_name = _sanitize_identifier(str(part_type.get("name", "DomainEntity")), prefix="DomainEntity")
        part_doc = str(part_type.get("description", "Domain entity derived from requirement content."))
        in_port_def = _sanitize_identifier(str(part_type.get("in_port_def", "InPort")), prefix="InPort")
        out_port_def = _sanitize_identifier(str(part_type.get("out_port_def", "OutPort")), prefix="OutPort")
        lines.append(f"\t\tpart def {part_name} {{")
        lines.append(f"\t\t\tdoc {_doc_block([part_doc])}")
        attributes = [item for item in part_type.get("attributes", []) if isinstance(item, dict)]
        for attribute in attributes:
            attr_name = _sanitize_identifier(str(attribute.get("name", "attr")), prefix="attr")
            scalar_type = _scalar_type_name(str(attribute.get("type", "String")))
            lines.append(f"\t\t\tattribute {attr_name} : {scalar_type};")
        lines.append(f"\t\t\tport input : {in_port_def};")
        lines.append(f"\t\t\tport output : {out_port_def};")
        for action_name_raw in part_type.get("actions", []):
            action_name = _sanitize_identifier(str(action_name_raw), prefix="act")
            lines.append(f"\t\t\tperform action {action_name};")
        lines.append("\t\t}")
        lines.append("")

    if isinstance(state_machine, dict):
        state_name = _sanitize_identifier(str(state_machine.get("name", "DomainStates")), prefix="DomainStates")
        states = [str(item) for item in state_machine.get("states", [])]
        transitions = [item for item in state_machine.get("transitions", []) if isinstance(item, dict)]
        if states:
            lines.append(f"\t\tstate def {state_name} {{")
            lines.append(f"\t\t\tentry; then {states[0]};")
            for state in states:
                lines.append(f"\t\t\tstate {state};")
            for transition in transitions:
                lines.append(f"\t\t\ttransition {transition['name']}")
                lines.append(f"\t\t\t\tfirst {transition['from']}")
                lines.append(f"\t\t\t\taccept {transition['trigger']}")
                lines.append(f"\t\t\t\tthen {transition['to']};")
            lines.append("\t\t}")
            lines.append("")

    lines.append("\t}")
    lines.append("")

    lines.append("\tpackage Architecture {")
    lines.append("\t\tprivate import Definitions::*;")
    lines.append("")
    lines.append(f"\t\tpart def {system_name} {{")
    lines.append("\t\t\tdoc /* Top-level domain architecture inferred from requirement statements. */")
    for part in system_parts:
        part_name = _sanitize_identifier(str(part.get("name", "part")), prefix="part")
        part_type = _sanitize_identifier(str(part.get("type", "DomainEntity")), prefix="DomainEntity")
        lines.append(f"\t\t\tpart {part_name} : {part_type};")
    if system_parts:
        lines.append("")
    for connection in connections:
        lines.append(
            f"\t\t\tconnect {connection['from_part']}.{connection['from_port']} "
            f"to {connection['to_part']}.{connection['to_port']};"
        )
    if connections:
        lines.append("")
    for message in messages:
        lines.append(
            f"\t\t\tmessage {message['name']} of {message['signal']} : {message['item_type']} "
            f"from {message['from_part']} to {message['to_part']};"
        )
    if isinstance(state_machine, dict) and state_machine.get("states"):
        state_name = _sanitize_identifier(str(state_machine.get("name", "DomainStates")), prefix="DomainStates")
        lines.append("")
        lines.append(f"\t\t\texhibit state lifecycleStates : {state_name};")
    lines.append("\t\t}")
    lines.append("\t}")
    lines.append("")

    if individuals:
        lines.append("\tpackage Instances {")
        lines.append("\t\tprivate import Definitions::*;")
        lines.append("")
        for individual in individuals:
            individual_name = _sanitize_identifier(str(individual.get("name", "instance")), prefix="instance")
            individual_type = _sanitize_identifier(str(individual.get("type", "DomainEntity")), prefix="DomainEntity")
            display_name = _string_literal(str(individual.get("display_name", individual_name)))
            lines.append(f"\t\tindividual part def {individual_name} :> {individual_type} {{")
            lines.append(f"\t\t\t:>> label = \"{display_name}\";")
            lines.append("\t\t}")
            lines.append("")
        lines.append("\t}")
        lines.append("")

    lines.append(f"\tpart generatedDomainSystem : Architecture::{system_name};")
    lines.append("}")
    lines.append("")
    content = "\n".join(lines)
    return attach_review_contracts(content, domain_ir["review_contracts"])["text"] if domain_ir.get("review_contracts") else content


def _validate_domain_sysml_output(sysml_text: str, domain_ir: Dict[str, Any]) -> None:
    style = str(domain_ir.get("render_style", "")).strip().lower()
    if style == "structural_curated":
        required_markers = ["part generatedDomainSystem"]
        structural = domain_ir.get("structural_model", {})
        if isinstance(structural, dict):
            for part_name in structural.get("part_names", []):
                marker = f"part def {_sanitize_identifier(str(part_name), prefix='DomainPart')}"
                required_markers.append(marker)
    else:
        system = domain_ir.get("system", {})
        system_name = _sanitize_identifier(str(system.get("name", "DomainSystem")), prefix="DomainSystem")
        required_markers = [
            "package Definitions {",
            "package Architecture {",
            f"part def {system_name}",
            "part generatedDomainSystem",
        ]
        if system.get("connections"):
            required_markers.append("connect ")

    missing = [marker for marker in required_markers if marker not in sysml_text]
    if missing:
        raise RuntimeError(f"Domain SysML validation failed; missing markers: {missing}")

    forbidden_markers = [
        "TraceRow",
        "satArtifactPath",
        "unsatArtifactPath",
        "requirementText",
        "SolverEvidence",
        "AutoGeneratedRequirement",
    ]
    present_forbidden = [marker for marker in forbidden_markers if marker in sysml_text]
    if present_forbidden:
        raise RuntimeError(
            "Domain SysML includes trace/evidence markers that must stay outside domain mode: "
            f"{present_forbidden}"
        )

    if style == "structural_curated":
        structural = domain_ir.get("structural_model", {})
        if isinstance(structural, dict):
            for part_name in structural.get("part_names", []):
                marker = f"part def {_sanitize_identifier(str(part_name), prefix='DomainPart')}"
                if marker not in sysml_text:
                    raise RuntimeError(f"Domain SysML missing structural part definition: {marker}")
        if "part def DomainOverview" in sysml_text or "generatedDomainOverview" in sysml_text:
            raise RuntimeError("Domain SysML should not emit DomainOverview in structural mode.")
    else:
        for part_type in domain_ir.get("part_types", []):
            if not isinstance(part_type, dict):
                continue
            part_name = _sanitize_identifier(str(part_type.get("name", "")), prefix="DomainEntity")
            if part_name and f"part def {part_name}" not in sysml_text:
                raise RuntimeError(f"Domain SysML missing domain part definition: {part_name}")

        if domain_ir.get("individuals"):
            if "package Instances {" not in sysml_text:
                raise RuntimeError("Domain SysML expected package Instances for explicit individuals.")
        else:
            if "package Instances {" in sysml_text:
                raise RuntimeError("Domain SysML unexpectedly emitted package Instances with no individuals.")


def _build_sysml_traceability_content(
    package_name: str,
    trace_ir: Dict[str, Any],
    context_snippet: Optional[str],
) -> str:
    rows = [item for item in trace_ir.get("rows", []) if isinstance(item, dict)]
    domain_package_name = _sanitize_identifier(str(trace_ir.get("domain_package_name", "DomainModel")), prefix="DomainModel")
    source = trace_ir.get("source", {})
    solver = trace_ir.get("solver_evidence", {})
    summary = trace_ir.get("summary", {})
    total = int(summary.get("total_requirements", len(rows)) or len(rows))
    covered = int(summary.get("covered_requirements", len(rows)) or len(rows))

    doc_lines = [
        "Separate traceability module generated from Domain IR.",
        f"Domain package target: {domain_package_name}",
        f"Source requirements file: {source.get('statement_path', 'N/A')}",
        f"SAT evidence: {solver.get('sat_path', 'N/A')}",
        f"UNSAT evidence: {solver.get('unsat_path', 'N/A')}",
    ]
    if context_snippet:
        doc_lines.append("")
        doc_lines.append("Reference modelling snippet that guided generation:")
        doc_lines.extend(context_snippet.splitlines())
    package_doc = _doc_block(doc_lines)

    sat_path = _string_literal(str(solver.get("sat_path", "")))
    unsat_path = _string_literal(str(solver.get("unsat_path", "")))
    sat_status = _string_literal(str(solver.get("sat_status", "n/a")))
    unsat_status = _string_literal(str(solver.get("unsat_status", "n/a")))

    lines: List[str] = []
    lines.append(f"package {package_name} {{")
    lines.append("\tprivate import ScalarValues::*;")
    lines.append(f"\tprivate import {domain_package_name}::*;")
    lines.append("")
    lines.append(f"\tdoc {package_doc}")
    lines.append("")
    lines.append("\tpart def TraceRow {")
    lines.append("\t\tattribute requirementId : ScalarValues::String;")
    lines.append("\t\tattribute requirementText : ScalarValues::String;")
    lines.append("\t\tattribute mappedPartTypes : ScalarValues::String;")
    lines.append("\t\tattribute mappedSystemParts : ScalarValues::String;")
    lines.append("\t\tattribute mappedIndividuals : ScalarValues::String;")
    lines.append("\t\tattribute sourceLineStart : ScalarValues::Integer;")
    lines.append("\t\tattribute sourceLineEnd : ScalarValues::Integer;")
    lines.append("\t\tattribute category : ScalarValues::String;")
    lines.append("\t\tattribute temporalKind : ScalarValues::String;")
    lines.append("\t\tattribute satArtifactPath : ScalarValues::String;")
    lines.append("\t\tattribute unsatArtifactPath : ScalarValues::String;")
    lines.append("\t\tattribute satStatus : ScalarValues::String;")
    lines.append("\t\tattribute unsatStatus : ScalarValues::String;")
    lines.append("\t}")
    lines.append("")

    for row in rows:
        row_name = _sanitize_identifier(str(row.get("row_name", "trace_row")), prefix="trace")
        req_id = _string_literal(str(row.get("requirement_id", "")))
        req_text = _string_literal(str(row.get("requirement_text", "")))
        mapped_part_types = _string_literal(",".join(str(item) for item in row.get("mapped_part_types", [])))
        mapped_system_parts = _string_literal(",".join(str(item) for item in row.get("mapped_system_parts", [])))
        mapped_individuals = _string_literal(",".join(str(item) for item in row.get("explicit_individuals", [])))
        line_start = int(row.get("source_line_start", 0) or 0)
        line_end = int(row.get("source_line_end", 0) or 0)
        category = _string_literal(str(row.get("category", "functional")))
        temporal_kind = _string_literal(str(row.get("temporal_kind", "event_triggered")))
        lines.append(f"\tindividual part def {row_name} :> TraceRow {{")
        lines.append(f"\t\t:>> requirementId = \"{req_id}\";")
        lines.append(f"\t\t:>> requirementText = \"{req_text}\";")
        lines.append(f"\t\t:>> mappedPartTypes = \"{mapped_part_types}\";")
        lines.append(f"\t\t:>> mappedSystemParts = \"{mapped_system_parts}\";")
        lines.append(f"\t\t:>> mappedIndividuals = \"{mapped_individuals}\";")
        lines.append(f"\t\t:>> sourceLineStart = {line_start};")
        lines.append(f"\t\t:>> sourceLineEnd = {line_end};")
        lines.append(f"\t\t:>> category = \"{category}\";")
        lines.append(f"\t\t:>> temporalKind = \"{temporal_kind}\";")
        lines.append(f"\t\t:>> satArtifactPath = \"{sat_path}\";")
        lines.append(f"\t\t:>> unsatArtifactPath = \"{unsat_path}\";")
        lines.append(f"\t\t:>> satStatus = \"{sat_status}\";")
        lines.append(f"\t\t:>> unsatStatus = \"{unsat_status}\";")
        lines.append("\t}")
        lines.append("")

    lines.append("\tpart def TraceabilityMatrix {")
    for row in rows:
        row_name = _sanitize_identifier(str(row.get("row_name", "trace_row")), prefix="trace")
        lines.append(f"\t\tpart {row_name}_entry :> {row_name};")
    lines.append(f"\t\tattribute totalRequirements : ScalarValues::Integer = {total};")
    lines.append(f"\t\tattribute coveredRequirements : ScalarValues::Integer = {covered};")
    lines.append("\t\tassert constraint coverageComplete { coveredRequirements == totalRequirements }")
    lines.append("\t}")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def _validate_traceability_sysml_output(sysml_text: str, trace_ir: Dict[str, Any]) -> None:
    required_markers = [
        "part def TraceRow",
        "individual part def",
        "part def TraceabilityMatrix",
        "assert constraint coverageComplete",
        "mappedPartTypes",
        "mappedSystemParts",
    ]
    missing = [marker for marker in required_markers if marker not in sysml_text]
    if missing:
        raise RuntimeError(f"Traceability SysML validation failed; missing markers: {missing}")

    for row in trace_ir.get("rows", []):
        if not isinstance(row, dict):
            continue
        row_name = _sanitize_identifier(str(row.get("row_name", "")), prefix="trace")
        if row_name and f"individual part def {row_name}" not in sysml_text:
            raise RuntimeError(f"Traceability SysML missing row instance: {row_name}")


def _build_sysml_architecture_content(
    package_name: str,
    domain_ir: Dict[str, Any],
    context_snippet: Optional[str],
) -> str:
    source = domain_ir.get("source", {})
    solver_evidence = domain_ir.get("solver_evidence", {})
    source_path = str(source.get("statement_path", "N/A"))
    source_sha = str(source.get("statement_sha256", "N/A"))
    sat_path = str(solver_evidence.get("sat_path", "N/A"))
    unsat_path = str(solver_evidence.get("unsat_path", "N/A"))
    sat_status = str(solver_evidence.get("sat_status", "n/a"))
    unsat_status = str(solver_evidence.get("unsat_status", "n/a"))

    package_doc_lines = [
        "Auto-generated SysML v2 package in architecture mode.",
        f"Source requirements file: {source_path}",
        f"Source statement digest: {source_sha}",
        "This model includes first-class workflow structure and behaviour.",
    ]
    if context_snippet:
        package_doc_lines.append("")
        package_doc_lines.append("Reference modelling snippet that guided generation:")
        package_doc_lines.extend(context_snippet.splitlines())
    package_doc = _doc_block(package_doc_lines)

    components = [item for item in domain_ir.get("components", []) if isinstance(item, dict)]
    ports = [item for item in domain_ir.get("ports", []) if isinstance(item, dict)]
    quantities = [item for item in domain_ir.get("quantities", []) if isinstance(item, dict)]
    constraints = [item for item in domain_ir.get("constraints", []) if isinstance(item, dict)]
    states = [item for item in domain_ir.get("states", []) if isinstance(item, dict)]
    transitions = [item for item in domain_ir.get("transitions", []) if isinstance(item, dict)]
    messages = [item for item in domain_ir.get("messages", []) if isinstance(item, dict)]
    connections = [item for item in domain_ir.get("connections", []) if isinstance(item, dict)]
    requirements = [item for item in domain_ir.get("requirements", []) if isinstance(item, dict)]

    ports_by_owner: Dict[str, List[Dict[str, Any]]] = {}
    for port in ports:
        ports_by_owner.setdefault(str(port["owner"]), []).append(port)

    quantities_by_owner: Dict[str, List[Dict[str, Any]]] = {}
    for quantity in quantities:
        quantities_by_owner.setdefault(str(quantity["owner"]), []).append(quantity)

    constraints_by_owner: Dict[str, List[Dict[str, Any]]] = {}
    for constraint in constraints:
        constraints_by_owner.setdefault(str(constraint["owner"]), []).append(constraint)

    signal_names = set()
    for transition in transitions:
        signal_names.add(str(transition["trigger"]))
    for message in messages:
        signal_names.add(str(message["signal"]))

    lines: List[str] = []
    lines.append(f"package {package_name} {{")
    lines.append("\tprivate import Requirements::*;")
    lines.append("\tprivate import ScalarValues::*;")
    lines.append("\tprivate import AnalysisCases::*;")
    lines.append("")
    lines.append(f"\tdoc {package_doc}")
    lines.append("")

    for signal in sorted(signal_names):
        lines.append(f"\tattribute def {signal};")
    lines.append("")

    for component in components:
        owner_id = str(component["id"])
        owner_name = str(component["name"])
        lines.append(f"\tpart def {owner_name} {{")
        lines.append(
            f"\t\tdoc {_doc_block([str(component.get('description', 'Auto-generated workflow component.'))])}"
        )
        for quantity in quantities_by_owner.get(owner_id, []):
            scalar_type = _scalar_type_name(str(quantity.get("type", "String")))
            quantity_name = _sanitize_identifier(str(quantity.get("name", "quantity")), prefix="q")
            default_value = quantity.get("default")
            if default_value is None:
                lines.append(f"\t\tattribute {quantity_name} : {scalar_type};")
            else:
                lines.append(f"\t\tattribute {quantity_name} : {scalar_type} = {default_value};")

        if owner_id == "controller":
            lines.append("\t\tstate workflowStates {")
            initial_state = str(states[0]["name"]) if states else "idle"
            lines.append(f"\t\t\tentry; then {initial_state};")
            for state in states:
                lines.append(f"\t\t\tstate {state['name']};")
            for transition in transitions:
                lines.append(f"\t\t\ttransition {transition['name']}")
                lines.append(f"\t\t\t\tfirst {transition['from']}")
                lines.append(f"\t\t\t\taccept {transition['trigger']}")
                lines.append(f"\t\t\t\tthen {transition['to']};")
            lines.append("\t\t}")

        for constraint in constraints_by_owner.get(owner_id, []):
            constraint_name = _sanitize_identifier(str(constraint.get("name", "constraint")), prefix="c")
            constraint_expr = str(constraint.get("expr", "true"))
            lines.append(f"\t\tassert constraint {constraint_name} {{")
            lines.append(f"\t\t\t{constraint_expr}")
            lines.append("\t\t}")

        for port in ports_by_owner.get(owner_id, []):
            port_name = _sanitize_identifier(str(port["name"]), prefix="port")
            signal_name = _sanitize_identifier(str(port["signal"]), prefix="signal")
            direction = str(port["direction"])
            scalar_type = _scalar_type_name(str(port.get("data_type", "String")))
            lines.append(f"\t\tport {port_name} {{")
            lines.append(f"\t\t\t{direction} attribute {signal_name} : {scalar_type};")
            lines.append("\t\t}")
        lines.append("\t}")
        lines.append("")

    lines.append("\tpart def RequirementsWorkflowSystem {")
    lines.append("\t\tdoc /* Top-level architecture wiring the generated workflow components. */")
    for component in components:
        instance_name = _sanitize_identifier(str(component["id"]), prefix="component")
        definition_name = _sanitize_identifier(str(component["name"]), prefix="Component")
        lines.append(f"\t\tpart {instance_name} : {definition_name};")
    lines.append("")
    for connection in connections:
        lines.append(f"\t\tconnect {connection['from']} to {connection['to']};")
    lines.append("")
    for message in messages:
        lines.append(
            f"\t\tmessage {message['name']} of {message['signal']} "
            f"from {message['from']} to {message['to']};"
        )
    lines.append("\t}")
    lines.append("")

    lines.append("\tpart def SolverEvidence {")
    lines.append("\t\tdoc /* Solver artefacts backing architecture-level constraints. */")
    lines.append(f"\t\tattribute satArtifactPath : ScalarValues::String = \"{_string_literal(sat_path)}\";")
    lines.append(f"\t\tattribute unsatArtifactPath : ScalarValues::String = \"{_string_literal(unsat_path)}\";")
    lines.append(f"\t\tattribute satStatus : ScalarValues::String = \"{_string_literal(sat_status)}\";")
    lines.append(f"\t\tattribute unsatStatus : ScalarValues::String = \"{_string_literal(unsat_status)}\";")
    lines.append("\t}")
    lines.append("")

    for req in requirements:
        req_id = str(req["id"]).replace("'", "_")
        req_name = _sanitize_identifier(str(req["sysml_name"]), prefix="Req")
        req_text = str(req.get("text", ""))
        status_symbol = _sanitize_identifier(str(req["status_symbol"]), prefix="req")
        req_doc = _doc_block(
            [
                req_text,
                f"Category: {req['category']}",
                f"Temporal kind: {req['temporal_kind']}",
            ]
        )
        lines.append(f"\trequirement <'{req_id}'> {req_name} :> RequirementCheck {{")
        lines.append(f"\t\tdoc {req_doc}")
        lines.append("\t\tsubject workflow : RequirementsWorkflowSystem;")
        lines.append("\t\trequire constraint {")
        lines.append(
            f"\t\t\tworkflow.controller.{status_symbol} >= 0 & workflow.controller.{status_symbol} <= 1"
        )
        lines.append("\t\t}")
        lines.append("\t}")
        lines.append("")

    lines.append("\tanalysis def ArchitectureConsistency :> AnalysisCase {")
    lines.append("\t\tdoc /* Checks architecture-level invariants and solver status consistency. */")
    lines.append("\t\tsubject workflow : RequirementsWorkflowSystem;")
    lines.append("\t\tsubject evidence : SolverEvidence;")
    lines.append("\t\tobjective consistencyChecks {")
    lines.append("\t\t\trequire constraint {")
    lines.append(
        "\t\t\t\tworkflow.controller.retryCount <= workflow.controller.retryBudget & "
        "workflow.controller.humanReviewApproved >= 0 & "
        "workflow.controller.humanReviewApproved <= 1 & "
        "evidence.satStatus == \"sat\" & evidence.unsatStatus == \"unsat\""
    )
    lines.append("\t\t\t}")
    lines.append("\t\t}")
    lines.append("\t}")
    lines.append("")

    lines.append("\tpart def ArchitectureOverview {")
    lines.append("\t\tdoc /* Compile-safe summary wrapper over architecture artefacts. */")
    lines.append("\t\tpart workflow : RequirementsWorkflowSystem;")
    lines.append("\t\tpart evidence : SolverEvidence;")
    lines.append("\t\tpart analysis : ArchitectureConsistency;")
    lines.append("\t}")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def _validate_architecture_sysml_output(sysml_text: str, domain_ir: Dict[str, Any]) -> None:
    required_markers = [
        "part def RequirementsWorkflowSystem",
        "state workflowStates",
        "transition ",
        "connect ",
        "message ",
        "port ",
        "analysis def ArchitectureConsistency",
        "part def ArchitectureOverview",
    ]
    missing = [marker for marker in required_markers if marker not in sysml_text]
    if missing:
        raise RuntimeError(f"Architecture SysML validation failed; missing markers: {missing}")

    for component in domain_ir.get("components", []):
        if not isinstance(component, dict):
            continue
        name = str(component.get("name", ""))
        if name and f"part def {name}" not in sysml_text:
            raise RuntimeError(f"Architecture SysML missing component part definition: {name}")

    for req in domain_ir.get("requirements", []):
        if not isinstance(req, dict):
            continue
        sysml_name = _sanitize_identifier(str(req.get("sysml_name", "")), prefix="Req")
        if sysml_name and sysml_name not in sysml_text:
            raise RuntimeError(f"Architecture SysML missing synthesized requirement: {sysml_name}")


def _write_json_artifact(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _build_sysml_evidence_content(
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
        f"\tprivate import Requirements::*;\n"
        f"\tprivate import ScalarValues::*;\n"
        f"\tprivate import AnalysisCases::*;\n\n"
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
        f"\tpart def EvidenceOverview {{\n"
        f"\t\tdoc {view_doc}\n"
        f"\t\tpart context : RequirementContext;\n"
        f"\t\tpart proof : SolverEvidence;\n"
        f"\t\tpart summary : SmtValidationSummary;\n"
        f"\t}}\n"
        f"}}\n"
    )


def _write_sysml_model(
    output_path: Path,
    translate_payload: Dict[str, Any],
    sat_entry: Dict[str, Any],
    unsat_entry: Dict[str, Any],
    context_snippet: Optional[str],
    sysml_mode: str,
    domain_ir: Optional[Dict[str, Any]] = None,
) -> None:
    package_name = output_path.stem.replace("-", "_")
    if sysml_mode == "architecture":
        if domain_ir is None:
            raise RuntimeError("Architecture mode selected but Domain IR was not provided.")
        model = _build_sysml_architecture_content(
            package_name=package_name,
            domain_ir=domain_ir,
            context_snippet=context_snippet,
        )
        _validate_architecture_sysml_output(model, domain_ir)
    elif sysml_mode == "domain":
        if domain_ir is None:
            raise RuntimeError("Domain mode selected but Domain IR was not provided.")
        model = _build_sysml_domain_content(
            package_name=package_name,
            domain_ir=domain_ir,
            context_snippet=context_snippet,
        )
        _validate_domain_sysml_output(model, domain_ir)
    else:
        model = _build_sysml_evidence_content(
            package_name=package_name,
            statement_path=str(translate_payload.get("statement_path", "") or ""),
            translate_payload=translate_payload,
            sat_entry=sat_entry,
            unsat_entry=unsat_entry,
            context_snippet=context_snippet,
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(model, encoding="utf-8")


def _write_traceability_model(
    output_path: Path,
    trace_ir: Dict[str, Any],
    context_snippet: Optional[str],
) -> None:
    package_name = output_path.stem.replace("-", "_")
    model = _build_sysml_traceability_content(
        package_name=package_name,
        trace_ir=trace_ir,
        context_snippet=context_snippet,
    )
    _validate_traceability_sysml_output(model, trace_ir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(model, encoding="utf-8")


def _compile_with_sysml_kernel(
    jar_path: Path,
    sysml_path: Path,
    timeout: float,
) -> None:
    if not jar_path.exists():
        raise RuntimeError(
            "SysML kernel JAR not found at "
            f"{jar_path}. Set SYSML_KERNEL_JAR or pass --sysml-jar explicitly."
        )
    cmd = [
        "java",
        "-cp",
        str(jar_path),
        "org.omg.sysml.interactive.SysMLInteractive",
    ]
    def _run_payload(payload: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                cmd,
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "SysML kernel validation timed out. "
                "Use --skip-sysml-compile to bypass validation or increase --timeout."
            ) from exc

    def _error_lines(output: str) -> List[str]:
        return [line.strip() for line in output.splitlines() if "ERROR:" in line]

    def _summarize_errors(errors: Sequence[str], output: str) -> str:
        snippets = list(errors[:20])
        if not snippets:
            snippets = output.splitlines()[:40]
        return "\n".join(snippets)

    # Strategy A (legacy): ask the interactive kernel to load by file path.
    payload = f"load {sysml_path}\n{DEFAULT_EXIT_COMMAND}\n"
    completed = _run_payload(payload)
    output = completed.stdout or ""
    if completed.returncode != 0:
        raise RuntimeError(
            f"SysML kernel reported failure (exit={completed.returncode}). Output:\n{output}"
        )
    errors = _error_lines(output)
    if not errors:
        return

    unsupported_load = any("no viable alternative at input" in err for err in errors)
    if unsupported_load:
        raise RuntimeError(
            "SysML kernel does not accept `load <local-path>` in this runtime, so path-based compile "
            "validation is unavailable. This is common with jupyter-sysml-kernel-0.57.x interactive mode. "
            "Use --skip-sysml-compile, or run with a kernel/runtime that supports local file loading."
        )

    raise RuntimeError(
        "SysML kernel reported validation errors during load. "
        "Excerpt:\n"
        f"{_summarize_errors(errors, output)}"
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


def _apply_run_dir(args: argparse.Namespace) -> Optional[Path]:
    if not args.run_dir:
        return None
    label = _safe_run_label_from_statement(args.statement)
    run_dir = _next_available_run_dir(args.run_dir, label)
    run_dir.mkdir(parents=True, exist_ok=False)
    args.output_prefix = run_dir / args.output_prefix.name
    args.sysml_output = run_dir / args.sysml_output.name
    if args.traceability_output:
        args.traceability_output = run_dir / args.traceability_output.name
    return run_dir


async def _run_pipeline(args: argparse.Namespace) -> Dict[str, Any]:
    if not args.statement.exists():
        raise RuntimeError(f"File not found: {args.statement}")
    run_dir = _apply_run_dir(args)
    output_prefix = args.output_prefix
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    result_json_path = output_prefix.with_suffix("") if output_prefix.suffix else output_prefix
    result_json_path = result_json_path.with_name(result_json_path.name + "_translate.json")
    architecture_template: Optional[Dict[str, Any]] = None

    intent_payload_path: Optional[Path] = None
    intent_requirement_set_path: Optional[Path] = None
    translate_input_statement_path: Path = args.statement

    if not args.skip_intent_formalization:
        default_set_id = _sanitize_identifier(args.output_prefix.stem.upper(), prefix="REQSET")
        intent_set_id = args.intent_set_id or default_set_id
        intent_title = args.intent_title or f"{args.statement.stem} Intent Formalization"
        intent_system = args.intent_system or args.sysml_output.stem
        try:
            intent_payload = await run_formalize_intent_flow(
                source_path=args.statement,
                model=args.model,
                set_id=intent_set_id,
                title=intent_title,
                system_name=intent_system,
                provider=args.llm_provider,
            )
            intent_payload_path = output_prefix.with_name(f"{output_prefix.name}_intent.json")
            _write_json_artifact(intent_payload_path, intent_payload)

            requirement_set = intent_payload.get("requirement_set")
            if not isinstance(requirement_set, dict):
                raise RuntimeError("Intent formalization output is missing requirement_set.")
            requirements = requirement_set.get("requirements")
            if not isinstance(requirements, list) or not requirements:
                raise RuntimeError("Intent requirement_set has no requirements to translate.")

            intent_requirement_set_path = output_prefix.with_name(
                f"{output_prefix.name}_intent_requirement_set.json"
            )
            intent_requirement_set_path.write_text(
                json.dumps(requirement_set, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            translate_input_statement_path = intent_requirement_set_path
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, (FileNotFoundError, PermissionError)):
                raise
            if "File not found:" in str(exc):
                raise
            if args.require_intent_formalization:
                raise
            print(
                "Warning: intent formalization failed; continuing with raw statement source. "
                f"Details: {exc}",
                file=sys.stderr,
            )

    translate_payload = await run_translate_flow(
        statement_path=translate_input_statement_path,
        model=args.model,
        smt_prefix=output_prefix,
        unsat_extra=None,
        provider=args.llm_provider,
    )
    translate_payload["statement_path"] = str(args.statement.resolve())
    translate_payload["translate_statement_path"] = str(translate_input_statement_path.resolve())
    translate_payload["intent_applied"] = bool(intent_requirement_set_path is not None)

    with result_json_path.open("w", encoding="utf-8") as handle:
        json.dump(translate_payload, handle, indent=2)

    sat_entry, unsat_entry = _select_smt_entries(translate_payload["smt_files"])
    sat_path = Path(sat_entry["path"])
    unsat_path = Path(unsat_entry["path"])
    semantic_checks_path: Optional[Path] = None
    semantic_checks: Optional[Dict[str, Any]] = None
    domain_ir_path: Optional[Path] = None
    domain_ir: Optional[Dict[str, Any]] = None
    traceability_ir_path: Optional[Path] = None
    traceability_ir: Optional[Dict[str, Any]] = None
    traceability_sysml_path: Optional[Path] = None
    tlf_path: Optional[Path] = None
    tlf_payload_for_write: Optional[Dict[str, Any]] = None
    tlr_file_raw = translate_payload.get("tlr_file")
    if not isinstance(tlr_file_raw, str) or not tlr_file_raw.strip():
        tlr_file_raw = translate_payload.get("tlf_file")
    if isinstance(tlr_file_raw, str) and tlr_file_raw.strip():
        tlf_path = Path(tlr_file_raw)
    else:
        tlf_payload_for_write = translate_payload.get("typed_requirements_form")
        if not isinstance(tlf_payload_for_write, dict):
            tlf_payload_for_write = translate_payload.get("typed_logical_form")
    if isinstance(tlf_payload_for_write, dict):
        tlf_path = output_prefix.with_name(f"{output_prefix.name}_tlr.json")
        tlf_path.write_text(
            json.dumps(tlf_payload_for_write, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    tlf_for_semantic = _resolve_requirements_tlf(translate_payload)
    if tlf_for_semantic is None:
        tlf_for_semantic = {
            "schema_version": "1.0",
            "source": {"statement_path": str(args.statement.resolve())},
            "requirements": [],
            "symbol_table": [],
            "traceability": [],
        }
    requirement_ids = _tlf_requirement_ids(translate_payload)
    sat_fragment_text = sat_path.read_text(encoding="utf-8")
    semantic_checks = run_semantic_checks(
        sat_fragment=sat_fragment_text,
        requirement_ids=requirement_ids,
        tlf_payload=tlf_for_semantic,
    )
    semantic_checks["semantic_strict"] = bool(args.semantic_strict)
    semantic_checks["sat_fragment_path"] = str(sat_path)
    semantic_checks["requirement_ids"] = requirement_ids
    semantic_checks["requirement_count"] = len(requirement_ids)
    semantic_checks_path = output_prefix.with_name(f"{output_prefix.name}_semantic_checks.json")
    _write_json_artifact(semantic_checks_path, semantic_checks)

    translate_payload["semantic_checks"] = semantic_checks
    translate_payload["semantic_ok"] = bool(semantic_checks.get("passed"))
    with result_json_path.open("w", encoding="utf-8") as handle:
        json.dump(translate_payload, handle, indent=2)

    repair_iterations: List[Dict[str, Any]] = []
    repair_diff: List[Dict[str, str]] = []
    dangerous_repair_diff: List[Dict[str, str]] = []
    original_sat_fragment = sat_fragment_text
    if not semantic_checks.get("passed", False) and SMT_MAX_SEMANTIC_REPAIRS > 0:
        repaired_fragment, repaired_results, repair_iterations = await repair_failed_requirements(
            sat_fragment=sat_fragment_text,
            semantic_results=semantic_checks,
            extended_statement=translate_payload.get("informal_statement", ""),
            informal_proof=translate_payload.get("informal_proof", ""),
            requirement_ids=requirement_ids,
            tlf_payload=tlf_for_semantic,
            model=args.model,
            provider=args.llm_provider,
        )
        repair_diff = classify_repair_diff(original_sat_fragment, repaired_fragment, requirement_ids)
        dangerous_repair_diff = [
            entry
            for entry in repair_diff
            if entry.get("classification") in {"weakened", "temporal_shifted"}
        ]
        if repaired_results.get("passed", False):
            sat_path.write_text(repaired_fragment, encoding="utf-8")
            sat_fragment_text = repaired_fragment
            semantic_checks = repaired_results
            semantic_checks["repaired"] = True
            semantic_checks["repair_iterations"] = repair_iterations
            semantic_checks["repair_diff"] = repair_diff
            semantic_checks["dangerous_repair_diff"] = dangerous_repair_diff
            semantic_checks["dangerous_repair_count"] = len(dangerous_repair_diff)
            semantic_checks["approve_weakened"] = bool(args.approve_weakened)
            semantic_checks["blocked_on_weakened"] = False
            semantic_checks_path = output_prefix.with_name(f"{output_prefix.name}_semantic_checks.json")
            _write_json_artifact(semantic_checks_path, semantic_checks)
            translate_payload["semantic_checks"] = semantic_checks
            translate_payload["semantic_ok"] = True
            with result_json_path.open("w", encoding="utf-8") as handle:
                json.dump(translate_payload, handle, indent=2)
        else:
            semantic_checks["repair_iterations"] = repair_iterations
            semantic_checks["repair_exhausted"] = True
            semantic_checks["repair_diff"] = repair_diff
            semantic_checks["dangerous_repair_diff"] = dangerous_repair_diff
            semantic_checks["dangerous_repair_count"] = len(dangerous_repair_diff)
            semantic_checks["approve_weakened"] = bool(args.approve_weakened)
            semantic_checks["blocked_on_weakened"] = False
            _write_json_artifact(semantic_checks_path, semantic_checks)

    if dangerous_repair_diff and not args.approve_weakened:
        semantic_checks["blocked_on_weakened"] = True
        semantic_checks["approve_weakened"] = False
        _write_json_artifact(semantic_checks_path, semantic_checks)
        translate_payload["semantic_checks"] = semantic_checks
        translate_payload["semantic_ok"] = False
        with result_json_path.open("w", encoding="utf-8") as handle:
            json.dump(translate_payload, handle, indent=2)
        raise RuntimeError(
            "Repair introduced weakened or temporally shifted requirement encodings. "
            "Re-run with --approve-weakened to proceed. "
            f"Details: {semantic_checks_path}"
        )

    if not semantic_checks.get("passed", False):
        semantic_msg = (
            f"Semantic checks failed ({_semantic_summary(semantic_checks)}). "
            f"Details: {semantic_checks_path}"
        )
        if args.semantic_strict:
            raise RuntimeError(semantic_msg)
        print(f"Warning: {semantic_msg}", file=sys.stderr)

    if args.sysml_mode == "architecture":
        architecture_template = _load_architecture_template(args.architecture_template)
        domain_ir = _build_domain_ir(
            package_name=args.sysml_output.stem.replace("-", "_"),
            statement_path=str(args.statement.resolve()),
            translate_payload=translate_payload,
            sat_entry=sat_entry,
            unsat_entry=unsat_entry,
            architecture_template=architecture_template,
        )
        _validate_domain_ir_schema(domain_ir)
        _validate_domain_ir_coverage(domain_ir, translate_payload)
        domain_ir_path = output_prefix.with_name(f"{output_prefix.name}_domain_ir.json")
        _write_json_artifact(domain_ir_path, domain_ir)
    elif args.sysml_mode == "domain":
        domain_ir = _build_domain_ir_v2(
            package_name=args.sysml_output.stem.replace("-", "_"),
            statement_path=str(args.statement.resolve()),
            translate_payload=translate_payload,
            sat_entry=sat_entry,
            unsat_entry=unsat_entry,
        )
        _validate_domain_ir_v2_schema(domain_ir)
        _validate_domain_ir_v2_coverage(domain_ir, translate_payload)
        domain_ir_path = output_prefix.with_name(f"{output_prefix.name}_domain_ir.json")
        _write_json_artifact(domain_ir_path, domain_ir)

        traceability_ir = _build_traceability_ir(
            trace_package_name=f"{args.sysml_output.stem}_trace",
            domain_ir=domain_ir,
        )
        _validate_traceability_ir(traceability_ir, domain_ir)
        traceability_ir_path = output_prefix.with_name(f"{output_prefix.name}_traceability_ir.json")
        _write_json_artifact(traceability_ir_path, traceability_ir)

        traceability_sysml_path = args.traceability_output
        if traceability_sysml_path is None:
            traceability_sysml_path = args.sysml_output.with_name(f"{args.sysml_output.stem}_trace.sysml")
    context_snippet = None
    if args.sysml_context:
        context_snippet = args.sysml_context.read_text(encoding="utf-8")
    _write_sysml_model(
        output_path=args.sysml_output,
        translate_payload=translate_payload,
        sat_entry=sat_entry,
        unsat_entry=unsat_entry,
        context_snippet=context_snippet,
        sysml_mode=args.sysml_mode,
        domain_ir=domain_ir,
    )
    if args.sysml_mode == "domain":
        if traceability_ir is None or traceability_sysml_path is None:
            raise RuntimeError("Domain mode expected traceability IR and traceability SysML path.")
        _write_traceability_model(
            output_path=traceability_sysml_path,
            trace_ir=traceability_ir,
            context_snippet=context_snippet,
        )

    compile_warnings: List[str] = []
    if not args.skip_sysml_compile:
        compile_targets = [args.sysml_output]
        if traceability_sysml_path is not None:
            compile_targets.append(traceability_sysml_path)
        for target in compile_targets:
            try:
                _compile_with_sysml_kernel(
                    jar_path=args.sysml_jar,
                    sysml_path=target,
                    timeout=args.timeout,
                )
            except Exception as exc:  # noqa: BLE001
                if args.require_sysml_compile:
                    raise
                warning = f"{target}: {exc}"
                compile_warnings.append(warning)
                print(
                    "Warning: SysML compile step failed but pipeline artifacts were generated. "
                    f"Details: {warning}",
                    file=sys.stderr,
                )

    artefacts = {
        "intent_json": str(intent_payload_path) if intent_payload_path else "not_generated",
        "intent_requirement_set_json": (
            str(intent_requirement_set_path) if intent_requirement_set_path else "not_generated"
        ),
        "translate_statement_source": str(translate_input_statement_path),
        "translate_json": str(result_json_path),
        "tlr_json": str(tlf_path) if tlf_path else "not_generated",
        # Legacy key kept for backwards compatibility with existing consumers.
        "tlf_json": str(tlf_path) if tlf_path else "not_generated",
        "semantic_checks_json": str(semantic_checks_path) if semantic_checks_path else "not_generated",
        "semantic_ok": str(bool(semantic_checks.get("passed"))) if isinstance(semantic_checks, dict) else "n/a",
        "domain_ir_json": str(domain_ir_path) if domain_ir_path else "not_generated",
        "traceability_ir_json": str(traceability_ir_path) if traceability_ir_path else "not_generated",
        "sat_smt": str(sat_path),
        "unsat_smt": str(unsat_path),
        "sysml_model": str(args.sysml_output),
        "traceability_sysml_model": str(traceability_sysml_path) if traceability_sysml_path else "not_generated",
    }
    if compile_warnings:
        artefacts["sysml_compile_warning"] = " | ".join(compile_warnings)
    if run_dir:
        artefacts["run_dir"] = str(run_dir)
    return artefacts


def _delegated_toolkit_commands() -> set[str]:
    return {"translate", "harvest", "formalize_intent"}


def _run_toolkit_subcommand(argv: Sequence[str]) -> None:
    args = parse_args(argv)
    try:
        _validate_csv_only_contract(args)
    except RuntimeError as exc:
        raise SystemExit(f"Error: {exc}") from exc
    provider = _normalise_provider(getattr(args, "provider", DEFAULT_LLM_PROVIDER))
    try:
        if provider == "openai":
            ensure_api_key()
        result = asyncio.run(dispatch(args))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Error: {exc}") from exc

    output_path = getattr(args, "output_json", None)
    if output_path is None and getattr(args, "write_smt_prefix", None) is not None:
        prefix = Path(args.write_smt_prefix)
        base = prefix
        if base.suffix:
            try:
                base = base.with_suffix("")
            except ValueError:
                base = Path(base.parent, base.stem)
        output_path = base.parent / f"{base.name}_translate.json"
    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    else:
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")


def legacy_main(argv: Optional[Sequence[str]] = None) -> None:
    cli_argv = list(argv) if argv is not None else list(sys.argv[1:])
    if cli_argv and cli_argv[0] in _delegated_toolkit_commands():
        _run_toolkit_subcommand(cli_argv)
        return

    args = _parse_args(cli_argv)
    try:
        _validate_csv_only_contract(args)
    except RuntimeError as exc:
        raise SystemExit(f"Error: {exc}") from exc
    try:
        if args.llm_provider == "openai":
            ensure_api_key()
        artefacts = asyncio.run(_run_pipeline(args))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Error: {exc}") from exc
    print("Pipeline completed. Artefacts:")
    for key, value in artefacts.items():
        print(f"  {key}: {value}")


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Run the canonical research CLI; older review and generator modes are explicit."""
    cli_argv = list(argv) if argv is not None else list(sys.argv[1:])
    if cli_argv and cli_argv[0] in _delegated_toolkit_commands():
        _run_toolkit_subcommand(cli_argv)
        return
    if cli_argv and cli_argv[0] == "legacy":
        legacy_main(cli_argv[1:])
        return
    if cli_argv and cli_argv[0] == "review":
        from review_cli import main as review_main
        raise SystemExit(review_main(cli_argv[1:]))
    from canonical_cli import main as canonical_main
    raise SystemExit(canonical_main(cli_argv))


if __name__ == "__main__":
    # Reuse this instance when the shared workflow imports generator helpers.
    sys.modules.setdefault("requirements_pipeline", sys.modules[__name__])
    main()
