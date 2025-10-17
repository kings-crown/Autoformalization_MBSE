# Autoformalization MBSE Toolkit

This project demonstrates an end‑to‑end Model‑Based Systems Engineering (MBSE) workflow that turns
natural-language requirements into solver-backed artefacts. The Node pipeline ingests structured
requirements, synthesises typed logical forms, emits SMT-LIB, and executes Z3 (native or WebAssembly).
The Python toolkit complements this flow with automatic policy translation and SAT/UNSAT witness
generation.

```
requirements.json  ─┐
                    │ src/pipeline.js ─► logical form ─► SMT-LIB ─► Z3 result
requirements.tx  ───┘                         ▲
                                           scripts/openai_toolkit.py
```

The sections below describe every stage and point to the corresponding source files.

---

## 1. Core Pipeline (Node.js)

### 1.1 CLI driver – `src/cli.js`
- Argument parsing (`parseArgs` in `src/cli.js:12-38`) supports `--input`, `--output`, optional
  `--model`, and `--skip-solver`.
- Requirements are read from disk (`src/cli.js:44-49`), passed to `runPipeline`, and emitted as
  `<id>.tlf.json`, `<id>.smt2`, and `<id>.solver.json` (`src/cli.js:58-70`).

Run it with `npm run pipeline:run -- --input examples/door-controller.json --output out`.

### 1.2 Translation to typed logical form – `src/llm`
- `runPipeline` (`src/pipeline.js:7-27`) drives the flow: translate → validate → canonicalise → emit
  SMT → solve.
- `translateRequirementsToLogicalForm` (`src/llm/translator.js:6-24`) builds a structured prompt and
  calls the OpenAI API via `createChatCompletion` (`src/llm/openaiClient.js`), or returns canned
  output from `src/llm/mockResponses.js` when `MOCK_LLM=1`.
- Prompts (`src/llm/prompt.js`) embed assumptions, requirement IDs, and a JSON shape reminder to keep
  the LLM honest.

### 1.3 Schema enforcement – `src/tlf/schema.js`
- `logicalFormSchema` (`src/tlf/schema.js:204-227`) uses Zod to ensure the LLM output contains
  well-typed symbols and formula ASTs. Unsupported constructs throw before SMT generation.
- Term variants (`src/tlf/schema.js:36-174`) cover identifiers, arithmetic, boolean connectives,
  quantifiers, `let`, function/predicate calls, etc.

### 1.4 Canonicalisation – `src/tlf/transformers.js`
- `canonicaliseLogicalForm` (`src/tlf/transformers.js:73-88`) alphabetises symbol tables and applies
  `deepSortTerm` (`src/tlf/transformers.js:5-70`) so the same logical form always generates the same
  SMT-LIB—ideal for diffing and caching.

### 1.5 SMT-LIB emission – `src/tlf/smtlibGenerator.js`
- `logicalFormToSmtlib` (`src/tlf/smtlibGenerator.js:114-160`) converts the canonical logical form into
  solver-ready SMT-LIB. Helpers such as `emitTerm` (`src/tlf/smtlibGenerator.js:11-78`) and
  `emitEnumDecls` (`src/tlf/smtlibGenerator.js:82-90`) map each AST node into SMT syntax, attach
  comments, and append `(check-sat)`/`(get-model)`.

### 1.6 Solver execution – `src/solver/wasmSolver.js`
- Preferred execution path wraps the native `z3` binary (`run_z3_fragment` in
  `scripts/openai_toolkit.py:236-272` and `solveWithZ3` in `src/solver/wasmSolver.js:77-111`).
- When `Z3_USE_WASM=1`, `locateZ3Module` (`src/solver/wasmSolver.js:16-49`) loads the Emscripten
  bundle from `src/solver/vendor/`. `Module.locateFile` rewrites URLs so `z3.wasm` and
  `z3.worker.cjs` resolve correctly.
- If `SKIP_Z3=1`, the SMT string is returned but no solver is invoked.

### 1.7 Outputs
- `<output>/<id>.tlf.json` – canonical typed logical form ready for traceability.
- `<output>/<id>.smt2` – solver-ready SMT program.
- `<output>/<id>.solver.json` – solver result/diagnostics when Z3 runs.

Sample files are available under `examples/` (input) and `out/` (generated output).

---

## 2. Python Toolkit (`scripts/openai_toolkit.py`)

### 2.1 `translate`
- Accepts a plain-text policy (`--statement`).
- Calls `generate_validated_smt_fragment` (`scripts/openai_toolkit.py:215-287`) which repeatedly
  solicits SMT-LIB from the LLM, strips Markdown fences (`strip_code_fences` at
  `scripts/openai_toolkit.py:67-79`), runs Z3 (`run_z3_fragment` at `scripts/openai_toolkit.py:191-213`),
  and feeds solver feedback back into the prompt until the fragment is valid.
- With `--write-smt-prefix out/policy`, `write_smt_outputs` (`scripts/openai_toolkit.py:289-323`)
  writes `out/policy_sat.smt2` and `out/policy_unsat.smt2`, verifying both via Z3. The UNSAT variant
  appends either `--unsat-extra` or the default `assert false` using `prepare_unsat_variant`
  (`scripts/openai_toolkit.py:81-92`).
- Results (original text, informal reasoning, clean SMT, solver iterations, file paths) are returned as
  JSON; use `--output-json` or rely on the auto-generated `<prefix>_translate.json` when
  `--write-smt-prefix` is supplied.

Example:

```bash
OPENAI_API_KEY=sk-... python scripts/openai_toolkit.py translate \
  --statement policy_charging.txt \
  --write-smt-prefix out/policy_charging \
  --output-json out/policy_charging_translate.json

z3 out/policy_charging_sat.smt2     # sat
z3 out/policy_charging_unsat.smt2   # unsat
```

### 2.2 `harvest`
- Splits long documents into manageable chunks (`chunk_text` at `scripts/openai_toolkit.py:48-66`).
- Extracts structured requirements and assumptions with
  `extract_requirements_from_chunk` (`scripts/openai_toolkit.py:137-182`).
- Aggregates the chunks into a JSON payload that mirrors `examples/door-controller.json`, ideal for
  `npm run pipeline:run`.

---

## 3. Directory Reference

| Path | Purpose |
|------|---------|
| `policy_charging.txt` | Example policy used in the toolkit walkthrough. |
| `scripts/openai_toolkit.py` | Python CLI (translate & harvest commands). |
| `src/cli.js` | Node CLI driver (see §1.1). |
| `src/pipeline.js` | Orchestration of the translation/validation/generation/solve phases. |
| `src/llm/` | Prompt building, OpenAI client, mock responses. |
| `src/tlf/` | Typed logical form schema, canonicalisation, SMT generator. |
| `src/solver/` | Native + WASM Z3 loader utilities. |
| `tests/smtlibGenerator.test.js` | Node test validating SMT emission against the mock logical form. |

---

## 4. Configuration

| Variable | Effect |
|----------|--------|
| `OPENAI_API_KEY` | API token for all LLM calls (Node & Python). |
| `OPENAI_BASE_URL` | Override the OpenAI-compatible base URL. |
| `OPENAI_MODEL` | Default model used by the Node pipeline. |
| `OPENAI_TRANSLATION_MODEL`, `OPENAI_HARVEST_MODEL` | Override models for toolkit commands. |
| `MOCK_LLM` | When `1`, use canned logical forms. |
| `SKIP_Z3` | Skip solver execution (still write SMT-LIB). |
| `Z3_PATH` | Path to the native Z3 binary (default `z3`). |
| `Z3_USE_WASM` | Force the pipeline to use the WASM bundle. |
| `SMT_FIX_ATTEMPTS` | Max SMT refinement attempts in the toolkit (default `3`). |
| `SMT_SOLVER_TIMEOUT` | Solver timeout in seconds for toolkit validation (default `10`). |

Place `z3.js`, `z3.wasm`, and `z3.worker.cjs` under `src/solver/vendor/` to enable WebAssembly.

---

## 5. Common Workflows

| Task | Command |
|------|---------|
| Run the Node pipeline with real LLM | `OPENAI_API_KEY=sk-... npm run pipeline:run -- --input examples/door-controller.json --output out` |
| Run the pipeline with the canned logical form | `MOCK_LLM=1 npm run pipeline:run -- --input examples/door-controller.json --output out` |
| Translate a policy and emit SAT/UNSAT SMT files | `OPENAI_API_KEY=sk-... python scripts/openai_toolkit.py translate --statement policy_charging.txt --write-smt-prefix out/policy_charging --output-json out/policy_charging_translate.json` |
| Harvest requirements from a long PDF/text | `OPENAI_API_KEY=sk-... python scripts/openai_toolkit.py harvest --source requirements.txt --set-id SYS --title "Target System" --system "Subsystem" > examples/harvested.json` |
| Run the SMT generator test suite | `npm run pipeline:test` |

---

## 6. Example Artefacts

Running the toolkit example above creates the following files under `out/`:

- `policy_charging_translate.json` – Original statement, informal reasoning, SMT fragments, solver
  diagnostics, and file paths.
- `policy_charging_sat.smt2` – Z3-verifiable SAT scenario (`z3 …` prints `sat`).
- `policy_charging_unsat.smt2` – The same scenario with an injected contradiction (defaults to
  `(assert false)`), yielding `unsat`.

The Node pipeline produces analogous outputs for structured JSON requirements (e.g.,
`door-controller.tlf.json`, `door-controller.smt2`, `door-controller.solver.json`).

---

## 7. Development Notes

- LLM calls are deterministic (`temperature=0`, `top_p=0.1`, `response_format=json_object`).
- The toolkit strips Markdown fences before hitting Z3 and keeps every refinement attempt in
  `smt_iterations` for debugging.
- `tests/smtlibGenerator.test.js` ensures the SMT generator stays in sync with the mock logical form;
  add additional tests as the schema expands.
- When adding new term kinds, update `src/tlf/schema.js`, `src/tlf/transformers.js`, and
  `src/tlf/smtlibGenerator.js` in tandem.

---

## 8. License

This project is released under the MIT License (see `LICENSE`).

---

## 9. Detailed Execution Flow & Error Handling

### 9.1 Event trace for `npm run pipeline:run`

1. **CLI parsing** (`src/cli.js:12-38`) reads switches and resolves absolute paths.
2. **Requirement ingestion** (`src/cli.js:44-49`) loads JSON and preserves it in memory.
3. **Logical form synthesis** (`src/pipeline.js:12`) calls the LLM translator, which builds prompts
   (`src/llm/prompt.js`) targeting the schema documented in `logicalFormShapeDescription`
   (`src/tlf/schema.js:230-258`).
4. **Validation** (`src/pipeline.js:13`) throws on schema mismatches. Errors bubble back to the CLI so
   shell status codes remain non-zero.
5. **Canonicalisation** (`src/pipeline.js:14`) ensures deterministic ordering; no-op if the logical
   form is already consistent.
6. **SMT-LIB emission** (`src/pipeline.js:15`) yields a single string. Comments include the originating
   requirement IDs for traceability.
7. **Solver invocation** (`src/pipeline.js:18-22`) chooses execution mode:
   - Native (`Z3_PATH`): `solveWithZ3` spawns `z3 -in` and streams the SMT program through stdin.
   - WASM (`Z3_USE_WASM=1`): `locateZ3Module` loads `src/solver/vendor/z3.js` and runs `Module.solve`.
   - Skipped (`SKIP_Z3=1`): returns `{ status: "skipped" }` with a reason.
8. **Artefact write-out** (`src/cli.js:62-70`) never discards intermediate files—even on solver errors—
   so you can re-run the solver manually after corrections.

### 9.2 Solver diagnostics

- Native failures (`exit_code != 0` or any `stderr`) propagate through `solveWithZ3` and are written to
  `<id>.solver.json`. Expect fields `status: "error"`, `error: <message>`, and any partial stdout.
- WASM failures surface as thrown exceptions from the Emscripten module. These get wrapped into the
  same structure by `solveWithZ3`.
- Use `Z3_PATH` to point to alternate builds (e.g., a debug or nightly binary).

### 9.3 Toolkit retry logic

- Each iteration (`smt_iterations[i]`) records:
  - `raw_fragment` – verbatim LLM response.
  - `clean_fragment` – fence-free version sent to Z3.
  - `validation` – Z3 status/diagnostics for that attempt.
- If all attempts fail, the final JSON emphasises the last error. You can adjust the policy text, pass
  `--unsat-extra` for custom contradictions, or bump `SMT_FIX_ATTEMPTS`/`SMT_SOLVER_TIMEOUT`.
- When the solver responds `unknown`, consider tightening the prompt (e.g., “avoid quantifiers”) or
  building a bounded horizon manually.

---

## 10. Customising Prompts & Logical Form Schema

- **Prompt templates** (`src/llm/prompt.js`) can be tailored to your domain: add new instruction lines
  or augment the schema reminder. Ensure corresponding schema changes are made in
  `src/tlf/schema.js`.
- **Schema extensions**: add new term variants to the discriminated union in `schema.js`, extend
  `deepSortTerm` so canonicalisation handles the new shape, and update `emitTerm` in
  `smtlibGenerator.js` to produce correct SMT.
- **Toolkit generation hints**: the translate command’s prompt includes instructions such as “Use a
  small bounded horizon” and “avoid quantifiers”. Tweak those strings inside
  `generate_smt_skeleton` (`scripts/openai_toolkit.py:120-146`) to suit your modelling needs.

---

## 11. Native vs WASM Z3

- **Native path**: lower latency, full feature set, no need for the WASM bundle. Ensure `Z3_PATH`
  points to a local executable (default `z3`).
- **WASM path**: convenient when distributing a self-contained package or running in environments
  without a native binary. Place `z3.js`, `z3.wasm`, `z3.worker.cjs` under `src/solver/vendor/`.
  `locateZ3Module` injects a global `Module.locateFile` so dependent files load correctly.
- **Fallback behaviour**: if the WASM bundle is missing and `Z3_USE_WASM=1`, the solver stage produces
  `{ status: 'skipped', reason: 'Z3 wasm bundle unavailable.' }` and the CLI reports the skip.

---

## 12. Testing & Continuous Validation

- **Unit tests**: `npm run pipeline:test` executes `tests/smtlibGenerator.test.js`. Expand the suite to
  cover new schema constructs or solver behaviours.
- **Manual smoke tests**:
  1. `npm run pipeline:run -- --input examples/door-controller.json --output out`
  2. `python scripts/openai_toolkit.py translate --statement policy_charging.txt --write-smt-prefix out/policy_charging`
  3. `z3 out/policy_charging_sat.smt2`, `z3 out/policy_charging_unsat.smt2`
- **Regeneration checks**: because canonicalisation enforces deterministic output, re-running the
  pipeline should produce identical SMT strings for unchanged inputs.

---

## 13. Troubleshooting

| Symptom | Likely Cause | Resolution |
|---------|--------------|-----------|
| `OPENAI_API_KEY is not set` | Environment not configured | `export OPENAI_API_KEY=...` or populate `.env`. |
| `LLM call failed with status …` | Network/auth issue | Verify key, model name, and base URL. |
| `Z3 timed out after … seconds` | Solver stuck in undecidable fragment | Tighten prompt (avoid quantifiers), increase timeout, or reduce model horizon. |
| `MODULE_NOT_FOUND z3.worker.js` | WASM bundle incomplete | Rename the worker to `z3.worker.cjs` and ensure `z3.js/z3.wasm` coexist. |
| Toolkit emits malformed SMT | Inspect `smt_iterations` → `raw_fragment` for prompt adjustments; consider adding guardrails in `generate_smt_skeleton`. |

---

## 14. Glossary

- **TLF (Typed Logical Form)** – Structured, typed representation of requirements used as an
  intermediate artefact before SMT generation.
- **SMT-LIB** – Standard language for SMT solvers such as Z3.
- **SAT / UNSAT** – Solver verdict indicating whether the constraints have a satisfying assignment or
  are contradictory.
- **MBSE** – Model-Based Systems Engineering, emphasising formal system models alongside textual
  documentation.

