# Autoformalization MBSE Toolkit

This repository provides a Python-first pipeline for turning natural-language requirements into:

- structured requirement representations,
- typed logical representations,
- validated SAT/UNSAT SMT-LIB artefacts,
- SysML v2 outputs, and
- traceability and run reports.

Prototype caveat:
- The currently validated prototype workflow is Codex-focused (`--llm-provider codex`).
- Other provider paths should be treated as experimental at this stage.

```text
requirements.csv
      |
      v
scripts/requirements_pipeline.py
      |
      +--> intent formalization -> <prefix>_intent.json
      |                         -> <prefix>_intent_requirement_set.json
      |
      +--> translation + TLR    -> <prefix>_translate.json
      |                         -> <prefix>_tlr.json
      |
      +--> SMT + Z3 validation  -> <prefix>_sat.smt2
      |                         -> <prefix>_unsat.smt2
      |                         -> <prefix>_semantic_checks.json
      |
      +--> SysML generation     -> <sysml-output> (domain/evidence/architecture)
```

## Primary Entry Points

- `scripts/requirements_pipeline.py`: end-to-end translation + SMT validation + SysML generation.
- `scripts/mbse_run.py`: wrapper around the pipeline with friendlier diagnosis and markdown reports.
- `scripts/openai_toolkit.py`: compatibility shim that forwards to the unified pipeline entrypoint.
- `scripts/mbse_toolkit_core.py`: legacy compatibility shim for older imports/CLI calls.

`requirements_pipeline.py` is the authoritative unified CLI surface. It accepts:
- pipeline flags directly (existing behavior)
- delegated toolkit commands: `translate`, `harvest`, `formalize_intent`

By default, pipeline mode now runs an intent-formalization pre-stage and then
translates from the normalized `requirement_set`. Use `--skip-intent-formalization`
to bypass this.

Main demonstration CSV files used in this repo:
- `examples/pure/eirene_fun7_harvest_requirements_top10.csv` (baseline set)
- `examples/pure/eirene_fun7_harvest_requirements_top11.csv` (baseline + contradiction/ambiguity injection)

Canonical pipeline command format:

```bash
STATEMENT="${1:-examples/pure/eirene_fun7_harvest_requirements_top10.csv}"
PREFIX="${2:-out/e2e_prod}"
SYSML_OUT="${3:-out/e2e_prod_domain.sysml}"
TRACE_OUT="${4:-out/e2e_prod_trace.sysml}"

python3 scripts/requirements_pipeline.py \
  --llm-provider codex \
  --model gpt-5.4 \
  --statement "$STATEMENT" \
  --output-prefix "$PREFIX" \
  --sysml-mode domain \
  --sysml-output "$SYSML_OUT" \
  --traceability-output "$TRACE_OUT" \
  --require-intent-formalization \
  --semantic-strict \
  --skip-sysml-compile
```

## Quick Start

### 1. Run a production-style pipeline (Codex provider)

```bash
python3 scripts/mbse_run.py \
  --llm-provider codex \
  --model gpt-5.4 \
  --statement examples/pure/eirene_fun7_harvest_requirements_top11.csv \
  --output-prefix out/eirene_fun7_top11 \
  --sysml-mode domain \
  --sysml-output out/EIRENE_FUN7_top11_domain.sysml \
  --traceability-output out/EIRENE_FUN7_top11_trace.sysml \
  --skip-sysml-compile
```

### 2. Run end-to-end directly

```bash
python3 scripts/requirements_pipeline.py \
  --llm-provider codex \
  --model gpt-5.4 \
  --statement examples/pure/eirene_fun7_harvest_requirements_top10.csv \
  --output-prefix out/eirene_fun7_top10_codex \
  --sysml-output out/EIRENE_FUN7_top10_codex.sysml \
  --skip-sysml-compile
```

## Input Support

Prototype input contract (current stage):
- `--statement` is expected to be a CSV requirements file (`.csv`).
- Non-CSV inputs (`.pdf`, `.txt`, `.json`) are not supported at this stage.
- Use the demonstration CSVs:
  - `examples/pure/eirene_fun7_harvest_requirements_top10.csv`
  - `examples/pure/eirene_fun7_harvest_requirements_top11.csv`

- `openai_toolkit.py harvest` long-form document ingestion is work in progress; under the current CSV-only prototype contract, non-CSV sources are intentionally rejected.
- `formalize_intent` can normalize requirement sets and produce formalization scaffolds.

## Output Artefacts

Typical pipeline outputs include:

- `<prefix>_translate.json`
- `<prefix>_tlr.json`
- `<prefix>_sat.smt2`
- `<prefix>_unsat.smt2`
- `<prefix>_run_report.md` (via `mbse_run.py`)
- `<sysml-output>`
- `<traceability-output>` (domain mode)

## LLM Providers

Two providers are supported:

- `openai`: via OpenAI API key.
- `codex`: via local `codex exec` CLI.

Current prototype caveat:
- For this repository stage, treat `codex` as the primary validated provider for end-to-end runs.
- Expansion and hardening of additional providers is planned future work.

Examples:

```bash
python3 scripts/requirements_pipeline.py --llm-provider codex --model gpt-5.4 ...
```

```bash
OPENAI_API_KEY=sk-... python3 scripts/requirements_pipeline.py --llm-provider openai ...
```

## Configuration

Common variables:

- `OPENAI_API_KEY`: OpenAI credentials for provider `openai`.
- `OPENAI_BASE_URL`: override OpenAI-compatible endpoint.
- `MBSE_LLM_PROVIDER`: default provider (`openai` or `codex`).
- `CODEX_MBSE_MODEL`: default Codex model (default `gpt-5.4`).
- `CODEX_EXEC_TIMEOUT`: timeout (seconds) for `codex exec` calls.
- `CODEX_REASONING_EFFORT`: `low|medium|high|xhigh`.
- `SMT_FIX_ATTEMPTS`: retries for SMT correction loop.
- `SMT_SOLVER_TIMEOUT`: solver timeout in seconds.
- `Z3_PATH`: path to native Z3 binary.
- `SYSML_KERNEL_JAR`: optional SysML kernel jar for compile validation.

## TLR Contracts

The toolkit distinguishes two schema contracts:

- `requirements_tlr`: requirement-centric working representation.
- `logical_form_tlr`: solver-facing representation.

See `scripts/tlr_contracts.py` for detection and conversions.

## Testing

Automated tests are intentionally deferred at this prototype stage while the core pipeline is being stabilized.

Future work (planned):
- Add a focused integration suite for `requirements_pipeline.py` covering:
  - `--sysml-mode domain`,
  - `--sysml-mode architecture`,
  - traceability output generation,
  - semantic-strict SMT repair/validation scenarios.
- Expand and harden non-Codex provider support.
- Add a usability-focused user interface on top of the current CLI workflow.

## Troubleshooting

- `OPENAI_API_KEY is not set`: export key or switch to `--llm-provider codex`.
- `codex exec failed`: ensure `codex` is installed/authenticated and network is available.
- `Z3 timed out`: reduce fragment complexity, tighten prompts, or increase timeout.
- SysML compile issues: use `--skip-sysml-compile` to keep generated artefacts while diagnosing.

## License

MIT (see `LICENSE`).
