# Autoformalization MBSE Toolkit

A working prototype for converting requirement documents into inspectable typed interpretations, solver evidence, and SysML v2 models. Engineers review the meaning, assumptions, architectural associations, and proposed corrections before recording scoped model acceptance.

## One workflow through the GUI or CLI

The GUI and default CLI use the same request schema, run store, generation engines, solver checks, compiler checks, and review gates in [`review_workflow.py`](scripts/review_workflow.py). A CLI run appears in the GUI when both use the same data directory. Running the CLI does not require an HTTP server and does not approve the resulting model.

Install the Python dependencies into your environment:

```bash
python -m pip install -r requirements-review.txt
```

Start the GUI:

```bash
python scripts/review_server.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Upload or paste CSV, TXT, or JSON requirements. The generated SysML text, typed meaning, exact solver artifacts, and review history remain available per run.

Run the same workflow from the command line with your requirements file:

```bash
python scripts/requirements_pipeline.py \
  --statement requirements.csv \
  --data-dir out/review_workbench
```

An explicit `run` subcommand is equivalent:

```bash
python scripts/requirements_pipeline.py run --statement requirements.csv
```

Both interfaces default to the **local** engine and **Requirements model** mode. The local engine recognizes a limited quantitative grammar and makes no LLM calls; clauses outside that grammar remain visible and unresolved. Select `--engine pipeline` or **Existing Codex pipeline** to use LLM generation. That engine sends the submitted requirements to the configured Codex provider and keeps source-semantic approval pending.

| Analysis mode | What it does |
|---|---|
| `requirements` — default | Formalize requirement constraints, check encoded consistency, generate and compile SysML. No separate design is invented or checked. |
| `propose_design` — pipeline engine | Request an additional LLM candidate with variables, dynamics, assumptions, and source-linked properties. Preserve it unchecked for review. |
| `check_design` | Check an explicitly supplied candidate after a reviewer, rationale, and design acknowledgment are provided. |

For “The battery shall have a voltage of at most 28 V,” a requirement-consistency witness must satisfy the bound. A 29 V **design counterexample** means the separately supplied candidate admits a violation; it is not a satisfying interpretation of that requirement.

```text
CSV / TXT / JSON requirements
             |
    GUI request or default CLI
             |
      shared review workflow
             |
    +--------+------------------+--------------------+
    |                           |                    |
local grammar              Codex generation    explicit reviewed
(no LLM)                   + native TLR       behavior candidate
    |                           |                    |
    +---- interpretation -------+             finite Z3 checks
             |                                      |
      constraint consistency                  property evidence
             |                                      |
             +---- shared contracts / assumptions --+
                               |
                   SysML generation + compilation
                               |
                formalization quality evidence profile
                               |
                  engineer decisions and revisions
```

The shared workflow preserves submitted requirement identities and wording. It does not run the optional legacy intent-drafting stage. The effective generation policy, model selection, solver, compiler settings, and analysis mode are recorded in `execution_config.json` and shown in the GUI. The same settings apply to both interfaces; separate LLM calls may still produce different candidates.

See the [workbench guide](prototypes/review-workbench/README.md) for prerequisites, supported grammar, and the full review procedure.

## How formalization quality is assessed

The **Formalization quality** tab and `formalization_quality.json` report separate evidence dimensions. There is no aggregate score or claimed probability that the formalization is correct.

| Dimension | Evidence reported | What it does not establish |
|---|---|---|
| Source preservation | Distinct source IDs, exact text/location availability, duplicates | Complete document extraction or authenticated authority |
| Interpretation coverage | Local supported, candidate pending, unresolved, missing, and ambiguous source IDs | Semantic accuracy; an LLM candidate remains pending review |
| Types and units | Recorded native typecheck and validated candidate schema/unit scope | Physical correctness or stakeholder intent |
| Requirement consistency | Actual SAT, UNSAT, unknown, or not-run verdict and associated source IDs | Design compliance or the correct way to resolve a conflict |
| Encoding diagnostics | Named coverage, state alignment, conflict, vacuity, and symbol checks; skips and failures remain visible | Natural-language fidelity; these probes are separate from whole-formula SAT |
| Design behavior | Model feasibility, finite property verdicts, horizon, missing results, and unproved liveness | Unbounded safety/liveness or behavior of the physical system |
| SysML compilation | Actual compiler outcome for the generated model | Validation of engineering intent or implementation of the checked dynamics |
| Candidate provenance | Expression origins, rationale, missing updates, and repeated-guarantee diagnostics | Verified source derivation or complete detection of circular assumptions |
| Review obligations | Interpretations, assumptions, contracts, bindings, and revision changes awaiting review at generation | Current acceptance; decisions live in the review ledger |
| Artifact integrity | Snapshot and artifact hash checks | Semantic correctness |

Coverage counts use distinct source IDs and do not count duplicate interpretations as additional coverage. Empty or missing evidence is not a pass. A satisfiable LLM encoding can coexist with failed semantic probes, unresolved requirements, or failed compilation; the profile preserves all of those outcomes.

The quality artifact is an immutable completion snapshot. Its review counts describe obligations when the candidate was generated. Subsequent accept/reject/defer decisions remain in the live review views and never rewrite the machine evidence. Human source review and evidence that the architecture implements the intended behavior remain necessary.

## Shared contracts and engineer review

The **Contracts** tab connects each source requirement to its interpretation rule, typed contract, assumptions, generated SysML element, and exact Z3 query/result. Engineers can inspect the proposed meaning, compare revisions, and record review decisions.

### Supported contract rules

The available rules are defined in [`scripts/contract_rules.json`](scripts/contract_rules.json):

| Local rule | Meaning and evidence scope |
|---|---|
| `FINITE_ALWAYS` | Require an independently stated predicate at every observed state; check for a violating full-horizon execution. |
| `FIRST_RESPONSE_WINDOW` | Require the first response in an inclusive step window and forbid earlier responses. Trigger reachability, overlapping commands and late windows receive separate checks/statuses. |
| `DISTURBANCE_INVARIANT` | Search for violations across modeled disturbance choices with fixed design parameters. This is bounded robustness evidence; no worst-case margin optimization is performed. |
| `UNBOUNDED_EVENTUAL_OBLIGATION` | Preserve eventual response as an unproved obligation. A finite completion witness cannot establish unbounded liveness. |
| `UNRESOLVED_SOURCE` | Keep a requirement visible when no executable shared interpretation is available. No placeholder guarantee is promoted to a checked property. |
| `scalar_bound` | Retain supported local scalar comparisons with their shared quantity identity, units and applicability context. These establish scoped consistency, not controller behavior. |

Scalar contracts come from the supported requirement interpretation. Behavior rules are selected from a structurally validated candidate only in an explicitly selected design mode. An LLM proposal remains unchecked until an engineer reviews it and submits a separate design-check run. Contract generation, rendering, and change comparison use Python and Z3; these stages make no additional LLM calls.

### One representation, separate questions

[`review_contracts.py`](scripts/review_contracts.py) builds the canonical contract bundle and rejects divergent source/property mirrors. [`review_behavior.py`](scripts/review_behavior.py) consumes that bundle for bounded SMT queries. [`review_contract_sysml.py`](scripts/review_contract_sysml.py), connected through [`requirements_pipeline.py`](scripts/requirements_pipeline.py), renders the same behavior/property AST into the finite SysML projection. [`review_assumptions.py`](scripts/review_assumptions.py) and the model index connect premises and exact model locations to these records.

The workbench keeps the questions separate:

- **Consistency:** can the encoded requirements hold together under their recorded interpretation?
- **Candidate behavior:** does the proposed model admit a violating execution under its assumptions?
- **Compilation:** are the generated SysML syntax, references and model constructs well formed?
- **Engineering review:** are the interpretation, assumptions, allocation and any proposed change justified by the source and its authority?

A solver result answers its particular formal query. Compilation and hash-linked traceability do not establish source fidelity or equivalence between the diagnostic trace and the inferred architecture. SysML trace variables use canonical numeric magnitudes with documented units; dimensional validation occurs in the shared AST. The current bounded checker does not establish deadlock freedom, coverage of shorter nonextendable executions, general realizability, or unbounded liveness.

### Reviewing a change with Z3

The directional comparison in [`review_contract_changes.py`](scripts/review_contract_changes.py) is implemented locally. Under a fixed context `C`, it checks:

```text
C AND G_new AND NOT G_old   -> newly permitted valuations
C AND G_old AND NOT G_new   -> newly forbidden valuations
```

For behavior contracts, `C` includes declared domains, fixed parameters and explicit environmental assumptions; candidate initialization, implementation transitions and other guarantees are excluded. Scalar comparisons use their shared quantity/domain context. Context feasibility is checked first. Changed behavior contexts and unsupported comparisons remain explicit, and bounded response comparisons identify the common fully observed windows.

For example, changing a voltage limit from 28 V to 29 V permits a 29 V valuation that the old contract forbade, even if the existing candidate always produces 27 V. The GUI shows the change and witness for review. This identifies a semantic effect; it does not determine whether the old or new stakeholder requirement is correct.

The workbench includes a structured candidate editor for equations, variable declarations, and per-statement provenance. Inspection flags missing next-state references and guarantees repeated in assumptions or bounds. Engineers review architectural associations in **Architecture bindings**, with checks against exact SysML declarations and recognized types/units. The **Review** view groups revision corrections and includes existing semantic comparison evidence before acceptance. These features preserve the checked model and its artifacts; a reviewed association is not an implementation-equivalence proof. See the [workbench guide](prototypes/review-workbench/README.md) for the complete procedure.

To exercise the workflow, run the server and choose **Load contract review example**. The [workbench guide](prototypes/review-workbench/README.md#shared-contract-inspection) explains how to inspect and revise the example. Earlier saved runs keep their original evidence; new snapshots are produced by new runs.

## CLI requests and saved evidence

The default CLI accepts `.txt`, `.csv`, and `.json` through the same ingestion rules as the GUI: at most 500 requirements, 512,000 UTF-8 bytes, and 4,000 characters per requirement. CSV needs a `text`, `requirement`, `statement`, or `description` column; `id`, `owner`, `authority`, and `source` are optional. JSON can be an array or an object with a `requirements` array. PDF extraction is not connected.

Use `--request-json` to submit the exact GUI/API request object, including a reviewed candidate or parent revision details. It is different from a JSON requirements document:

```json
{
  "name": "Battery limits",
  "text": "R1: The battery shall have a voltage of at most 28 V.",
  "format": "text",
  "engine": "local",
  "analysis_mode": "requirements"
}
```

Save that object as `request.json`, then run:

```bash
python scripts/requirements_pipeline.py --request-json request.json
```

`--request-json` cannot be mixed with request-setting flags such as `--engine`, `--name`, or `--behavior`; storage and export options remain available. For flag-based design checks, use `--analysis-mode check_design`, a plain behavior object through `--behavior`, `--reviewer`, `--rationale`, and the explicit `--acknowledge-design` flag. Revision checks also require the current parent's source/evidence hashes and a revision rationale. No acknowledgment is supplied automatically.

Runs are retained under `out/review_workbench`, configurable through `--data-dir` or `MBSE_REVIEW_DATA_DIR`. Each contains source, normalized requirements, typed interpretations, solver queries/results, generated SysML, compiler diagnostics, contract/provenance snapshots, `execution_config.json`, and `formalization_quality.json`, as available. Failures preserve partial evidence. CLI stdout is JSON with the complete saved `run` and a separate `exports` map.

Optional `--output-prefix`, `--sysml-output`, and `--traceability-output` copy recorded artifacts to new destinations outside the run store. They do not relocate or overwrite evidence. A separate traceability export is available only when the engine produced that artifact; the local engine normally embeds its traceability in the model.

Exit status `0` means execution completed, not that the requirements are consistent or every property passed. Inspect the saved verdicts and quality dimensions. Status `1` indicates execution/export failure; `2` indicates an invalid request. A failed export does not erase the saved run.

## Legacy generator and utilities

Older generator controls remain available explicitly through `legacy`:

```bash
python scripts/requirements_pipeline.py legacy --help
```

For example:

```bash
python scripts/requirements_pipeline.py legacy \
  --llm-provider codex \
  --statement examples/pure/eirene_fun7_harvest_requirements_top10.csv \
  --output-prefix out/legacy_example \
  --sysml-mode domain \
  --sysml-output out/legacy_example.sysml \
  --semantic-strict
```

Legacy mode retains its CSV input contract, optional intent formalization, generator/repair flags, and `<prefix>_translate.json`, `<prefix>_tlr.json`, `<prefix>_sat.smt2`, and `<prefix>_semantic_checks.json` outputs. It does not provide the shared run/review lifecycle. Its negative `_unsat.smt2` artifact may append `assert false`; that diagnostic is not a proof that a hazard is impossible. The shared design checker instead queries the negation of a separate property over explicit candidate premises.

[`mbse_run.py`](scripts/mbse_run.py) remains a legacy reporting wrapper. [`openai_toolkit.py`](scripts/openai_toolkit.py) and [`mbse_toolkit_core.py`](scripts/mbse_toolkit_core.py) provide compatibility entry points. Standalone `translate`, `harvest`, and `formalize_intent` commands remain available; long-document harvesting is experimental. See each command's `--help` for its own options. The Codex route is the demonstrated LLM path; other legacy provider paths remain experimental.

## Configuration and prerequisites

Z3 and the SysML pilot compiler are separate prerequisites. Missing tools are reported as unavailable or inconclusive. The workbench guide describes compiler discovery and setup.

| Setting | Purpose |
|---|---|
| `MBSE_REVIEW_DATA_DIR` | Saved-run directory shared by CLI and GUI |
| `MBSE_REVIEW_PIPELINE_MODEL`, then `CODEX_MBSE_MODEL` | Override the Codex model; otherwise use installed CLI configuration and the recorded fallback |
| `MBSE_REVIEW_PIPELINE_TIMEOUT` | LLM pipeline execution timeout |
| `MBSE_SOLVER`, `SMT_SOLVER_TIMEOUT`, `Z3_PATH` | Solver selection, timeout, and Z3 executable override |
| `SYSML_JAVA`, `SYSML_KERNEL_JAR`, `SYSML_LIBRARY_DIR` | Compiler executable, kernel JAR, and SysML libraries |

The shared pipeline engine uses strict semantic checks, no semantic repair loop, one SMT correction attempt, full submitted-source prompt availability, and compilation of the final model. The per-run execution configuration records the actual settings; prompt availability does not establish semantic coverage. Native TLR preflight retains unresolved numeric bindings for inspection instead of attaching numeric bounds to Boolean placeholders.

## Testing

```bash
python -m pip install -r requirements-review.txt httpx
python -m unittest discover -s tests -v
node tests/test_review_editor.js
```

Tests cover shared CLI/GUI requests, interpretation coverage, units, solver/compiler evidence, bounded properties, semantic comparisons, provenance, architectural bindings, stale review decisions, quality reporting, and saved artifacts. Checks needing external tools or historical diagnostic fixtures skip when those prerequisites are absent. Passing implementation tests does not establish arbitrary document-to-model fidelity or industrial readiness.

## License

MIT (see [LICENSE](https://github.com/kings-crown/Autoformalization_MBSE?tab=MIT-1-ov-file)).
