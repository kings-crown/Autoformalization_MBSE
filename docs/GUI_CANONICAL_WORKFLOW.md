# GUI for the canonical requirements workflow

The main review application presents the same requirements-to-SysML implementation used by the canonical CLI. It calls `canonical_cli.run_candidate` with the selected source packet, condition, model, repair budget, fixed context, and development scenarios. The GUI adds saved runs, inspection views, separate evaluation jobs, and recorded engineering opinions; it does not add a second formalization or rewrite the generated SysML.

Sharing the conversion implementation does not establish GUI usability, reduced engineer effort, or empirical efficacy of the interface.

## Launch and storage

From the repository root in the project Python environment:

```bash
python -m pip install -r requirements-review.txt
python scripts/review_server.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The service binds to localhost. Stop the foreground service with Ctrl+C. Opening the HTML file directly does not connect it to the service.

Generation uses the configured Codex CLI; logical checks require Z3; compilation requires the configured SysML pilot compiler. Tool detection indicates availability, while each run records actual execution outcomes. Opening the application or inspecting a saved run makes no model calls.

New canonical runs are stored under `out/review_workbench/canonical` by default. With `--data-dir path/to/runs`, they are stored under `path/to/runs/canonical`. `MBSE_REVIEW_DATA_DIR` provides the alternative base-directory setting. Earlier saved runs and the former design-review application remain accessible at [http://127.0.0.1:8765/legacy](http://127.0.0.1:8765/legacy), using the base directory itself. They are not converted into canonical runs or overwritten.

## Three processes and two fixed handoffs

```text
  Requirements document: clauses, glossary, tables, figures
                              |
                              v
  +---------------------------------------------------------+
  | 1. PREPARE SOURCE                                       |
  | Source-check wording and collect relevant context        |
  | Retain definitions, conditions, exceptions, dependencies |
  | Record unresolved questions                              |
  +---------------------------+-----------------------------+
                              |
             Prepared requirements and source context
  ==================== FIXED HANDOFF 1 ======================
                              |
                              v
  +---------------------------------------------------------+
  | 2. CONVERT THROUGH THE CANONICAL CLI CORE                |
  |                                                         |
  | A: source -> LLM -> SysML -> compiler                    |
  |                                                         |
  | B/C: source -> LLM -> TLR -> type/unit checks             |
  |                        |                                |
  |                  shared expressions                     |
  |                   /             \                       |
  |                  v               v                      |
  |            SysML/compiler   C: SMT/Z3 audits             |
  |                   \             /                       |
  |                    optional feedback                    |
  |                           |                             |
  |        bounded proposal -> validate -> select/retain    |
  |        development scenarios and regression gate        |
  |        when explicitly enabled                          |
  +---------------------------+-----------------------------+
                              |
           Frozen selected candidate and attempt history
  ==================== FIXED HANDOFF 2 ======================
                              |
                              v
  +---------------------------------------------------------+
  | 3. EVALUATE SEPARATELY                                   |
  | Explicit mutation manifest -> formal comparisons        |
  | Changed sources -> separate declared C generation trials|
  | Keep outcomes, missing evidence, cost and latency        |
  +---------------------------+-----------------------------+
                              |
              Engineer inspection and recorded opinion
```

Source clarification starts a new input revision. Evaluation never changes the selected candidate or supplies its answers to the parent run's repair loop. Engineering review is a separate decision: it does not transform SAT or compilation into proof of stakeholder intent.

## Prepare and upload the source packet

Use a prepared JSON requirements packet for document-level work. Each requirement has an ID, its original text, and a `source` object containing the supplied citations and contextual passages. A top-level array or an object with a `requirements` array is accepted. Nested source context is preserved by the same importer used by the CLI.

```json
{
  "requirements": [
    {
      "id": "REQ_1",
      "text": "The framework shall support setting the multicast TTL.",
      "source": {
        "document": "Prepared demonstration",
        "section": "Specific requirements",
        "context": [
          {
            "id": "DEF_TTL",
            "text": "TTL denotes a multicast hop limit in this example."
          }
        ]
      }
    }
  ]
}
```

This example illustrates the input shape using constructed content. The [canonical fixtures](../examples/canonical/README.md) provide small runnable inputs.

Text and CSV imports remain available through the canonical importer. The GUI does not flatten a JSON packet to CSV before generation, so nested source definitions and qualifications remain available. The imported source view is the place to check that the supplied context is present. A CSV of isolated sentences cannot supply definitions that were never included.

Optional **fixed formal context** is a separate JSON object with `variables`, `background`, and optional `symbol_meanings`. It fixes the vocabulary and premises for the trial. Document context is broader: it may include obligations, alternatives, examples, and unresolved questions that must not all become SMT assumptions. Do not insert a required guarantee into `background` merely to make its violation impossible.

PDF transcription, figure interpretation, and context selection are preparation activities. Automatic PDF ingestion remains future work. The GUI accepts prepared sources; it does not certify that their transcription or interpretation is correct.

## Choose the generation condition and budget

| Condition | Actual conversion |
|---|---|
| A | LLM produces SysML directly; compiler checking follows. No TLR or Z3 audit is produced by this route. |
| B | LLM produces TLR; shared schema/type/unit checks and the deterministic SysML renderer run. No Z3 calls. |
| C | The structured route also runs Z3 audits and records the scoped machine-admission result. |
| BC | One structured candidate supplies the unchanged B and C views. C adds audits; it does not alter that candidate. |

Selecting B and C in separate GUI submissions does not create a paired study: separate calls can generate different candidates. Use BC for the unchanged-candidate comparison. Use the CLI `study` command for its controlled paired A/B/C orchestration. The GUI submits one run at a time; it does not claim that separately submitted runs form a matched experiment.

The GUI defaults both repair budgets to **zero**, making the selected policy explicit. The CLI standalone command has its own documented defaults; supplying the same options reaches the same implementation, but selecting different defaults or inputs does not create an identical trial. Inference can vary across separate calls even with identical settings.

Choose at most one repair policy:

- **Abstention recovery:** available for structured conditions, with a budget of zero to five rounds. Diagnosis and a separate proposal opportunity inspect unrepresented requirements under the shared abstraction policy. Missing context or genuinely unsupported meaning can remain unresolved.
- **General feedback refinement:** available for separate B or C candidates, with zero to five review/proposal calls. B receives source-based review; C additionally receives its current solver evidence. Accepted proposals regenerate both target encodings from the revised TLR. BC and A do not accept this policy.

A supplied TLR permits a structured artifact replay without an initial generation call. A supplied SysML artifact is eligible only for A. Positive repair budgets can still invoke the model; supplying an artifact does not make a repair-enabled run an offline run.

Development scenarios require **C with a positive general-feedback budget**. Upload an explicit source-grounded scenario suite. The canonical scenario validator, query builder, diagnostics, and regression gate are reused. Previously passing scenarios must not regress for a proposal to be selected. Scenario results are development assistance, with their expected outcomes exposed to refinement; they are not independent final evaluation.

The shared abstraction policy, static TLR profile, type/unit normalization, compiler invocation, solver queries, and correction rules are unchanged by the GUI transport. Capability availability, state constraints, and relations for a selected occurrence retain their declared limits. The GUI does not create an implicit transition-system design or claim temporal verification from scalar consistency.

## Inspect the result

Inspect the prepared source, typed meanings, abstraction scopes, generated SysML, logical queries, compilation diagnostics, and attempt history. Download the selected `canonical/model.sysml` to inspect the actual artifact. The GUI retains the renderer's output without a display-driven model rewrite.

| Displayed result | Meaning |
|---|---|
| Run completed | The workflow executed; inspect its individual outcomes. |
| Partial representation | Some requirements remain unsupported or unresolved. |
| Supported requirement | The declared profile contains an executable abstraction; fidelity is still a separate question. |
| Supported-set SAT | The represented formulas are jointly satisfiable under their declared background. |
| UNSAT | That exact query is inconsistent; it does not identify which source requirement is authoritative. |
| Unknown/error/not run | No conclusive result for that query; it must not become a pass. |
| Compilation passed | The configured compiler accepted the generated model. |
| Admission withheld | Prerequisites for the canonical complete-encoding admission policy were not all met. |
| Admitted consistent encoding (`admitted_consistent_encoding`) | The policy's coverage, consistency, and compilation prerequisites hold; this is not engineer approval. |

For a voltage requirement `voltage <= 28`, a violatability witness of 29 asks what is possible under the background when the target requirement is deliberately excluded. It is not a satisfying value for that requirement or a prediction of a generated battery design. Read each query's purpose before interpreting its witness.

Raw artifacts and diagnostics remain inspectable for failed or partial runs. Service interruption preserves partial files and records the interrupted run; it does not automatically repeat paid generation. Browser edits that have not been submitted are not saved run artifacts.

## Separate mutation jobs

Upload an explicit `mutation_campaign/1` manifest. Its requirement list must preserve the selected run's **complete source packet**, including unchanged contextual clauses, IDs, ordering, wording, and nested source records. The manifest supplies its own declared comparison vocabulary, background, reference formulas, variants, and reference-review status.

The interface records whether the manifest's variables and background match a parent fixed context, differ from it, or have no parent fixed context to compare. This comparison does not certify equivalence of prose symbol meanings. A mismatch is a visible difference in experimental conditions; it is not automatically repaired by renaming variables or altering assumptions. Source-context equality and formal-context agreement are separate checks.

| Job | What executes | Interpretation |
|---|---|---|
| Formal mutation comparison | Reference formulas in the manifest versus explicit AST edits, using Z3 | No model calls. It tests the declared formula differences; it does not read back or verify the selected SysML. |
| Source mutation campaign | One new baseline plus changed/control source bundles, through separately declared C workflows | New generation calls with fixed manifest vocabulary and explicit budgets. It evaluates mutation preservation in those new trials. |

A source campaign always records its distinct condition-C policy, generation model, one repetition, invocation cap, and repair budget. It does not replay the selected parent candidate or inherit its development scenarios. A scenario-assisted parent therefore does not make the campaign scenario assisted. Final reference formulas, labels, expected mutation results, and comparison witnesses stay outside each generation/repair prompt.

The source campaign's maximum workflow count is one plus the number of variants that contain changed source text. Its maximum generation-model calls are:

```text
workflow count * (1 + feedback repair budget + 2 * abstention budget)
```

The source-campaign form and HTTP API expose the general-feedback budget and the alternative abstention-recovery budget. Only one may be positive. The interface shows the declared budgets before execution and retains the effective policy with results. Formal mode has zero model calls; source-mode generation usage or cost can remain unavailable and must not be reported as zero.

Mutation equivalence concerns the declared formulas and context. Detecting a difference does not prove that either formula preserves stakeholder intent. These jobs cannot change the parent candidate, its repair history, or its machine-admission result.

## Record an engineering opinion or create a revision

A reviewer can append a recommendation, deferral, or approval opinion with a name and rationale after conversion stops. Review entries remain separate from automatic admission and independent evaluation. An approval entry does not change failed checks, add missing assumptions, repair the model, or authenticate organizational sign-off.

There is no requirement to approve every assumption or provide hash attestations before running the canonical workflow. Its inspection artifacts expose assumptions for engineering consideration. The earlier compatibility interface retains its own review rules under `/legacy`; those rules do not become prerequisites for canonical generation.

Create a new run for a changed source, context, TLR, or policy. A parent ID and revision rationale can connect the records. Preserve the earlier result so an engineer can inspect why the new interpretation was proposed. A recorded review never authorizes silent source amendment.

## Implementation and HTTP appendix

| Responsibility | Implementation |
|---|---|
| Service and routing | [review_server.py](../scripts/review_server.py) |
| Canonical GUI transport and run store | [review_canonical.py](../scripts/review_canonical.py) |
| Conversion implementation shared with CLI | [canonical_cli.py](../scripts/canonical_cli.py) |
| Independent GUI job orchestration | [review_evaluation.py](../scripts/review_evaluation.py) |
| Mutation comparisons and campaigns | [mutation_stress.py](../scripts/mutation_stress.py), [mutation_sources.py](../scripts/mutation_sources.py) |
| Main page | [canonical.html](../prototypes/review-workbench/canonical.html) |
| Earlier compatibility page | [index.html](../prototypes/review-workbench/index.html) |

The main interface uses these endpoints:

```text
GET  /api/workflow/config
GET  /api/workflow/runs
POST /api/workflow/runs
GET  /api/workflow/runs/{run_id}
GET  /api/workflow/runs/{run_id}/artifacts/{relative_path}
GET  /api/workflow/runs/{run_id}/packet
POST /api/workflow/runs/{run_id}/reviews
POST /api/workflow/runs/{run_id}/evaluations
GET  /api/workflow/runs/{run_id}/evaluations/{evaluation_id}
```

Conversion accepts source `text` and `format`, plus explicit `condition`, `model`, `context`, `feedback_repairs`, `abstention_repairs`, and optional `development_scenarios`, supplied `tlr` or A-only `sysml_text`, and parent/revision fields. Evaluation uses `kind: mutation_formal` or `mutation_source` and an explicit manifest. Unknown fields and incompatible combinations are rejected before execution.

A canonical run contains imported `sources.json`, its submitted request, a `canonical/` directory of CLI artifacts, optional review records, and separate `evaluations/eval-.../` snapshots and results. The JSON packet export provides inspectable files; it is not an independent preservation certificate. Artifact access rejects paths outside the run directory and symlink traversal, without requiring historical hashes to match.

Earlier endpoints under `/api/...` remain for the compatibility interface and the explicit CLI `review` command. They do not route new main-page requests through the earlier engine. A standalone canonical CLI directory is not automatically imported into the GUI run list; the shared implementation concerns conversion semantics, not an identical storage wrapper.

For interpretation details, see [canonical CLI](CANONICAL_CLI.md), [feedback refinement](SOLVER_FEEDBACK_REPAIR.md), [development scenarios](DEVELOPMENT_SCENARIO_REFINEMENT.md), and [mutation stress tests](MUTATION_STRESS_TEST.md).
