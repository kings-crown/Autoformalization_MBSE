# Canonical requirements-to-SysML CLI

The CLI implements an LLM-to-TLR-to-SysML path with deterministic symbolic audits. Its central representation is `mbse_tlr/1`: one typed expression AST feeds both the SysML renderer and the SMT encoder. This replaces the earlier CLI path in which heuristic TLR metadata preceded independently generated SMT and inferred model structure.

This guide describes executable behavior. No research fidelity result follows from implementing the controller or passing its tests; independent semantic assessment remains separate.

## 1. Conditions and generation policy

| Condition | Model call | Static validation | SysML rendering | Z3 | Compiler |
|---|---|---|---|---|---|
| A | Direct SysML | No hidden TLR | Model output | Off | Same compiler policy |
| B | Executable TLR | Schema, symbols, types, units, profile, source IDs | Fixed renderer | Off | Same compiler policy |
| C | Executable TLR | Same as B | Same renderer | Audits | Same compiler policy |
| BC | One executable TLR call | One shared candidate | One shared model | C view only | One shared outcome |

The default condition is C. With `--feedback-repairs 0`, `BC` is the paired audit comparison: both arm views point to the same `tlr.json` and `model.sysml`. The default `study` generates A and BC for each repetition and alternates their execution order. With positive feedback repair, `study` instead generates one shared initial TLR and forks independent B source-review and C Z3-feedback branches. Separate standalone B and C generations do not provide that shared initial control; positive feedback repair rejects standalone `BC`.

Each generation path permits one initial model call. The `study` batch command defaults to zero semantic repairs; generated structured standalone runs additionally allow bounded diagnosis and recovery of abstained clauses by default. An explicit `--feedback-repairs K` selects a separate source-review/Z3-feedback policy that can revise supported formulas. The two positive repair budgets cannot be combined. The defaults and guards are specified below. Invalid initial JSON, types, units and expression operators remain generation failures. The CLI does not soften source requirements or add assumptions to obtain SAT. Fence removal is a formatting operation; raw model responses remain recorded.

All generation paths receive the same versioned [abstraction policy](ABSTRACTION_POLICY.md). A source-grounded capability predicate can represent required feature availability; it cannot stand for actual delivery, temporal behavior or implementation realization. New TLR records declare `state_constraint`, `capability` or `event_relation` with their meaning, scope and limitations. A policy label is structural metadata, not a source-fidelity proof.

## 2. Setup and commands

Run commands from the repository root. Install `requirements-review.txt`, configure and authenticate the Codex CLI, and provide `z3` on PATH. The compiler adapter uses the installed SysML pilot compiler; see [compiler setup](../prototypes/review-workbench/README.md). The usual compiler settings are `SYSML_JAVA`, `SYSML_KERNEL_JAR`, and `SYSML_LIBRARY_DIR`.

A new generated run invokes the configured LLM:

```bash
python scripts/requirements_pipeline.py run \
  --statement requirements.csv \
  --condition C \
  --output-dir out/requirements_c \
  --model "$MODEL_ID"
```

The `run` keyword can be omitted. Input can be CSV, TXT, or JSON. CSV must have an accepted requirement-text column; JSON can be a requirement array or an object containing `requirements`. Imported IDs, source text, and source locations are retained. PDF extraction is not part of this command.

Prepare JSON or CSV containing the original clauses, identifiers, source locations, and necessary context. Conversion preserves this input packet; changes to source meaning require a separate input revision. Automated PDF extraction is outside this command.

Useful arguments:

| Argument | Behavior |
|---|---|
| `--condition A\|B\|C\|BC` | Select the intervention for a single run; default C. |
| `--model` | Explicit generation model; otherwise uses installed configuration and records the choice. |
| `--context-file` | Fixed `{variables, background}` and optional `symbol_meanings` for prompts and structured-output validation. |
| `--tlr-file` | Supply a TLR to B/C/BC without an initial generation call; recovery defaults to zero. |
| `--abstention-repairs K` | Permit 0–5 abstention diagnosis/recovery rounds; generated structured `run` defaults to 2 unless feedback repair is selected, supplied TLR and `study` to 0. |
| `--feedback-repairs K` | Permit 0–5 semantic review/proposal rounds; default 0. Standalone B uses source review, C adds Z3 evidence; `study` forks one initial TLR into separate B/C branches. Mutually exclusive with positive abstention recovery. |
| `--development-scenarios PATH` | Optional `development_scenarios/1` suite with source-grounded expected SAT/UNSAT cases. Requires positive feedback repair; standalone C only, or the C branch of `study`. Adds scenario feedback, a no-regression gate and formal change evidence. |
| `--sysml-file` | Supply a SysML candidate to A without a generation call. |
| `--skip-compile` | Record compilation as not run; C cannot claim admission. |
| `--solver` | Z3 executable name/path. |
| `--timeout-seconds` | Per-solver-call timeout; default 10 seconds. |
| `--output-dir` | Fresh output directory. Existing directories are not overwritten. |

A fixture run makes no LLM call:

```bash
python scripts/requirements_pipeline.py run \
  --statement examples/canonical/requirements.json \
  --tlr-file examples/canonical/tlr.json \
  --condition BC \
  --output-dir out/canonical_fixture
```

A paired batch invocation:

```bash
python scripts/requirements_pipeline.py study \
  --statement requirements.csv \
  --context-file path/to/context.json \
  --repetitions 5 \
  --model "$MODEL_ID" \
  --output-dir out/abc_study
```

Omit `--context-file` for exploratory formalization without a frozen evaluation vocabulary. For a controlled comparison, provide the context before generation. The study supports `--tlr-file` and `--a-sysml-file` for offline orchestration fixtures; repeating supplied artifacts is not repeated stochastic generation.

### Bounded abstention recovery

The current policy is `source_grounded_abstention_recovery/2`. An abstention is a generated `unsupported` or `unresolved` record. It can reflect a real expressiveness limit, missing source context, or failure to use a permitted abstraction. **Diagnosis is advisory: it cannot prevent an otherwise eligible abstention from reaching the proposal stage.** Every current abstention receives a separate consideration of all three permitted abstraction kinds.

| Entry point | Default `--abstention-repairs` | Consequence |
|---|---:|---|
| Generated structured `run` in B/C/BC, without positive feedback repair | 2 | At most two diagnosis/proposal rounds after a valid initial TLR. |
| `run --tlr-file ...` | 0 | Supplied artifacts cause no implicit paid recovery calls; an explicit positive budget opts in. |
| `study` | 0 | No implicit recovery calls; an explicit positive budget enables recovery. |
| Mutation `source` | 0 | Preserve the frozen converter's no-repair policy unless explicitly changed. |
| Direct SysML A | 0 | This TLR controller does not change A; a positive standalone budget is rejected. |

`--abstention-repairs K` accepts zero to five. Each entered round allows one diagnosis invocation and one proposal invocation. Generated structured runs therefore permit at most `1 + 2K` controller transport invocations; supplied TLRs omit the initial call. Provider-internal requests and billing may differ. Actual calls and early stopping remain recorded.

```text
Fixed source/context -> valid initial TLR
                                  |
                         Any abstained clauses?
                            /             \
                          no              yes
                          |                |
                          |        Advisory diagnosis
                          |        valid / failed / malformed
                          |                |
                          |                v
                          |        Separate proposal review
                          |        EVERY current abstention
                          |        consider all three rules
                          |                |
                          |        proposal or concrete blocker
                          |                |
                          |        static + frozen-content guard
                          |                |
                          |        progressing candidate compiles?
                          |                |
                          +----------------+
                                  |
                    Last accepted candidate + all attempt evidence
                                  |
                       SysML and C-only Z3 evidence
```

The diagnosis uses the configured generation model, fixed source packet, current TLR and abstraction policy. It can recommend `repair`, `retain_unsupported` or `needs_clarification`. Its source quotations are validated and its recommendations are retained as advice. A failed or malformed diagnosis is logged; the separate proposal call still runs without that advice. A valid diagnosis recommending that every clause be retained likewise cannot stop proposal review. The proposal can select any permitted abstraction supported by its own source-grounded explanation, including overriding the diagnosis.

The proposal response is an `abstention_proposal/1` envelope:

| Field | Required content |
|---|---|
| `schema` | `abstention_proposal/1` |
| `tlr` | Complete proposed `mbse_tlr/1` candidate, preserving the source inventory. |
| `reviews` | Exactly one review for every currently unsupported/unresolved ID, including retained clauses. |
| Review `outcome` | `proposed` when the candidate recovers that clause, otherwise `retained`. |
| Review `considered_rules` | A rationale for each of `capability`, `state_constraint` and `event_relation`. |
| Review `reason` and `source_basis` | Explanation and literal evidence including the target's own source wording. |
| Review `blocking_detail` | A concrete nonempty obstacle for a retained clause; `null` for a proposed recovery. |

For model transport, the complete immutable `source_packet` carries requirement wording and context once. The model-facing `accepted_tlr` omits only each requirement record's repeated `text` and `source` fields; IDs, formulas, abstraction metadata, statuses/reasons, variable definitions and assumptions remain present. Proposal TLR records are instructed to omit the same duplicated fields. Before the full normalized preservation guard runs, the controller restores source metadata by ID from the immutable packet; any supplied conflicting source text/context is rejected before rebinding. This reduces repeated input/output text without dropping source context or weakening the guard. Raw responses and full normalized artifacts remain saved.

A generic “needs clarification” does not discharge the proposal review. It must examine whether feature availability or an opaque address relation already preserves the required abstraction level, and explain why missing information would change the obligation when retaining the clause. Preparer-authored `unresolved_interpretation_questions` are nonbinding review notes, not additional requirements or commands to abstain. The underlying quoted source still governs. This process does not force temporal ordering, automatic processing, cryptographic correctness or unspecified compatibility into unsupported static surrogates.

The guard freezes the source and fixed context, existing variable identities/meanings/types/domains, background assumptions and all previously supported records. Retained abstention records remain unchanged; their additional reasoning belongs in the proposal review. With fixed context no symbol may be added. Without it, a new unbounded variable must have an explicit meaning and be used by a recovered requirement. A diagnosis does not restrict which permitted abstraction the proposal may select.

Selection requires a valid proposal envelope, a statically valid and guard-compliant TLR, an increased supported-record count and successful compilation when enabled. Explicit `--skip-compile` does not block recovery but prevents C admission. Invalid proposals consume their attempted round and remain recorded; remaining budget may use their validation diagnostic. If a valid complete proposal retains every current abstention, stop as `no_progress_after_proposal`. Full support and exhausted budget also stop. Enabled initial compilation failure stops without recovery calls; proposal compilation failure rejects the candidate and stops further reinterpretation. Rendering/checking exceptions remain tool failures. Invalid initial TLRs remain initial generation failures, and legacy supplied TLRs requiring explicit conversion stop without paid migration. A no-progress proposal preserves its normalized TLR and reviews without another render/audit.

No Z3 finding, compiler diagnostic, final judge response or held-out mutation result enters the diagnosis/proposal prompt. Compilation is an artifact-eligibility gate and SAT/UNSAT is not a recovery selection criterion. Both internal calls use the generation model: a second call is a separate task, not independent final validation. Review-schema compliance and quoted evidence do not prove source fidelity.

For an explicit zero-repair baseline:

```bash
python scripts/requirements_pipeline.py run \
  --statement requirements.json --condition BC \
  --abstention-repairs 0 --output-dir out/bc_baseline --model "$MODEL_ID"
```

For a separately identified recovery treatment:

```bash
python scripts/requirements_pipeline.py run \
  --statement requirements.json --condition BC \
  --abstention-repairs 2 --output-dir out/bc_recovery --model "$MODEL_ID"
```

`repair.json` records the policy, `diagnosis_advisory: true`, budget, diagnosis/proposal counts, accepted recoveries, source-ID inventories, selected attempt and stopping reason. Each step records all current `eligible_ids`, advisory `diagnosis_recommended_ids`, and `diagnosis_error` when advice fails. `overridden_diagnosis_ids` counts accepted recoveries contrary to valid retain/clarify recommendations; absent or failed advice is not counted as an override. Attempt files retain raw/validated diagnosis, the raw proposal envelope and `proposal.json`, extracted `candidate_tlr.json`, `proposal_reviews.json`, and available normalized/compiled/audited artifacts. Root `generation_response.txt` remains the initial response; root candidate TLR and SysML files identify the selected attempt.

Historical `source_grounded_abstention_recovery/1` runs can end as `no_recoverable_abstentions`: that means the diagnosis selected no repair targets and no proposal followed. It is not proof that the source cannot be formalized. Revision 2 supersedes that stopping rule. Existing results keep their recorded version and are not retrospectively relabeled or regenerated by this code change.

Under this abstention policy, B/C share the selected recovered TLR and SysML. B runs no Z3 queries; C adds audits of that same candidate. Enabling abstention recovery adds computation before the B/C distinction and does not isolate the contribution of Z3. The separate feedback policy below permits supported-formula correction and a matched B/C comparison. Independent SysML preservation remains future work.

### Matched source-review and Z3-feedback repair

`--feedback-repairs K` enables policy `source_grounded_semantic_feedback/1`, defaulting to zero. It permits one review/proposal call per round, up to K rounds; each response reviews all requirements, including supported formulas. B sees the fixed source/context, current TLR, abstraction policy and previous local parse/guard errors. C additionally sees its current Z3 audit findings and selected exact query/result/witness payloads. All check statuses/purposes/artifact references remain listed; deterministic selection prioritizes global and finding/inconclusive checks, then violatability, up to 64 full checks. Selection and omitted payloads are recorded, and complete audits remain saved. Judge verdicts, reference formulas and held-out mutation results are excluded.

```text
Shared initial TLR -> B: source review ----> B final TLR -> shared renderer
                  \-> C: source + Z3 -----> C final TLR -> shared renderer
                          ^        |
                          +--------+  up to the same K proposal calls per branch
```

The source inventory/context, existing symbol meanings/types/domains and assumptions remain fixed. Supported formulas can change, and support regressions are recorded. Selection accepts the latest structurally valid, source-grounding-valid revision that passes enabled compilation; it does not require SAT or increased support. Genuine source conflicts can remain UNSAT. No-change reviews stop; malformed or guard-invalid proposals consume a round and may retry within the remaining budget. Initial compiler failure blocks semantic review; proposal compiler or checking-tool failures stop and retain the selected candidate.

Positive feedback repair disables the implicit standalone abstention default. Explicit positive values for both repair options are rejected. Standalone feedback runs allow B or C, not A or BC. Use `study` for batch execution from one shared initial candidate:

```bash
python scripts/requirements_pipeline.py study \
  --statement requirements.json --context-file path/to/context.json \
  --model "$MODEL_ID" --repetitions 5 \
  --abstention-repairs 0 --feedback-repairs 2 \
  --output-dir out/abc_feedback
```

A remains one direct generation. Per repetition, the controller permits at most two initial calls (A and shared TLR) plus `2K` proposal calls across B/C. Actual calls and token/latency differences remain measured. `feedback_repair.json` and `feedback_attempts/000`, `001`, etc. retain each branch's selection and history; they are distinct from abstention recovery's `repair.json` and `attempts/`.

See [solver-feedback repair](SOLVER_FEEDBACK_REPAIR.md) for commands, exact boundaries, failure handling, artifacts and study claims. The default audit-only study and previously saved results are unchanged; this option does not establish that Z3 feedback improves source fidelity.

### Development scenario feedback

`--development-scenarios PATH` augments a positive-budget C feedback run with a prepared `development_scenarios/1` suite. Each case supplies target IDs, a typed predicate, an expected `sat` or `unsat`, literal source support and a rationale. Its preparation origin is recorded; `llm_prepared` does not mean independently reviewed by a human. In `study`, A and B do not receive the suite, so this assistance changes the C treatment explicitly.

```bash
python scripts/requirements_pipeline.py run \
  --statement requirements.json --tlr-file shared_initial_tlr.json \
  --condition C --model "$MODEL_ID" \
  --abstention-repairs 0 --feedback-repairs 2 \
  --development-scenarios study/development/scenarios.json \
  --output-dir out/c_development_feedback
```

For background `Gamma`, supported candidate conjunction `M` and scenario `S`, the checker establishes `Gamma`, `Gamma AND M`, and `Gamma AND S` are SAT before comparing `Gamma AND M AND S` with the expected outcome. Targets without executable representations remain unsupported; missing symbol bindings are `not_run`. SAT means a permitted partial valuation has at least one completion, not that an action is required or eventually occurs. Unknowns and infeasible premises remain explicit.

Each changed proposal reruns the suite. Losing a previous pass or producing a blocked, inconclusive or invalid candidate prevents selection; existing unresolved gaps can remain visible. Source/context, assumptions and existing variable meanings stay frozen. A new source-grounded symbol may be added only where the existing feedback policy permits it; definitions supplied by the suite are declared extra assistance. Formal old/new comparisons use domains/background without assuming either target guarantee. Changes in vocabulary or unavailable formulas remain uncomparable.

`development_suite.json` stores the fixed suite. The initial and changed attempts retain `development_scenarios/scenarios.json` with exact query/result/witness artifacts; changed proposals also retain `development_gate.json` and `semantic_comparison/semantic_changes.json`. The feedback ledger records initial/final development results. Final judges, held-out references and mutation scores remain outside repair. See [development scenarios](DEVELOPMENT_SCENARIO_REFINEMENT.md) for the complete semantics and paired-run protocol.

## 3. The executable representation

Example of a newly generated record (older supplied `mbse_tlr/1` files remain readable as legacy/unclassified):

```json
{
  "schema": "mbse_tlr/1",
  "abstraction_policy": "mbse_abstraction/1",
  "variables": [
    {
      "name": "battery_voltage",
      "type": "Real",
      "unit": "V",
      "description": "Battery terminal voltage in volts."
    }
  ],
  "assumptions": [],
  "requirements": [
    {
      "id": "R1",
      "status": "supported",
      "formula": {
        "op": "<=",
        "args": [
          {
            "var": "battery_voltage"
          },
          {
            "value": "28",
            "unit": "V"
          }
        ]
      },
      "abstraction": {
        "kind": "state_constraint",
        "meaning": "Battery terminal voltage is at most 28 V.",
        "scope": "One system observation.",
        "limitations": [
          "No battery dynamics or implementation proof."
        ]
      }
    },
    {
      "id": "R2",
      "status": "unsupported",
      "reason_code": "profile_limit",
      "reason": "Eventual completion requires temporal semantics outside this profile."
    }
  ]
}
```

The submitted source must contain exactly these IDs. The validator adds authoritative source text to the normalized records, rejects changed wording if the model supplies it, and rejects missing/extra/duplicate IDs. A source ID is an association, not evidence that every clause was preserved correctly.

Variables have a name, primitive type `Bool`, `Int`, or `Real`, an optional unit, optional numeric `lower`/`upper` domain bounds, and a description of the symbol meaning. Descriptions are required for symbols referenced by new declared abstractions; legacy supplied artifacts may omit them. Domain bounds and `assumptions` are background premises. Source obligations belong in requirement formulas. Moving “voltage ≤ 28” into the variable's upper bound would make its own violatability check vacuous; the background-entailment diagnostic helps expose that effect but cannot judge whether a premise was justified.

Requirements have `supported`, `unsupported`, or `unresolved` status. Supported records need a Boolean `formula`; other records need a reason and cannot carry an executable formula. New records also carry the shared policy version: supported rows need `abstraction` metadata; unsupported rows use reason code `profile_limit` or `resource_limit`, while unresolved rows use `source_ambiguity` or `missing_context`. These codes distinguish a tool limitation from a question about the source. Unexplained source-named Boolean placeholders and whole-formula `true`/`false` substitutes are rejected. This catches specified structural shortcuts, not every semantically empty or misleading expression.

The AST supports:

- Boolean `and`, `or`, `not`, and `implies`.
- Equality/inequality and ordered numeric comparisons.
- Linear `+`, unary/binary `-`, and multiplication with a fixed dimensionless literal factor.
- Typed conditional expressions through `ite`.
- Declared variable references, Boolean literals inside expressions, and exact decimal quantities.

Numeric values use canonical units and exact arithmetic. Floating-point JSON values, raw SMT, code, division, nonlinear products, quantifiers, functions, and next-state references are rejected. Compatible units are normalized before either encoder runs. Integer and real compatibility follows the shared validator's arithmetic rules; this is mathematical arithmetic, not machine overflow or floating-point semantics.

Current limits: 24 variables, 40 explicit assumptions, expression depth 16, Boolean conjunction/disjunction arity 2–16, and a 1,600-node validation budget. File ingestion has its own limits. The representation's limit on requirement records does not enlarge the importer limits. An all-unsupported packet may have no variables; the auditor then reports unsupported scope without introducing artificial symbols.

For a fixed-context study, `--context-file` supplies variables and `background` assumptions. Both generation prompts receive that context. B/C output must match its normalized vocabulary and background; a changed context fails validation. A is given the same context in its prompt, but its adherence is not independently extracted from SysML. The optional `symbol_meanings` object maps every variable name to its fixed natural-language definition. Both generation routes receive those definitions. B/C must preserve them verbatim in variable descriptions; changing a definition fails validation. This freezes the vocabulary interpretation without supplying the target formulas. A receives the same definitions, but its adherence is assessed from its actual SysML rather than an independent parser. Outside fixed-context mode, generated assumptions and symbol meanings remain proposed interpretations.

Three [synthetic messaging fixtures](../examples/canonical/message_abstractions/README.md) exercise sending capability, a recipient relation for a selected occurrence, and hop-limit configurability. They are authored regression inputs, not model-generated outputs or approved system requirements.

## 4. What the audits mean

Let `Gamma` be the declared variable domains and explicit environmental assumptions. Let `rho_i` be each supported requirement and `M = Gamma AND all rho_i`.

| Check | Query | Interpretation |
|---|---|---|
| Background feasibility | `Gamma` | UNSAT means the premises conflict before any requirement is added. |
| Joint consistency | `M` | UNSAT means the supported encoding conflicts under a feasible background. |
| Conditional trigger | `Gamma AND p_i` | UNSAT means the implication's trigger is unreachable under the background. |
| Trigger under specification | `M AND p_i` | Only run after `M` is SAT; UNSAT means the full specification excludes this trigger. |
| Violatability | `Gamma AND p_i AND NOT q_i`, or `Gamma AND NOT rho_i` | SAT gives a static valuation excluded by the requirement. |
| Redundancy context | `Gamma AND all rho_j for j != i` | Establish feasibility before interpreting redundancy. |
| Redundancy | Previous context `AND NOT rho_i` | UNSAT means the feasible remaining specification already entails the target. |

Only a root `implies` is interpreted as a conditional for trigger checks. Nested guards are not separately extracted. Unsupported clauses remain listed and are excluded from the executable conjunction; the report identifies the exact supported subset.

SAT violatability is often expected: it shows that the requirement excludes something the background otherwise permits. It is not a failure of a proposed design. If the background alone entails the requirement, the report flags that fact for inspection. A trigger impossible under the background is distinguished from an otherwise meaningful consequence entailed by the background.

An inconsistent or inconclusive background blocks downstream interpretation. An inconsistent remaining specification cannot justify a redundancy claim. An inconsistent complete model cannot justify an in-model trigger claim. These prerequisites prevent contradiction from being mistaken for successful verification.

`unknown`, `timeout`, `solver_error`, `encoding_error`, `unsupported`, `blocked`, and `not_applicable` remain distinct from SAT/UNSAT. The aggregate audit status is `passed`, `findings`, `inconclusive`, or `unsupported`; individual results retain more detail. A finding can coexist with a satisfiable complete encoding.

No artificial `(assert false)` is appended. There is no independent hazard-verification query over an inferred battery/controller design. Each SMT assertion comes from the validated context or a declared audit formula.

## 5. SysML and the admission decision

The renderer emits a shared subject definition with typed scalar attributes, background constraints, source-linked requirements, and actual `require` constraints for supported formulas. Unsupported clauses are documentation-only records with explicit reasons. The renderer does not infer ports, physical architecture, behavioral implementation, or `satisfy` claims that were absent from the TLR.

Numeric attributes are canonical scalar magnitudes with documented physical units. Unit checking happens in the shared AST; the model does not claim an independent SysML physical-quantity proof. Compilation checks the emitted artifact with the installed compiler and preserves diagnostics.

The same AST drives SMT and SysML rendering, reducing opportunities for independent translation drift. It is still possible for a renderer or solver encoder to contain a bug. Independent SysML read-back and semantic equivalence checking are future D, not implied by compiler acceptance.

C reports `admitted_consistent_encoding` only when:

1. Every source requirement is represented as supported.
2. The background and complete supported conjunction are SAT.
3. SysML compilation passed.

This is the controller's machine-admission policy. Redundancy and vacuity are reported without automatically rejecting a satisfiable candidate. Unknown diagnostic probes remain visible even where the declared admission prerequisites are satisfied. Admission does not approve the requirement interpretation or the design; engineer review remains a separate decision.

## 6. Saved evidence and completion

A run writes plain files as stages become available:

```text
run/
  sources.json
  context.json                  # when supplied
  configuration.json
  generation.json               # when a model call occurs
  generation_response.txt       # structured generation response
  candidate_tlr.json            # supplied/raw structured candidate
  tlr.json                      # normalized supported/unresolved records
  representation.json           # B/C counts and abstraction kinds; not semantic coverage
  candidate_content.json        # all conditions: lexical require-constraint presence
  model.sysml
  compilation.json
  analysis.json
  audit/                        # C/BC only
    tlr.json
    background.smt2
    background.json
    consistency.smt2
    consistency.json
    requirement_0001_*.smt2
    requirement_0001_*.json
    *_witness.smt2
    *_witness.json
    audit.json
  repair.json                   # recovery policy, decisions, selection and stopping reason
  attempts/                     # initial candidate and recovery proposals when recorded
    000/                        # initial raw/normalized candidate and applicable evidence
    001/                        # first proposal; later proposals use subsequent numbers
  result.json
```

A run in A has no TLR/audit artifacts; B has no solver queries. When positive-budget recovery is eligible to begin, `attempts/000/` and subsequent numbered directories keep the initial and proposed artifacts with their raw responses, validation outcome, available normalized TLR, SysML, compiler result and C-only audit evidence. Root-level candidate files represent the selected accepted attempt; `repair.json` identifies the selection. Invalid proposals retain partial evidence rather than receiving invented compilation or solver outcomes. Zero-repair runs keep the earlier root layout and a disabled `repair.json`; they do not create an artificial attempt history. Failure can leave a partial set. Every executed audit records source IDs, purpose, solver version, timeout, output, and exact query/result filenames. Paths and source associations suffice to run and inspect the prototype; no hash, source-authority attestation, provenance certificate, or review approval is required.

The default study adds `study_configuration.json`, per-repetition `A/` and `BC/` directories, `progress.json`, `study.json`, and `report.md`. A positive-feedback study instead saves `rep-001/initial/`, `A/`, `B/` and `C/`, with separate branch histories under `feedback_attempts/` and ledgers in `feedback_repair.json`. Each selected C candidate has regenerated audit evidence. The report counts candidates, failures, compilation, and C admission; it does not invent a fidelity score from those machine outcomes.

Exit 0 means the command completed; inspect `admission`, compiler outcomes, and audit statuses. A completed UNSAT run is not a passed model. `representation.formalization_status=no_executable_formalization` means no supported constraint was generated, even when a documentation-only SysML file compiled. `partial` counts supported records, not independently validated semantic completeness. Failures retain errors and available artifacts. Generation logs include model, prompts, responses, latency, and failures. Token counts and estimated cost are `null` where transport usage is unavailable, not zero.

## 7. Mutation evaluation

The [mutation stress test](MUTATION_STRESS_TEST.md) uses the same AST and context. Formal campaigns mutate a reference expression directly; source campaigns replace one requirement's wording, retain the bundle context, and regenerate through condition C. Generated `mbse_tlr/1` formulas can now be extracted automatically for comparison under the fixed campaign vocabulary/background. Historical native TLR formats still require explicit normalization/replay; that limitation does not apply to the new path.

Comparisons distinguish changes from the reference, correct preservation of the expected mutation, baseline errors, and changes to untouched clauses. Equivalent controls remain separate. These comparisons do not modify the selected conversion candidate.

## 8. Scope and follow-up work

The CLI currently formalizes static Boolean and linear numeric relationships. Temporal persistence, eventual response, probabilistic deadlines, quantified populations, and dynamic robustness must remain explicitly unsupported or unresolved. The older optional finite design checker is available through the existing review workflow; it is not silently invoked by the new static CLI.

Automated PDF ingestion and requirement extraction are separate upstream future work. The current workflow begins with a prepared contextual source packet. Independent semantic reconstruction of generated SysML is also future work.

The main GUI uses the canonical CLI conversion implementation; see the [GUI guide](GUI_CANONICAL_WORKFLOW.md). The explicit `requirements_pipeline.py review` command and `/legacy` page preserve the earlier review workflow.

The CLI exports candidate models, source context and diagnostics for separate semantic assessment. Solver results and compilation do not establish source fidelity or engineer approval.
