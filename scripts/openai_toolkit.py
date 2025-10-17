#!/usr/bin/env python3
"""
Utility CLI for OpenAI-assisted MBSE workflows.

The tool exposes two high-level commands:

  • translate – convert a plain-text requirements into natural language,
    an informal mathematical lemma, and an SMT-LIB skeleton.
  • harvest   – parse a long-form source document into a requirement-set JSON
    envelope that can be fed into the text → TLF → SMT pipeline.

Usage examples:

    OPENAI_API_KEY=sk-... python openai_toolkit.py translate --statement "..."
    OPENAI_API_KEY=sk-... python openai_toolkit.py harvest --source path/to/requirements.txt \\
        --set-id SERC --title "Charging Controller Requirements" --system "Battery Charger"

Set OPENAI_TRANSLATION_MODEL / OPENAI_HARVEST_MODEL to override defaults.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from openai import AsyncOpenAI

DEFAULT_TRANSLATION_MODEL = os.getenv("OPENAI_TRANSLATION_MODEL", "gpt-4o")
DEFAULT_HARVEST_MODEL = os.getenv("OPENAI_HARVEST_MODEL", DEFAULT_TRANSLATION_MODEL)
SMT_MAX_FIX_ATTEMPTS = int(os.getenv("SMT_FIX_ATTEMPTS", "3"))
SMT_SOLVER_TIMEOUT = float(os.getenv("SMT_SOLVER_TIMEOUT", "10"))


def ensure_api_key() -> None:
    """Raise a readable error if the OpenAI key is missing."""
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Export your key before running this script."
        )


def load_text(path: Path) -> str:
    """Read UTF-8 text from disk."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(f"File not found: {path}") from exc


def normalise_whitespace(text: str) -> str:
    """Collapse excessive whitespace to single spaces."""
    return re.sub(r"\s+", " ", text.strip())


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


async def translate_to_natural_language(client: AsyncOpenAI, statement: str, model: str) -> str:
    """Translate a structured statement into descriptive natural language."""
    system_prompt = (
        "You are an expert in translating structured statements into descriptive natural language."
    )
    user_prompt = (
        "You are an expert in analyzing statements.\n"
        "Please read the following text and explain it in plain English:\n\n"
        f"{statement}"
    )

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=1024,
        temperature=0.01,
    )
    return response.choices[0].message.content.strip()


async def generate_informal_statement(client: AsyncOpenAI, natural_language_text: str, model: str) -> str:
    """Convert a plain-English description into a mathematical-style lemma."""
    system_prompt = "You are an expert in mathematical formalisation."
    user_prompt = (
        "Convert the following plain-English description into a concise, mathematically styled statement "
        "or proposition:\n\n"
        f"{natural_language_text}\n\n"
        "Format it as if you're writing a short lemma statement in mathematical language (no proof)."
    )

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=1024,
        temperature=0.01,
    )
    return response.choices[0].message.content.strip()


async def generate_informal_proof(client: AsyncOpenAI, informal_statement: str, model: str) -> str:
    """Produce an informal proof sketch for the lemma statement."""
    system_prompt = "You are an expert in mathematical reasoning."
    user_prompt = (
        "Provide a brief, high-level proof sketch or argument supporting the following statement:\n\n"
        f"{informal_statement}\n\n"
        "Keep it at the level of an informal mathematical proof."
    )

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=1024,
        temperature=0.01,
    )
    return response.choices[0].message.content.strip()


async def generate_smt_skeleton(
    client: AsyncOpenAI,
    extended_statement: str,
    informal_proof: str,
    model: str,
    solver_feedback: Optional[str] = None,
) -> str:
    """Ask the model to draft an SMT-LIB sketch that captures the requirements context."""
    system_prompt = (
        "You are an SMT engineer. Produce ONLY an SMT-LIB snippet containing declarations, "
        "assumptions, and assertions that reflect the described requirements. Finish with `(check-sat)`."
    )
    template = f"""
Create an SMT-LIB fragment that captures the following requirements context.
Include declarations for key state variables, any helper functions, and assertions for safety/progress.
Reference identifiers consistently, add brief `;` comments to aid traceability, and terminate with `(check-sat)`.
Use a small bounded horizon with explicit state variables (e.g., `b0`, `b1`, `b2`, `c0`, `c1`, `c2`).
Avoid quantifiers, higher-order functions, derivatives, or recursion; stay within quantifier-free linear integer arithmetic.
Ensure each scenario ends with exactly one `(check-sat)`.

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

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": template},
        ],
        max_tokens=1024,
        temperature=0.01,
    )
    return response.choices[0].message.content.strip()


def run_z3_fragment(fragment: str) -> Dict[str, Any]:
    """Execute the SMT-LIB fragment with Z3 and capture diagnostics."""
    z3_path = os.getenv("Z3_PATH", "z3")
    try:
        proc = subprocess.run(  # noqa: S603, S607
            [z3_path, "-in"],
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
            "diagnostics": f"Z3 executable not found: {z3_path} ({exc})",
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "exit_code": None,
            "diagnostics": f"Z3 timed out after {SMT_SOLVER_TIMEOUT} seconds.",
        }

    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    lower_out = stdout.lower()
    lower_err = stderr.lower()
    error_detected = proc.returncode != 0 or "error" in lower_out or "error" in lower_err

    if error_detected:
        diagnostics_parts = [part for part in (stdout, stderr) if part]
        diagnostics = "\n".join(diagnostics_parts) or f"Z3 exited with code {proc.returncode}."
        return {
            "status": "error",
            "exit_code": proc.returncode,
            "diagnostics": diagnostics.strip(),
        }

    return {
        "status": "ok",
        "exit_code": proc.returncode,
        "result": stdout,
        "stderr": stderr or None,
    }


async def generate_validated_smt_fragment(
    client: AsyncOpenAI,
    extended_statement: str,
    informal_proof: str,
    model: str,
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
            client=client,
            extended_statement=extended_statement,
            informal_proof=informal_proof,
            model=model,
            solver_feedback=feedback,
        )
        clean_fragment = strip_code_fences(fragment)
        validation = await asyncio.to_thread(run_z3_fragment, clean_fragment)
        iterations.append({
            "raw_fragment": fragment,
            "clean_fragment": clean_fragment,
            "validation": validation,
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
            unsat_extra = "(assert false)"
        unsat_text = prepare_unsat_variant(sat_text, unsat_extra)
        unsat_path = parent / f"{base_name}_unsat.smt2"
        unsat_path.write_text(unsat_text, encoding="utf-8")
        unsat_validation = await asyncio.to_thread(run_z3_fragment, unsat_text)
        files.append(
            {
                "path": str(unsat_path),
                "mode": "unsat",
                "extra_assertions": unsat_extra,
                "validation": unsat_validation,
            }
        )

    return files


async def extract_requirements_from_chunk(
    client: AsyncOpenAI,
    chunk_text: str,
    model: str,
    id_prefix: str,
    start_index: int,
) -> Dict[str, Any]:
    """
    Extract functional requirements and assumptions from a chunk of source material.

    Returns a structure with `requirements` and `assumptions` so the caller can merge them.
    """
    system_prompt = (
        "You are a systems engineer preparing material for an MBSE pipeline. "
        "Read the provided excerpt and extract concrete, verifiable functional requirements. "
        "Each requirement must describe observable behaviour, thresholds, or invariants that "
        "could later be mapped to typed logical forms and SMT constraints."
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

    response = await client.chat.completions.create(
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
    for idx, item in enumerate(payload.get("requirements", []), start=start_index):
        req = {
            "id": item.get("id") or f"{id_prefix}{idx:03d}",
            "text": normalise_whitespace(item.get("text", "")),
            "rationale": normalise_whitespace(item.get("rationale", "")),
            "category": item.get("category", "functional"),
            "priority": item.get("priority", "medium"),
        }
        requirements.append(req)
    for assumption in payload.get("assumptions", []):
        cleaned = normalise_whitespace(assumption)
        if cleaned:
            assumptions.append(cleaned)

    return {"requirements": requirements, "assumptions": assumptions}


async def run_translate_flow(
    statement_path: Path,
    model: str,
    smt_prefix: Optional[Path] = None,
    unsat_extra: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute the natural language → informal → SMT sketch chain."""
    client = AsyncOpenAI()
    statement = load_text(statement_path)
    natural = await translate_to_natural_language(client, statement, model)
    informal_stmt = await generate_informal_statement(client, natural, model)
    informal_proof = await generate_informal_proof(client, informal_stmt, model)
    extended = (
        "Requirements source text:\n"
        f"{statement}\n\n"
        "Natural language interpretation:\n"
        f"{natural}"
    )
    smt_fragment, validation, iterations = await generate_validated_smt_fragment(
        client=client,
        extended_statement=extended,
        informal_proof=informal_proof,
        model=model,
    )
    smt_files: List[Dict[str, Any]] = []
    if smt_prefix:
        default_unsat = unsat_extra
        if default_unsat is None:
            default_unsat = "; Auto-generated contradiction\n(assert false)"
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
    }


async def run_harvest_flow(
    source_path: Path,
    model: str,
    set_id: str,
    title: str,
    system_name: str,
    chunk_chars: int,
) -> Dict[str, Any]:
    """
    Convert a long-form document into a structured requirement set.

    The result matches the JSON envelope consumed by the pipeline's `runPipeline`.
    """
    client = AsyncOpenAI()
    source_text = load_text(source_path)
    source_chunks = chunk_text(source_text, max_chars=chunk_chars)
    if not source_chunks:
        raise RuntimeError("Source document is empty after preprocessing.")

    aggregated_requirements: List[Dict[str, Any]] = []
    aggregated_assumptions: List[str] = []
    next_index = 1
    id_prefix = f"{set_id.upper()}-R"

    for chunk in source_chunks:
        result = await extract_requirements_from_chunk(client, chunk, model, id_prefix, next_index)
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
        },
    }


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Utility CLI for OpenAI-powered MBSE workflows.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    translate_parser = subparsers.add_parser("translate", help="Translate a requirements statement.")
    translate_parser.add_argument(
        "--statement",
        type=Path,
        required=True,
        help="Path to the text file containing the requirements or statement.",
    )
    translate_parser.add_argument(
        "--model",
        default=DEFAULT_TRANSLATION_MODEL,
        help="Chat completion model to use for translation (default: %(default)s).",
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
        help="Path to the source text document (UTF-8).",
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
        "--chunk-chars",
        type=int,
        default=3200,
        help="Maximum number of characters per chunk sent to the LLM (default: %(default)s).",
    )

    return parser.parse_args(argv)


async def dispatch(args: argparse.Namespace) -> Any:
    if args.command == "translate":
        return await run_translate_flow(
            statement_path=args.statement,
            model=args.model,
            smt_prefix=args.write_smt_prefix,
            unsat_extra=args.unsat_extra,
        )
    if args.command == "harvest":
        return await run_harvest_flow(
            source_path=args.source,
            model=args.model,
            set_id=args.set_id,
            title=args.title,
            system_name=args.system,
            chunk_chars=args.chunk_chars,
        )
    raise ValueError(f"Unsupported command: {args.command}")


def main(argv: Optional[Iterable[str]] = None) -> None:
    ensure_api_key()
    args = parse_args(argv)
    try:
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


if __name__ == "__main__":
    main()
