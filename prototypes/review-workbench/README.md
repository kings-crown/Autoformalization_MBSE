**Requirements-to-SysML review workbench**

This local application connects requirement ingestion, generation, actual solver/compiler runs, and engineer review. Its frontend includes a model explorer, source-linked assumption decisions, and bounded behavioral evidence. The actual generated SysML stays visible beside the explorer with line numbers and raw download. Its frontend is [index.html](index.html); the service is [review_server.py](../../scripts/review_server.py).

**Run the application**

From the repository root, using the project Python environment:

```bash
python scripts/review_server.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Opening the HTML file directly does not connect the backend. Stop the foreground service with Ctrl+C.

The web service requires FastAPI, Uvicorn, and Pydantic 2. The tested environment uses Python 3.13.12, FastAPI 0.136.3, Uvicorn 0.51.0, and Pydantic 2.13.4. To install those web dependencies into an environment you manage:

```bash
python -m pip install -r requirements-review.txt
```

Z3 and the SysML pilot implementation are separate prerequisites. The sidebar reports their detected availability; each run records the actual outcome. The compiler requires a compatible Java executable, a `jupyter-sysml-kernel-*-all.jar`, and its `sysml.library` directory. Discovery searches common local Conda installations. Override discovery when necessary:

```bash
export Z3_PATH=/path/to/z3
export SYSML_JAVA=/path/to/java
export SYSML_KERNEL_JAR=/path/to/jupyter-sysml-kernel-version-all.jar
export SYSML_LIBRARY_DIR=/path/to/sysml.library
python scripts/review_server.py --port 8765 --data-dir /path/to/review-runs
```

Use real installed paths in these overrides. Missing tools produce unavailable/not-run evidence, rather than a simulated pass. The service binds to `127.0.0.1`; it is a local research application, not an authenticated multiuser deployment.

**Choose an engine**

| Engine | Behavior and scope |
|---|---|
| Local constraint profile | Deterministically recognizes the grammar below, creates an explicit typed interpretation, calls the existing Z3 runner, and emits SysML quantities and requirement constraints from that interpretation. It invokes the installed SysML parser/validator. No LLM call is made by this engine. |
| Existing Codex pipeline | Invokes the repository's existing LLM pipeline using the configured Codex CLI account. Submitted requirements go to that provider. Availability in the selector means the executable was found; authentication, model access, and network access are checked during execution. Partial artifacts and failures remain inspectable. Generator-reported assumptions are captured. An additional LLM call proposes a transition model only when **Propose design for review** is explicitly selected. |

The pipeline adapter selects its model from `MBSE_REVIEW_PIPELINE_MODEL`, then `CODEX_MBSE_MODEL`, then the installed CLI configuration, with a legacy default as fallback. `MBSE_REVIEW_PIPELINE_TIMEOUT` sets the execution limit in seconds; the default is 600. See [review_pipeline_adapter.py](../../scripts/review_pipeline_adapter.py) for the exact invocation. A pipeline-generated model is a review candidate: this workbench does not enable acceptance of its unestablished source-to-model semantic alignment.

**Choose what to analyze**

| Mode | What runs |
|---|---|
| Requirements model (`requirements`, default) | Generate requirement constraints and SysML, and check encoded requirement consistency. Neither engine requests or checks a separate candidate transition model in this mode. |
| Propose design for review (`propose_design`) | Use the Codex engine to propose a candidate transition model. Save it for inspection with no Z3 design checks. |
| Check reviewed design (`check_design`) | Check an explicitly supplied candidate after the engineer provides a reviewer name, rationale and acknowledgment of the reviewed model and assumptions. This is a separate question from requirement consistency. |

For “The battery shall have a voltage of at most 28 V,” the requirement constraint is `battery.voltage <= 28`. A requirement-consistency witness must respect that bound. A design counterexample such as 29 V means the separately reviewed candidate permits a violation; it is not a satisfying interpretation of the requirement. A blank behavior input never requests an automatic design proposal.

**Input formats and limits**

Paste input or upload `.txt`, `.csv`, or `.json`. The current limits are 500 requirements, 512,000 UTF-8 input bytes, and 4,000 characters per requirement. Set names are limited to 160 characters. PDF extraction and complete document/glossary reconstruction are not connected.
Large uploads retain their full imported source at the SMT-generation prompt boundary. The adapter sizes the source-text allowance to the serialized document and records `prompt_coverage.json`; this availability check does not establish semantic completeness of generated assertions.


- **Text:** one requirement per nonempty line. Bullet prefixes are removed. An identifier followed by `:`, `.`, or `)` and a space is recognized; otherwise IDs are generated.
- **CSV:** include a `text`, `requirement`, `statement`, or `description` column. `id`, `owner`, `authority`, and `source` are optional. Quote cells containing commas or newlines.
- **JSON:** an array of strings or requirement objects, or an object containing a `requirements` array. Objects can preserve source citations and owner/authority declarations.

For example:

```csv
id,requirement,owner,source
R1,"After each START command, the controller shall produce the response within 5 seconds.",Operations owner,Section 4.2
R2,"After each START command, the controller shall produce the response no earlier than 8 seconds.",Timing owner,Section 2.7
```

```json
[
  {
    "id": "R3",
    "text": "The battery shall have a voltage of at least 10.5 V.",
    "source": {"document": "Electrical requirements", "location": "Section 3.1"},
    "owner": "Electrical owner"
  }
]
```

Source and authority metadata are user supplied; importing them does not authenticate ownership or authorize an amendment.

**Supported local grammar**

[review_profile.py](../../scripts/review_profile.py) defines the authoritative grammar. Representative accepted clauses are:

```text
After each START command, the controller shall produce the response within 5 seconds.
After every START command, the controller shall send an acknowledgment before 500 ms.
The battery shall have a voltage of at least 10.5 V.
The battery's voltage shall be at most 28 V.
battery.voltage >= 10.5 V.
```

Comparisons include `at least`, `at most`, `less than`, `greater than`, `exactly`, and symbolic comparisons. Timing also supports `within`, `no earlier than`, `before`, and `later than`. Boundary inclusion follows the chosen comparison. Numbers use signed decimal notation, not spelled-out numbers or scientific notation in the source grammar.

The profile includes explicit conversions for time, voltage, mass, length, current, power, and energy; examples include milliseconds to seconds and millivolts to volts. It also supports kelvin, percent, and dimensionless quantities. SI unit symbols are case-sensitive (`mV` is supported; `MV` is not silently treated as millivolts). Spelled-out unit names are case-insensitive. Unsupported units and incompatible dimensions remain unresolved. Percent uses the 0–100-style numeric scale; its SysML unit is retained as metadata rather than an inferred dimensionless ratio.

Compound clauses and conditional applicability, including `and`, `or`, `if`, `unless`, and `when`, remain unsupported. Names establish shared quantities only when the recorded subject/property/trigger/response names match. Timing interpretation explicitly assumes a required response occurrence, a common command reference, and nonnegative elapsed time. It represents per-instance bounds; it does not implement an executable controller or prove general liveness.

**Inspect, revise, and record**

1. Generate a run, then inspect **SysML model**, **Source**, **Typed meaning**, **Assumptions**, **Constraint analysis**, and **Behavioral analysis**. Stage completion, satisfiability, compilation, and review are separate states. A contradictory model can still compile.
2. Select a model element to highlight its actual generated source and follow links to requirements, assumptions, and behavioral properties. The explorer is a conservative text index, not an independent semantic parser. Historical runs without an index still display their full generated code. Inspect conflict IDs, source wording, exact bounds, and the solver evidence. The bound diagram is only a visualization; it does not replace the recorded solver result.
3. Select **Create revised run**, edit the candidate wording, and record the engineering rationale and pending authority. Submission preserves identifiers and provenance, records the parent run, and produces fresh analysis/model artifacts. It does not overwrite or authorize the original source.
4. In **Assumptions**, inspect each premise’s origin, source links, encoded/metadata/unestablished status, impact, and artifact evidence. Record an individual **accept**, **reject**, or **defer** decision with reviewer and engineering rationale. Decisions are saved by the service and their history survives refreshes and restarts. Accepting a premise does not add it to the encoding; rejecting an encoded premise requires a revised model and fresh checks. Pending, rejected, and deferred assumptions block final acceptance.
5. Record a recommendation or deferral. **Accept this prototype model** is available only when the server's support, consistency, compilation, execution, and current-revision conditions pass, and the reviewer supplies rationale, scope, and acknowledgment. Acceptance covers the local model interpretation; it is not organizational signoff or a source amendment.

Reviews bind source and evidence hashes. Assumption reviews also use an optimistic concurrency hash; a changed assumption decision invalidates the current final-review acknowledgment and requires a fresh final model review. Changed artifacts or stale hashes are rejected. A newer candidate supersedes its parent's eligibility for current acceptance, including while the new run is still in progress. Historical evidence and decisions remain available.

**Candidate behavior and temporal evidence**

Choose **Check reviewed design** when creating a run to provide a candidate transition model and properties as JSON. Inspect the candidate's variable roles, numerical domains, initial conditions, transitions, assumptions and mapping to source requirements. Supply the reviewer and rationale, then explicitly acknowledge that review before submitting. This acknowledgment authorizes the bounded check; it is not final model acceptance or organizational signoff.

**Load behavior example + requirements** and **Load contract review example** replace the inputs with matching example requirements and behavior and select the design-check mode. They leave the review acknowledgment unchecked. Review the example and complete the same fields before starting its analysis.

To request an LLM candidate, select the Codex engine and **Propose design for review**. The behavioral view shows the proposal and its origin, or a proposal failure. The candidate is saved unchecked. After inspecting and, if needed, editing it, create a child run in **Check reviewed design** mode with a fresh reviewer, rationale and acknowledgment. Structural validation of a proposal does not establish that its assumptions reflect the source.

The **Behavioral analysis** tab presents model feasibility and each returned property check with its kind, exact verdict, scope/horizon, source requirement links, relevant model elements, artifacts, and actual execution/counterexample trace. It also preserves the full query/result JSON. Different quantifications and horizons remain explicit: one feasible execution does not establish every execution, bounded exclusion of a hazard does not establish unbounded safety, and unbounded liveness remains unproved. Checks cover only executions extendable through the complete configured horizon. Shorter or deadlocking executions and transition totality remain unverified. These limitations appear before property results. The separate candidate’s result does not prove equivalence to generated SysML behavior or the implemented system.

Engineers can revise the candidate while keeping requirement wording unchanged, provided they record a revision rationale. Each design-check run requires its own explicit review acknowledgment; prior checks and example loading do not approve a revised candidate. To omit design analysis, select **Requirements model**. To request a new LLM candidate, explicitly select **Propose design for review**. Source and candidate revisions produce fresh artifacts and retain their parent history.

**Persistence and evidence**

Runs are saved under `out/review_workbench` by default. Change this with `--data-dir` or `MBSE_REVIEW_DATA_DIR`. Source files, normalized requirements, interpretations, solver files, SysML, diagnostics, and review records survive a service restart. Browser-only edits and unsubmitted review text do not survive a page reload. A run interrupted by service shutdown is marked failed on restart; create a new run to retry it.

Download individual artifacts or the JSON review packet from the GUI. Artifact downloads are checked against their recorded hashes. The JSON packet embeds the original input, generated SysML, SMT files, other text artifacts, their hashes, and the run/review records. It is self-contained for inspecting the saved evidence; it is not a ZIP archive. The frontend also uses `/api/config`, `/api/runs`, `/api/runs/{id}`, the run's `/reviews` and `/packet` endpoints, and `/api/runs/{id}/assumptions/{assumption_id}/reviews`.

The supplied example intentionally contains a timing contradiction. Successful execution should retain that conflict for inspection while generating and compiling the model. Neither that demonstration nor the GUI establishes reduced engineer effort, faithful interpretation of arbitrary prose, or industrial readiness.

**Testing**

From the repository root:

```bash
python -m pip install -r requirements-review.txt httpx
python -m unittest discover -s tests -v
```

Tests cover units, solver evidence, compiler checks, source and behavior revisions, contract comparisons, review decisions, stale evidence, and packet exports. Checks requiring external tools or saved diagnostic fixtures skip when those prerequisites are absent.

### Native TLR preflight

Before LLM generation, the pipeline builds and strictly typechecks a heuristic requirements TLR. Numeric observations that cannot be bound to a numeric quantity are retained in `unresolved_ranges`, with the complete original clause, numeric mentions, and an explicit non-executable status. They are not cast onto Boolean requirement placeholders or presented as verified bounds. This preserves category-specific deadlines and population conditions such as “95% of messages” for explicit interpretation.

`initial_tlr.json` is saved before provider calls. A later generation failure therefore leaves the native candidate available for inspection; the GUI identifies this fallback as native preflight and partial. Unresolved numeric bindings appear in the assumption ledger and remain pending review. Invalid arithmetic on Boolean symbols still fails native typechecking.

### Candidate interpretation status

The LLM pipeline reports **Pending review** when a retained native TLR row and unique named SMT candidate exist, and **Needs interpretation** for missing or unresolved meaning. **Unsupported** remains the local grammar rejection. The typed-meaning table exposes exact candidate assertions and their referenced definitions. Existing runs receive a read-only display projection; their evidence and recorded approval state are preserved.

## Shared contract inspection

The **Contracts** tab connects exact source passages, versioned rules, typed contracts, assumptions, SysML declarations, and solver artifacts. To inspect the local example:

1. Choose **Load contract review example**. It selects **Check reviewed design** and supplies a candidate whose voltage stays at 27 V with a separate requirement limiting voltage to 28 V. Inspect the candidate, enter a reviewer and rationale, and explicitly acknowledge the design review before submitting. Loading the example does not approve it. This example requires no LLM request.
2. Open **Contracts** and select the voltage contract. Inspect its source, rule, property, assumptions, linked SysML declaration, and exact solver query/result. A successful bounded check applies only to the displayed scope.
3. Record separate assumption and contract decisions with engineering rationales. Each decision may accept, reject, or defer the interpretation; it preserves the source and evidence.
4. Choose **Propose a source or behavior revision** and select **Check reviewed design**. Change the source limit from 28 V to 29 V and the behavior property's predicate limit from `28` to `29`. Keep the candidate's `nominal` parameter at `27`, record the revision rationale, and complete a fresh design review acknowledgment before generating the child run.
5. Inspect the child's change comparison. Z3 can find a voltage above 28 V and at most 29 V that the new requirement permits. This witness describes a contract valuation; it need not be an execution of the current candidate, which satisfies both limits.
6. Investigate the source justification, operating context, and authority before approving the change. Final prototype acceptance requires current assumption and contract decisions, explicit acknowledgment of revision changes, and the other solver, compiler, and interpretation checks. Removing a contradiction does not establish which requirement was wrong.

To inspect a failed behavioral guarantee, change only `nominal` to `29` while retaining the 28 V source and property limit. Review and acknowledge that revised candidate in **Check reviewed design** mode. Z3 should report a counterexample. This changes the modeled context, so the revision comparison cannot establish equivalence under an unchanged context.

Runs created without a contract snapshot retain their original artifacts. Generate a new candidate to obtain contract evidence and decisions. Compilation checks SysML syntax, references, and well-formedness; neither compilation nor bounded Z3 evidence establishes stakeholder intent, architectural allocation, or unbounded liveness.
