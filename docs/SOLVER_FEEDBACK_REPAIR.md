# Bounded source and solver-feedback refinement

The CLI supports optional TLR refinement through `--feedback-repairs K`, recorded as policy `source_grounded_semantic_feedback/1`. A standalone B run reviews the source and current candidate; a standalone C run also supplies the candidate's Z3 evidence. Both can revise supported formulas and abstentions within fixed source and background constraints.

The reusable `study` batch command starts B and C from one shared initial TLR and gives each branch up to the same declared number of review calls. This is an execution interface; source selection, assessment endpoints, and claims about effectiveness belong to an external protocol. The default feedback budget is zero.

## 1. Policies and execution behavior

| Policy | Candidate relationship | Model feedback | Scope |
|---|---|---|---|
| Default study, both repair budgets zero | B and C share the same final TLR/SysML | None | Utility of auditing and admission; no possible B/C generation improvement |
| `--abstention-repairs K` | B and C share the selected recovered candidate | Source-based advisory diagnosis and separate abstention review | Recover unsupported/unresolved clauses while freezing previously supported records |
| `--feedback-repairs K` | B and C share the initial candidate, then revise independently | B: source review; C: source review plus candidate Z3 evidence | Incremental utility of solver feedback under matched revision opportunities |

The two positive repair budgets are mutually exclusive. Feedback repair accepts K from zero through five and defaults to zero for `run` and `study`. Selecting positive feedback repair suppresses the generated standalone run's implicit abstention-recovery default. Explicitly selecting both positive budgets is rejected.

A standalone feedback run supports condition B or C. `BC` is rejected with positive feedback repair because its shared-final-candidate semantics would hide the separate revision treatments. A remains a direct one-call SysML route. Use `study` for the controlled fork from one initial TLR; separate B and C generation commands do not establish a common initial candidate.

## 2. Data flow and information boundaries

```text
                 Fixed source packet X + fixed context Gamma
                                      |
                  +-------------------+-------------------+
                  |                                       |
                  v                                       v
          A: direct SysML                         One initial TLR T0
          one generation                          validate and compile
                  |                                       |
                  |                         +-------------+-------------+
                  |                         |                           |
                  |                         v                           v
                  |                    B0 = T0                     C0 = T0
                  |                         |                           |
                  |                  source + current TLR       source + current TLR
                  |                  + abstraction policy       + abstraction policy
                  |                         |                   + current Z3 evidence
                  |                         v                           v
                  |                   ONE review/proposal         ONE review/proposal
                  |                   call per round              call per round
                  |                         |                           |
                  |                   static/source guard        same static/source guard
                  |                   shared renderer/compiler   same renderer/compiler
                  |                         |                           |
                  |                   select eligible revision   select eligible revision
                  |                         |                    regenerate Z3 audits
                  |                         |                           |
                  |                   repeat up to K rounds      repeat up to K rounds
                  |                         |                           |
                  v                         v                           v
             final A SysML             final B SysML               final C SysML
                  |                         |                           |
                  +-------------------------+---------------------------+
                                            |
                            Freeze candidates and complete histories
                                            |
                             Separate inspection or assessment
```

Each branch sees its own current TLR and its own prior local parse/guard failures. B receives no Z3 query, result, witness, audit finding or C-branch revision. C receives selected evidence from the candidate's actual audit. Every check's status, purpose and artifact references remain in the feedback inventory. Full query/result/witness payloads prioritize global background/consistency checks, checks implicated in findings or inconclusive results, then per-requirement violatability checks, up to 64 full checks in deterministic order. The saved feedback records its selection policy and omitted payloads. The complete audit remains on disk; absence from the prompt is not a passed check, and unavailable/unknown evidence stays explicit. A review and proposed complete TLR are returned in the same response; there is no additional diagnosis call.

Source context and public abstraction rules are generation inputs. Final judge verdicts, held-out mutation outcomes, expected reference formulas and calibration answers are not. An audit of the current generated formula is permissible C feedback; a held-out reference formula is not. The mutation harness remains a separate evaluation process and does not supply development probes to this controller. Its source mode can select `--feedback-repairs K` to invoke the frozen C converter with the same declared budget for each original/variant input. Expected formulas, mutation relation labels and comparison outcomes still stay outside conversion. This is an auxiliary C campaign, not an automatic matched A/B/C mutation experiment.

Both branches use the same configured generation model and settings, round limit, representation, renderer, compiler policy and fixed source/context. Equal round limits match revision opportunities, not realized token counts, latency or successful calls. C has additional solver work and a longer evidence input. Record those differences instead of treating the two branches as cost-identical.

## 3. Permitted corrections and frozen content

The proposal reviews every source requirement, including supported clauses. It can correct a formula, reconsider its abstraction or disclose a previously unsupported interpretation. A source says “at most 28 V” and a candidate says `< 28 V`: changing the operator to `<=` is a possible translation correction. Whether a particular proposal preserves the source still needs external assessment.

The source inventory, wording and context stay fixed. Existing symbol meanings, types, units and domains, and environmental assumptions stay fixed. A proposal cannot quietly weaken an assumption or change what a variable denotes to obtain a different Z3 result. A problem in those frozen premises requires a separately identified input/context revision. With a fixed vocabulary, no variable may be added. Without one, a new variable must be defined, unbounded and referenced by a changed supported requirement; it then joins the preserved vocabulary for subsequent rounds. Source grounding and schema checks make a proposal inspectable; they do not prove its explanation true.

Each response uses `semantic_repair_proposal/1` with a complete `tlr` and exactly one `reviews` entry per source ID. An entry contains `id`, `outcome` (`changed` or `retained`), a specific `reason`, and `source_basis`. A changed record must quote its own requirement's wording; additional literal context quotes may supplement it. A retained record remains unchanged after normalization. Every supplied quote is checked against the immutable source. The current TLR omits only repeated source text/context in the model-facing payload; the full source packet remains present and source metadata is rebound by ID before validating the complete candidate.

Unlike abstention recovery, supported-record counts need not increase. A previously supported formula may have represented an unjustified interpretation; retracting that claim can reduce executable coverage while improving honesty. Record changed IDs, support gains and regressions, and judge their consequences independently. A higher supported count is not the selection objective.

The shared deterministic renderer regenerates SysML from each eligible TLR. C's audits are regenerated for that revision. Solver results for an earlier candidate cannot be presented as evidence for a later one. Independent extraction of meaning from generated SysML is still future work: the shared AST and compiler do not establish full semantic preservation by themselves.

## 4. Selection, stopping and failures

Selection uses the **latest structurally valid, source-grounding-valid, guard-compliant proposal whose generated SysML passes enabled compilation**. This is artifact eligibility, not independent confirmation of source fidelity. Selection does not require SAT, an improved audit score or increased support. With compilation explicitly skipped, no compiler success is claimed and C admission remains unavailable.

| Event | Controller behavior |
|---|---|
| Initial generation is invalid or fails | Retain the failure; block both structured branches instead of generating a replacement initial candidate. |
| Enabled initial compilation fails | Stop before semantic review; do not reinterpret source to compensate for a renderer/compiler failure. |
| Valid proposal does not change the normalized candidate | Retain its review and stop as `no_change_after_review`. |
| Proposal fails parsing, review validation or preservation guard | Consume that round, retain the last selected candidate and record the failure; a remaining round may receive the local validation diagnostic. |
| Proposal passes eligibility checks | Select it and retain the prior attempt; continue while budget remains. |
| Proposal compilation fails, or rendering/checking has a tool error | Keep the current selected candidate and stop; investigate the tool failure separately. |
| Round budget is exhausted | Return the current selected candidate and the complete history. |

For a generated standalone structured run, at most `1 + K` controller transport invocations occur: one initial generation and K review/proposal calls. A supplied TLR omits the initial model call. A full feedback study plans at most `2 + 2K` invocations per repetition: one A call, one shared initial TLR call and K proposals for each branch. These bounds concern controller invocations, not provider-internal requests or billable events. Early stopping and failures can reduce actual calls. Compilation and solver calls are separately recorded.

An UNSAT conjunction can faithfully encode contradictory source requirements. A feasible source-consistent encoding may contain redundancy or an unreachable trigger. A SAT violatability query can be an expected demonstration of what a requirement excludes. The controller must not interpret all such outcomes as defects to eliminate. A source conflict may remain UNSAT after every review, with its evidence retained for engineering resolution.

## 5. Commands

Run from the repository root with a fresh output directory. These commands invoke the configured generation model.

```bash
# Default audit-only paired study; explicit budgets fix the baseline.
python scripts/requirements_pipeline.py study \
  --statement path/to/requirements.json \
  --context-file path/to/context.json \
  --model "$MODEL_ID" --repetitions 5 \
  --abstention-repairs 0 --feedback-repairs 0 \
  --output-dir out/abc_audit_only

# Same initial structured candidate, two independent revision branches.
python scripts/requirements_pipeline.py study \
  --statement path/to/requirements.json \
  --context-file path/to/context.json \
  --model "$MODEL_ID" --repetitions 5 \
  --abstention-repairs 0 --feedback-repairs 2 \
  --output-dir out/abc_feedback

# One C branch for an operational trial; this alone is not a paired study.
python scripts/requirements_pipeline.py run \
  --statement path/to/requirements.json \
  --context-file path/to/context.json --condition C \
  --model "$MODEL_ID" \
  --abstention-repairs 0 --feedback-repairs 2 \
  --output-dir out/c_feedback
```

For B's source-only branch, use `--condition B` with the same feedback flag. For exploratory vocabulary generation, omit `--context-file` and report that interpretation freedom. Fix model reasoning settings, transport timeouts and aggregate budgets before launching a scored campaign; the flag does not silently standardize those settings.

Assess the frozen final candidates independently against source-derived criteria. Different B/C candidates require separate assessment; source/context and expected evaluation answers must remain outside the repair loop.

## 6. Saved evidence

```text
feedback_study/
  study_configuration.json
  study.json
  report.md
  rep-001/
    initial/                       # one shared initial structured generation
    A/                             # unchanged direct-generation treatment
    B/                             # source-only revision branch; no Z3
      model.sysml                  # selected final candidate
      tlr.json
      feedback_repair.json         # budget, mode, steps, selection, stopping
      feedback_attempts/
        000/                       # initial candidate copied into this branch
        001/                       # proposal, reviews, checks, applicable artifacts
        ...
    C/                             # separate Z3-feedback revision branch
      model.sysml
      tlr.json
      audit/                       # selected candidate's current evidence
      feedback_repair.json
      feedback_attempts/
        000/
        001/                       # also keeps available solver feedback/audits
        ...
```

Failed attempts retain their available raw response, errors and partial artifacts; they do not receive invented compiler/solver results. Root candidate files represent the selected attempt. `feedback_repair.json` distinguishes source versus solver feedback, requested/actual calls, selected attempt and stopping reason. The exact saved configuration and attempt history take precedence over an assumed budget or a CLI exit code. Valid attempts include `proposal_reviews.json` and `changes.json` with per-ID before/after records; C rounds retain `solver_feedback.json` showing the evidence actually supplied. Complete audit artifacts remain available separately.

## 7. Inspecting the outcome

Inspect initial and selected artifacts, proposed/accepted changes, support gains or losses, local validation failures, compilation, audit changes, stop reasons, and actual calls. Missing usage is unknown, not zero. Preserve genuine source conflicts instead of weakening the requirements to increase admission.

In batch execution, A receives no revision calls, while B and C may consume different numbers of calls because of early stops. The saved configuration and attempt histories describe the actual work performed. Do not interpret a selected proposal or a positive solver result as engineer approval.

Held-out mutation outcomes, reference formulas, and final judgments are excluded from repair prompts. Independent SysML read-back is outside this controller. See the [CLI reference](CANONICAL_CLI.md#matched-source-review-and-z3-feedback-repair) and [development-scenario guide](DEVELOPMENT_SCENARIO_REFINEMENT.md) for execution and feedback boundaries.
