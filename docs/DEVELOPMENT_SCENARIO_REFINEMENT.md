# Development scenarios for source-grounded refinement

The CLI can use a declared development scenario suite to make bounded semantic repair more informative. A scenario states a concrete or partial valuation and whether the contextual requirements should permit it. Z3 checks the candidate against that expectation. Proposed revisions retain source evidence, before/after scenario outcomes, and formal difference examples. A regression guard prevents replacing the selected candidate when a previously passing development scenario stops passing.

This extends the existing solver-feedback controller. Z3 remains the solver, TLR remains the shared representation, and the deterministic renderer still produces SysML. The development suite is additional input to conversion; it is separate from final judging and held-out mutation assessment. Successful development tests establish agreement with the supplied scenarios, not comprehensive source fidelity or engineer approval.

## Rule errors and interpretation errors

Refinement distinguishes incorrect formal rules from ambiguous mappings between source language and variables. Rule errors can be addressed by source-grounded proposals and explicit development tests. Ambiguous meanings, inconsistent unit conventions or overlapping concepts require a context issue to be recorded; changing existing definitions requires a separate input revision. Proposed edits and their test outcomes remain inspectable.

A typed scenario exercises the encoded candidate directly. Independent LLM judges assess the generated SysML against the contextual source. These checks provide different evidence: a passing formal test establishes agreement with the supplied interpretation and supported variables, while the judges assess source preservation under their evaluation protocol.

Prepared source/context stays fixed during conversion. Final semantic assessment and held-out mutation findings remain separate from the refinement controller.

## Where the additional evidence enters

```text
Contextual source packet                  Development scenario suite
       |                                  + expected SAT/UNSAT
       |                                  + literal source support
       v                                            |
  Shared initial TLR                                |
       |                                            |
       +--> C: source + ordinary Z3 audits            |
       |         |                                  |
       |         +--> bounded proposals --> C final |
       |                                            |
       +--> C with development scenarios <-----------+
                 |
                 +--> Z3 audits + scenario results
                 |               |
                 |               v
                 |       source-grounded proposal
                 |               |
                 |       schema/source/compile checks
                 |               |
                 |       scenario rerun + semantic diff
                 |               |
                 +------ regression guard
                                 |
                         selected final candidate

     Final candidates and source/context
                        |
                independent assessment
                        |
         Final judge verdicts and held-out scores
               never return to these trials
```

An ordinary violatability query asks whether the background permits violating a requirement **with that requirement excluded**. SAT is usually expected. A development scenario instead supplies an independently stated expectation and evaluates the scenario while the supported candidate requirements are asserted. Those queries answer different questions. A witness from the first is not automatically a failed test in the second.

## Scenario contract and preparation

The suite uses `development_scenarios/1`, with `role: "development"`. It contains:

| Field | Meaning |
|---|---|
| `origin` | Preparation kind (`llm_prepared`, `human_prepared`, or `fixture`) and a description of how expectations were obtained. |
| `context` | Fixed variables and environmental background used by the scenarios. |
| `symbol_meanings` | Explicit descriptions identifying what each scenario symbol denotes. |
| `scenarios[].id` | Stable scenario identifier. |
| `requirement_ids` | Target source requirements whose representation this scenario examines. |
| `description` | Readable scenario description. |
| `predicate` | Typed Boolean expression representing the scenario. |
| `expected` | `sat` for a permitted scenario, or `unsat` for a prohibited scenario. |
| `source_basis` | Literal quotations and resolvable source identifiers supporting the intended interpretation. |
| `rationale` | Why the source supports the expected outcome. |

Prepare expectations before running the refinement comparison. Include positive, negative and boundary cases, and record the interpretation behind each. Exact quotations provide traceability but do not prove the scenario's expectation correct. An LLM-prepared suite must be reported as such; it must not be described as human-reviewed or as stakeholder ground truth.

The scenario and candidate vocabulary must agree in meaning, types, units and background for a test to execute. A similarly named symbol is not sufficient. An absent candidate binding is recorded as not run; supplying a scenario definition does not silently add that symbol to the candidate. An existing binding with a different meaning is a context problem to inspect, rather than evidence that the candidate requirement is false. Keep the original suite and its expectations fixed throughout a trial.

The suite can provide explicit meanings for symbols not yet present in the initial candidate. Those definitions are additional development assistance. A proposal can add a described, source-grounded, unbounded symbol where the existing repair policy permits vocabulary growth; it cannot change an existing symbol's meaning. A separately supplied fixed formal context still forbids adding variables. Report the supplied vocabulary assistance as part of the treatment, rather than attributing its entire effect to Z3.

Pure capability requirements remain valid at the declared abstraction level. For example, a Boolean availability predicate can express that the library offers a named operation. A development scenario may check that the capability cannot be unavailable while the corresponding requirement holds. This does not prove that an implementation executes the operation successfully. Conversely, a capability flag cannot substitute for an obligation about an actual recipient, temporal order, or cryptographic processing. See the [shared abstraction policy](../scripts/canonical_abstractions.py).

## Exact solver interpretation

Let:

```text
Gamma = declared domains AND environmental assumptions
M     = conjunction of all supported candidate requirement formulas
S_j   = the predicate of development scenario j
e_j   = expected result, SAT or UNSAT
```

The runner checks the prerequisites before treating an outcome as a scenario pass:

1. `Gamma` must be SAT. An inconsistent background must not make every prohibited scenario appear correct.
2. `Gamma AND M` must be SAT. Conflicting candidate requirements cannot receive a suite of vacuous forbidden-case passes.
3. The targeted requirements must have executable representations. Their absence remains unsupported rather than being silently replaced with `true`.
4. `Gamma AND S_j` must be SAT. A scenario already impossible under the background is not a meaningful test of a requirement.
5. Query `Gamma AND M AND S_j`, and compare the actual result with `e_j`.

Unknown, timeout, malformed input and missing support remain distinct from pass. A failing expected outcome is diagnostic evidence, not proof that the source or expected answer should change.

For an illustrative source requirement, "The battery shall have a voltage of at most 28 V":

| Scenario | Expected | Candidate `voltage < 28` | Candidate `voltage <= 28` |
|---|---|---|---|
| `voltage = 27` | SAT | Pass | Pass |
| `voltage = 28` | SAT | Fail | Pass |
| `voltage = 29` | UNSAT | Pass | Pass |

These expectations assume a feasible background and no other contextual source restriction that rules out the chosen values. They expose the incorrect strict endpoint without asking the solver to decide the meaning of "at most."

Expected SAT means **at least one completion** of a partial scenario is permitted. It does not mean every completion is allowed, and it does not prove that a required outcome must occur. To test a static obligation `P implies Q`, use a feasible violating scenario `P AND NOT Q` with expected UNSAT, plus appropriate allowed cases. Temporal eventuality cannot be established by this static test.

## Repair, regression gate and semantic differences

The controller uses the current candidate, source packet, ordinary C audits and development scenario results in a bounded proposal call. Source text and context remain fixed. Existing symbols, meanings, units, bounds and environmental assumptions remain frozen under the existing semantic-feedback policy. The new evidence does not permit making a test pass by narrowing an environmental domain or changing the expected answer.

Each revised candidate is checked again. A previously passing scenario that becomes failing, unsupported, blocked or inconclusive is recorded as a regression; it prevents selecting that proposal. A comparison of aggregate pass counts alone is insufficient: repairing two tests must not conceal breaking a different previously passing test. Rejected proposals and their diagnostics remain in the attempt history and consume the declared proposal budget.

The gate protects only the fixed development suite. A proposal can satisfy these scenarios while being wrong elsewhere. It also cannot determine whether an incorrect expectation caused a rejection. Inspect source quotations, scenario rationale and exact evidence when that occurs.

The before/after semantic comparison uses the existing [formula comparator](../scripts/mutation_core.py). For comparable old and new formulas under a feasible common background, it asks:

```text
Newly permitted: Gamma AND new AND NOT old
Newly forbidden: Gamma AND old AND NOT new
```

| Newly permitted | Newly forbidden | Interpretation |
|---|---|---|
| SAT | UNSAT | New formula is weaker under this background. |
| UNSAT | SAT | New formula is stronger under this background. |
| SAT | SAT | Each permits cases forbidden by the other. |
| UNSAT | UNSAT | Equivalent within the supported formal fragment and background. |

A SAT result supplies a distinguishing valuation. Inconclusive results cannot establish equivalence, and a changed vocabulary or missing executable formula can prevent comparison. These examples explain an edit; they do not automatically authorize it or say which formula preserves the source.

Automated selection of an eligible candidate is a controller decision. It is not engineer acceptance, review certification, or authorization to change the source specification.

## Routing different failures

| Finding | Appropriate next step |
|---|---|
| Feasible scenario disagrees with its source-grounded expectation | Examine the candidate formula and scenario rationale; propose a source-preserving correction within the declared budget. |
| Unsupported target | Inspect whether a permitted abstraction can recover it; retain genuine formalism limits explicitly. |
| Variable meaning, unit convention or identity is ambiguous | Record a context issue. A clarified vocabulary requires a separately identified input revision and rerun. |
| Environmental or scenario premises are inconsistent | Investigate those premises before interpreting a requirement-test outcome. |
| SysML compiler/renderer failure | Preserve the last selected candidate and investigate the tool/renderer; do not reinterpret the requirement to hide the failure. |
| Solver unknown or timeout | Retain the inconclusive result; do not count it as a pass or claim equivalence. |
| Source requirements conflict | Preserve the conflict. Scenario success does not authorize silently removing a source obligation. |

Automatic merging or rewriting of variable definitions requires a future explicit context-revision workflow. Within the current trial, existing definitions remain frozen.

## Inspection and input boundaries

The run records the selected candidate, scenario outcomes, rejected proposals, and remaining unsupported obligations. Inspect the exact predicates and source evidence before accepting a correction. Passing a supplied development scenario is agreement with that test under the declared background; it does not establish complete source fidelity.

Keep final assessment answers outside repair inputs. A scenario intentionally supplied to guide refinement is development assistance and cannot also be treated as an unseen test of that run.

## CLI use

Enable the extension explicitly with `--development-scenarios`. Standalone use requires condition C and a positive `--feedback-repairs` budget. In a `study`, the suite is supplied to the C branch only. This makes the extra assistance an explicit treatment; B does not receive the suite or its expected outcomes.

For two C refinement branches starting from one saved TLR, use separate new output directories:

```bash
CODEX_REASONING_EFFORT=low python scripts/requirements_pipeline.py run \
  --statement path/to/contextual_requirements.json \
  --tlr-file path/to/shared_initial_tlr.json \
  --condition C --model gpt-6-astra \
  --abstention-repairs 0 --feedback-repairs 2 \
  --output-dir out/c_ordinary_feedback

CODEX_REASONING_EFFORT=low python scripts/requirements_pipeline.py run \
  --statement path/to/contextual_requirements.json \
  --tlr-file path/to/shared_initial_tlr.json \
  --condition C --model gpt-6-astra \
  --abstention-repairs 0 --feedback-repairs 2 \
  --development-scenarios path/to/development_scenarios.json \
  --output-dir out/c_development_feedback
```

These commands invoke generation-model review calls. The supplied TLR skips a new initial-generation call. Add the same `--context-file` to both branches if that initial TLR was generated with a fixed formal context; do not confuse the contextual source packet with a fixed formal vocabulary.

## Related implementation and methods

- [Scenario evaluation implementation](../scripts/canonical_scenarios.py).
- [Existing bounded solver-feedback controller](../scripts/canonical_cli.py).
- [Source-grounding and fixed-context guards](../scripts/canonical_feedback.py).
- [Formula comparison and witness generation](../scripts/mutation_core.py).
- [Existing solver-feedback method](SOLVER_FEEDBACK_REPAIR.md).
- [Mutation stress testing](MUTATION_STRESS_TEST.md).
