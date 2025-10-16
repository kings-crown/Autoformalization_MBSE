# Autoformalization MBSE Pipeline

This project provides a reference pipeline that converts structured functional requirements into Typed Logical Forms (TLFs) and finally into SMT-LIB artefacts that can be checked with the existing WebAssembly build of Z3.

## Overview

```
requirements.json ──► LLM prompt ──► typed logical form (JSON) ──► SMT-LIB (.smt2) ──► Z3 wasm
```

1. **Requirements ingestion** – Parse a JSON document describing the system context and the natural-language requirements.
2. **Typed Logical Form synthesis** – Call an LLM with a strict prompt that requests a machine-readable logical signature and formulas.
3. **Validation and canonicalisation** – Check the returned logical form against a schema (using `zod`) and normalise symbol ordering.
4. **SMT-LIB generation** – Lower the logical form into declarations and assertions suitable for Z3.
5. **Verification (optional)** – Send the SMT-LIB programme to the wasm-backed solver, or persist it for offline inspection.

## Getting started

```bash
npm install

# Run the pipeline (requires OPENAI_API_KEY unless MOCK_LLM=1)
OPENAI_API_KEY=sk-... npm run pipeline:run -- \
  --input examples/door-controller.json \
  --output out/

# Use the deterministic mock instead of calling an LLM
MOCK_LLM=1 npm run pipeline:run -- \
  --input examples/door-controller.json \
  --output out/

# Execute regression tests
npm run pipeline:test
```

The CLI produces two artefacts for each run:

- `*.tlf.json` – The canonical typed logical form emitted after validation.
- `*.smt2` – The SMT-LIB programme derived from the logical form.

## Environment variables

- `OPENAI_API_KEY` – Token for the target LLM API (OpenAI-compatible interface).
- `OPENAI_BASE_URL` – Override the API base URL (defaults to `https://api.openai.com/v1`).
- `OPENAI_MODEL` – Chat/completions model identifier (defaults to `gpt-4.1-mini`).
- `MOCK_LLM` – Set to `1` to skip network calls and return canned logical forms.
- `SKIP_Z3` – Set to `1` to bypass solver execution; useful when the wasm bundle is absent.

## Expected Z3 wasm bundle

Place the existing `z3.js`, `z3.wasm`, and `z3.worker.js` files under `src/solver/vendor/`. The helper will attempt to load them when solver execution is enabled. If they are missing, the pipeline still produces SMT-LIB but emits a warning instead of running verification.

## Repository layout

- `examples/` – Requirements samples used by the CLI and tests.
- `examples/battery-charger.smt2` – Standalone SMT-LIB “Example 2” describing a bounded battery charger.
- `src/cli.js` – Command-line entry point.
- `src/pipeline.js` – High-level orchestration of the pipeline stages.
- `src/llm/` – Abstractions for interacting with the LLM (real API and mock fallback).
- `src/tlf/` – Schema definitions, normalisation logic, and SMT-LIB lowering.
- `src/solver/` – Wrapper around the wasm Z3 bundle.
- `tests/` – Unit tests for the deterministic components (schema validation and SMT-LIB generation).

## Roadmap

- Integrate richer domain ontologies into the typed logical form schema.
- Support iterative refinement: automatically revise prompts when the solver returns `unsat`.
- Surface traceability metadata so generated SMT constraints link back to the original requirements IDs.

Contributions are welcome—raise issues or PRs to extend the schema, improve the prompt, or wire the pipeline into your MBSE tooling.
