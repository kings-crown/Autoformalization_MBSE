# CLI mutation stress test

The mutation tool compares an explicitly changed requirement against a supplied formal reference. It also checks whether regenerating from changed source text preserves the intended change. It is a separate comparison utility; it does not change the requirements pipeline's admission policy or approve a SysML model.

The implementation has two paths:

| Path | Input to the tool | What it measures |
|---|---|---|
| Formal | Canonical expressions and explicit, applicable mutation operators | Whether the solver comparison detects the constructed formal difference |
| Source | A complete requirements bundle, one edited source clause, and expected reference expressions | Whether regenerated interpretations reflect the source edit, including baseline mistakes and changes to untouched clauses |

A formal mutation detected by Z3 does not establish source-to-model fidelity. A source mutation that produces a different formula does not establish that it produced the *intended* different formula. The saved results keep those questions separate.

## Start with the offline demonstration

Run commands from the repository root. The formal command needs Python and a Z3 executable and makes no LLM calls:

```bash
python scripts/mutation_stress.py validate \
  --manifest examples/mutations/demo.json

python scripts/mutation_stress.py formal \
  --manifest examples/mutations/demo.json \
  --output out/mutations_demo
```

Source generation runs the [canonical LLM workflow](CANONICAL_CLI.md) in condition C with the campaign's fixed vocabulary/background. It invokes the configured model and can consume inference budget. Generated `mbse_tlr/1` expressions are extracted directly; no manual normalization is required for this path:

```bash
python scripts/mutation_stress.py source \
  --manifest examples/mutations/scalar_source.json \
  --output out/mutations_source \
  --model "$MODEL_ID" \
  --repetitions 1
```

The default source engine is the LLM pipeline. Use `replay` to evaluate supplied formulas without invoking generation. The formal fixture does not exercise provider-backed generation.

Choose a new output directory for each campaign. Existing campaign evidence is not overwritten. Validation checks the manifest and executable expressions; it is not an engineering review of their meaning.

The formal demo has four positive mutants, one per primary family, and four equivalent controls. The scalar source demo has four bound/endpoint mutants and two unit-conversion controls, requiring seven workflow invocations for one repetition. These counts describe fixture coverage, not empirical detection results.

The demonstration requirements and expected formulas are synthetic tool fixtures. They exercise operators and evidence handling; they are not observed system defects or independently approved reference requirements.

## What the formal comparison proves

Let `C` be the fixed declared background, `F0` the canonical target expression, and `F1` the changed expression. The checker first establishes that `C` is satisfiable. It then asks:

```text
newly permitted: C AND F1 AND NOT F0
newly forbidden: C AND F0 AND NOT F1
```

| Newly permitted | Newly forbidden | Interpretation under the recorded context |
|---|---|---|
| SAT | UNSAT | The changed expression is weaker |
| UNSAT | SAT | The changed expression is stronger |
| SAT | SAT | Both permitted and forbidden valuations changed |
| UNSAT | UNSAT | The expressions are equivalent |
| SAT | Inconclusive | A difference is established; full directional classification is unavailable |
| Inconclusive | SAT | A difference is established; full directional classification is unavailable |
| No SAT result and an inconclusive result | — | Equivalence and difference remain unestablished |

A SAT result is accompanied by the exact query and solver output so the distinguishing valuation can be inspected. An inconsistent background must not make every pair appear equivalent. Missing symbols, unsupported expressions, failed generation, parser errors, solver failures, unknown results, and timeouts remain separate evidence rather than successful detections.

For example, changing `language_count >= 10` to `language_count >= 9` permits the value nine. Both requirements are individually satisfiable. A runtime consistency checker may therefore pass both; the *offline reference comparison* is what exposes the weakening. Do not attribute that finding to a runtime check that never compared the two versions.

The comparison context must not assert the requirement being compared, its mutant, or an implementation rule that hides their difference. Inspect the justification for the background before collecting study data. An explicit context can still contain an unjustified assumption; structural validation cannot determine its engineering authority.

## Formal vocabulary and supported expressions

The comparison engine uses the static, current-state subset of the existing typed behavior AST. It reuses its validator, unit normalization, and SMT emitter; doing so does not enable temporal behavior checking. Generated SMT symbols have the form `v_<name>_0`. There is one valuation, no transition relation, and no execution trace.

The context declares 1–24 variables of type `Bool`, `Int`, or `Real`, optional numeric units and lower/upper bounds, and at most 40 named background assumptions. Each background assumption contains an `id`, explanatory `text`, and Boolean `predicate`. Roles, parameter values, next-state references, and raw SMT are not accepted as context shortcuts.

Expressions are JSON values:

| Form | Example |
|---|---|
| Boolean constant | `true` or `false` |
| Declared variable | `{"var": "voltage"}` |
| Exact numeric literal | `{"value": "28", "unit": "V"}` |
| Operator | `{"op": "<=", "args": [{"var": "voltage"}, {"value": "28", "unit": "V"}]}` |

Supported operators are `and`, `or`, `not`, `implies`, `ite`, `=`, `!=`, `<`, `<=`, `>`, `>=`, `+`, `-`, and linear `*`. Multiplication requires a fixed dimensionless literal factor; products of two unconstrained variables are rejected. Comparisons, addition, and subtraction require compatible physical dimensions. Boolean and numeric expressions cannot be substituted for each other. The final requirement expression must be Boolean.

Use integer values or exact decimal strings for literals and bounds. Binary floating-point literals, exponent notation, NaN, arbitrary code, unsupported operators, and oversized expressions are rejected. Known units are normalized exactly; for example, `28000 mV` and `28 V` use the same physical magnitude. A well-typed wrong voltage is a semantic mutation; changing volts to kilograms is a validation error, not evidence of semantic fault detection.

There is no support in this campaign for quantifiers, nonlinear arithmetic, arrays, probabilistic models, liveness, temporal operators, or independent extraction of constraints from SysML. Timing represented by a static scalar remains a static scalar. Record unsupported source meaning explicitly instead of assigning it an unconstrained Boolean and calling it formalized.

## Fault families and equivalent controls

| Family | Example | Required applicability |
|---|---|---|
| Required-response suppression | Disable a required consequence after its trigger | The canonical expression contains a genuine response obligation |
| Operating-guard removal | Remove a mode condition controlling an obligation | The source and expression contain the guard being removed |
| Value or binding mismatch | Replace a specified value or use a different compatible quantity | The replacement has the intended type and a separately justified meaning |
| Limit violation | Change a numeric threshold or a strict/inclusive endpoint | The requirement has that boundary and the context permits a distinguishing valuation |

The manifest identifies each edited target, its category, applicability, rationale, and expected semantic relation. The manifest lists eligible variants only; preserve excluded categories and reasons separately in the study preregistration. The tool does not invent guards or bounds to fill a desired count. A controlled edit should have one primary category even when other labels would also fit.

Equivalent controls test formal-comparator false alarms and unintended meaning changes during source regeneration: meaning-preserving wording, correctly converted units, reordered conjunctions, or other explicit equivalent expressions. Their expected equivalence is part of the reference definition, not a conclusion inferred from changed text alone. A failed or unsupported control is not silently counted as a false alarm or as a correct acceptance.

## Define a campaign manifest

A manifest has schema `mutation_campaign/1`. Unknown fields and duplicate JSON keys are rejected, so spelling mistakes do not silently change the experiment. The checked-in [formal demonstration](../examples/mutations/demo.json) covers all four fault families; [scalar source demonstration](../examples/mutations/scalar_source.json) provides a small set of static bounds and unit controls for source regeneration.

A minimal formal campaign looks like this:

```json
{
  "schema": "mutation_campaign/1",
  "id": "language_threshold",
  "reference": {
    "status": "constructed_fixture",
    "author": "Fixture author",
    "description": "Illustrative reference; not stakeholder ground truth."
  },
  "context": {
    "variables": [{"name": "n", "type": "Int", "bounds": {"lower": "0"}}],
    "background": []
  },
  "requirements": [{
    "id": "R1",
    "text": "The controller shall support at least ten languages.",
    "source": {"document": "synthetic example", "location": "R1"},
    "formula": {"op": ">=", "args": [{"var": "n"}, {"value": "10"}]}
  }],
  "variants": [{
    "id": "R1_weakened",
    "requirement_id": "R1",
    "kind": "mutant",
    "category": "limit_violation",
    "eligibility": "The reference contains an explicit integer lower bound.",
    "rationale": "Reduce the required number of supported languages by one.",
    "expected_relation": "weakened",
    "mutation": {"operator": "shift_bound", "value": "-1"},
    "text": "The controller shall support at least nine languages."
  }]
}
```

`reference`, including its `status`, `author`, and `description`, is optional descriptive metadata. The CLI does not demand authority attestations or approval labels. If supplied for a study, report an accurate status, including `LLM-reviewed` when appropriate. Source metadata must be a nonempty object; retain document identity and exact clause/row location. Requirement and variant IDs must be unique safe identifiers. `baseline` is reserved for the original source generation.

Each requirement retains a `formula`; unsupported requirements use `null` and a nonempty `unsupported_reason`. Their text can remain in a contextual source bundle, but an executable variant cannot target a missing reference. Each variant contains a target ID, category, eligibility rationale, edit rationale, expected relation, and explicit mutation. `text` is optional for a formal-only variant and required for participation in source regeneration. The expected relation is one of `weakened`, `strengthened`, `changed`, or `equivalent`; only a `control` with category `equivalent_control` can expect equivalence.

### Mutation operator reference

The optional `path` selects an expression within the canonical AST using object keys and array indices. The default `[]` selects the entire expression; for a comparison, `["args", 1]` selects its right-hand literal. A path must identify an expression, not a raw string or an argument array.

| Operator | Selects | `value` | Action |
|---|---|---|---|
| `suppress_response` | An `implies` expression | Omitted | Replace the selected obligation with `true` |
| `remove_guard` | An `implies` expression | Omitted | Replace the implication with its consequence |
| `replace_value` | A numeric literal | Exact replacement number | Change the literal and retain its unit |
| `replace_binding` | A variable reference | Declared replacement variable name | Bind the expression to another compatible quantity |
| `shift_bound` | An ordered comparison with a right-hand literal | Exact signed increment | Add the increment to the bound |
| `flip_boundary` | `<`, `<=`, `>`, or `>=` comparison | Omitted | Exchange strict and inclusive endpoints |
| `double_negation` | A Boolean expression | Omitted | Wrap it in two negations as an equivalent control |
| `reverse_conjunction` | An `and` expression | Omitted | Reverse its arguments as an equivalent control |
| `explicit` | Any selected expression | Replacement AST | Apply an explicitly supplied edit with reviewed category and rationale |

The canonical formula is normalized before an edit. Numeric replacements and bound increments are therefore expressed in the normalized unit of the selected expression. For example, a voltage declared in millivolts normalizes to volts; an increment of `"1"` then denotes one volt. Use an explicit replacement literal with a unit when that is clearer, and inspect the saved normalized inputs.

`suppress_response` removes the selected obligation; it does not require the response to be absent. `remove_guard` makes the consequence unconditional and can strengthen the requirement. Operator names specify edits, not guaranteed results: the background may mask the change. `formal` checks the declared expected relation and preserves a disagreement instead of rewriting the expectation.

### Fixed vocabulary and expression correspondence

The canonical generation prompt receives the manifest's declared variables and background. The source adapter reads supported `mbse_tlr/1` formulas directly and requires the generated vocabulary, domains, and assumptions to match the fixed context. A changed context is not silently merged into the comparison; it remains not comparable.

Additional `binding` records are unnecessary for canonical AST extraction. Historical scalar artifacts may still use their earlier explicit binding adapter as a compatibility path. That adapter does not define the new LLM workflow or restrict it to a particular textual grammar.

## Source regeneration and attribution

For each repetition, source mode generates the unmodified full requirement bundle once. It then generates a fresh full bundle per eligible variant, changing only the selected target's wording. Source IDs and surrounding clauses stay fixed. The LLM receives the source records and fixed vocabulary/background, but not canonical requirement formulas, expected mutants, or evaluation labels. Necessary contextual definitions belong in the source bundle. A repetition is a new generation trial, not an independent source case.

The workflow uses canonical condition C with one initial LLM-authored TLR, shared deterministic SysML/SMT encoding, audits and compilation. Source campaigns default to `--feedback-repairs 0`; an explicit positive budget enables the same source-grounded, solver-informed feedback controller for every baseline and variant invocation. Schema-valid supported rows are emitted as draft constraints without inventory preparation or a separate source-review eligibility gate. It does not invent an independent behavioral design or feed mutation comparison results back into generation. Equivalence verdicts concern extracted expressions; there is no independent SysML read-back.

For a canonical reference `F0`, baseline output `B`, expected changed reference `F1`, and changed-source output `G`, the experiment distinguishes:

1. **Baseline-reference agreement:** compare `B` with `F0`. A baseline error predates the mutation.
2. **Raw change detection:** compare `G` with `F0`. A difference alone is insufficient to establish successful mutation preservation.
3. **Expected-mutation preservation:** compare `G` with `F1`. This tests whether generation captured the intended changed meaning.
4. **Collateral changes:** compare interpretations of untouched clauses between the baseline and changed-source generations. Record these separately from the target mutation.
5. **Runtime findings:** retain the workflow's own consistency and compilation results. These are separate from offline comparisons.

A generated formula can differ from the canonical reference because it is wrong in an unrelated way. Conversely, the expected change may become indistinguishable under the selected background. The comparison evidence and baseline record allow both cases to be investigated.

Automatic extraction now accepts supported canonical `mbse_tlr/1` expressions, including conditionals in the declared static fragment. It validates source associations and fixed vocabulary/background before comparison. Unsupported clauses, missing formulas, generation failures, and changed contexts remain explicit. Historical native pipeline artifacts still need a separately normalized replay input; this compatibility limitation does not apply to the canonical generation path. None of these adapters extracts semantics from SysML.

## Replay or import normalized candidates

`replay` compares recorded formulas without invoking a generator. This is useful for rerunning source evidence with a recorded toolchain or for importing formulas from a separately implemented extractor:

```bash
python scripts/mutation_stress.py replay \
  --manifest examples/mutations/scalar_source.json \
  --candidates out/mutations_source/candidates.json \
  --output out/mutations_replay
```

A candidate file requires schema `mutation_candidates/1` and a `samples` array. It may include an explicit `context`, checked structurally against the campaign. Hash fields are not required and legacy hash metadata does not function as an approval gate. Every repetition, numbered from 1 to 50, needs a `baseline` sample. Each sample records:

| Field | Meaning |
|---|---|
| `variant_id` | `baseline` or a manifest variant ID |
| `repetition` | Positive repetition number |
| `source_requirements` | Exact complete bundle of `{id, text, source}` records for this original or edited input |
| `formulas` | Every requirement ID mapped to an AST or `null`; invalid ASTs are retained as encoding-error outcomes |
| `provenance` | Optional descriptive object; no review status or attestation is required |
| `context` | Optional explicit vocabulary/background, checked against the campaign |
| `unsupported` | Requirement ID to explanation for unavailable formulas |
| `runtime_evidence` | Optional recorded workflow evidence, separate from offline findings |
| `status` | Optional generation/import status |

Import requires the exact source bundle and full formula inventory so an artifact from another source revision cannot silently substitute for this trial. Missing variants remain missing candidates and stay in planned counts. `null` formulas need a nonempty explanation in `unsupported`. Duplicate samples and altered source text/order/metadata are rejected. Explicit supplied context is checked structurally; legacy hash strings are informational. Structural candidate-format errors reject import, while individual expression-validation failures remain measured outcomes.

Imported formulas are supplied evidence. The importer cannot establish their faithful derivation from a source document or SysML. Optional method descriptions and disclosure of manual changes help interpretation, but no provenance certificate or reviewer authorization is required to run replay.

## Reproducibility and budgets

Freeze the manifest before execution, including original text and source references, background, canonical formulas, explicit vocabulary bindings, reference review status, applicability decisions, mutant text or formal operator, and expected result. Describe LLM-only review as LLM-reviewed; the word *canonical* does not imply human-validated ground truth.

The CLI controls are:

| Option | Commands | Default / interpretation |
|---|---|---|
| `--solver` | `formal`, `source`, `replay` | `z3`; executable name or path for the offline comparator |
| `--timeout-seconds` | `formal`, `source`, `replay` | 10 seconds per solver process; positive and at most 3,600 |
| `--engine` | `source` | `pipeline`, the canonical LLM workflow; older adapters are compatibility options |
| `--model` | `source` | Explicit generation model; otherwise uses installed model configuration |
| `--repetitions` | `source` | One complete baseline/variant trial set; accepted range 1–50 |
| `--max-generations` | `source` | 20 workflow invocations; reject a larger plan before generation |
| `--feedback-repairs K` | `source` | Default 0; permit 0–5 source-grounded feedback proposals in each invocation |
| `--abstention-repairs K` | `source` | Deprecated alias for the same feedback budget; do not supply both positive options |
| `--generation-timeout-seconds` | `source` | 600 seconds per workflow subprocess; positive and at most 86,400 |
| `--candidates` | `replay` | Required recorded normalized-candidate file |

`--timeout-seconds` applies separately to feasibility, each implication query, and any follow-up witness query. It is not a total campaign timeout. `--solver` controls the experiment's comparisons; workflow-internal solver/compiler configuration remains in each generation's effective settings. Use `--model` for scored runs and inspect saved configuration before interpreting results; installed model defaults remain available for exploratory use.

Source generation invocations are determined by the number of repetitions and eligible source variants:

```text
repetitions * (1 baseline + number of eligible source variants)
```

Source invocations and source/replay trial denominators include only variants with a `text` field. Formal-only variants are listed as ineligible in `source_eligibility` and excluded from these trial denominators. A missing candidate for a planned text-bearing variant remains `missing_candidate` and stays in the denominator. The eligibility inventory distinguishes an inapplicable experiment from an attempted or missing output; freeze source eligibility before generation.

The canonical source path defaults to one generation transport invocation and zero feedback opportunities. With `--feedback-repairs K`, each workflow allows at most `1 + K` model transport invocations: one initial generation and up to K feedback proposals. The deprecated `--abstention-repairs K` alias uses this same controller and bound. Source mode explicitly disables initial format correction (`--format-repairs 0`) and has no inventory-preparation or separate source-review calls. For G planned workflows the ceiling is `G * (1 + K)`, with early stopping and actual counts retained separately. This is a controller-invocation bound, not an assertion about provider-internal request billing. `--max-generations` limits workflows; token, monetary, and total campaign-time budgets remain separate. Compiler and Z3 checks are local tools; C's own Z3 evidence can inform its feedback proposals. Logs retain actual prompts, responses, and latency. Unavailable token or cost figures are not zero, and the campaign does not manufacture aggregate prices.

The campaign saves its normalized manifest, configuration, exact queries/results, witnesses, generated candidates, commands, and source records. It no longer requires or generates artifact hash gates or implementation-source snapshots. A changed model, background, mutation, reference, or budget belongs in a fresh output location. Inspecting the actual files establishes what was compared; their semantic correctness remains an evaluation question.

## Keep recovery separate from mutation evaluation

A campaign with an explicit recovery budget evaluates a different frozen converter from the zero-repair baseline. Apply the same value to the original packet and every variant, record the upper call bound before execution, and inspect actual per-run configurations. Keep generation settings and per-call timeouts comparable; an outer subprocess timeout must allow the declared number of model calls and downstream tools to finish. Hitting that outer timeout is a retained workflow failure, not permission to silently exceed it or resample.

The active feedback controller reviews every source requirement in each proposal, including supported, unsupported, and unresolved rows. Source wording and engineer-supplied fixed context remain unchanged; generated interpretation may be revised against literal source evidence. It receives the source/context, current TLR, its own C solver audit, and earlier proposal-validation errors. It receives no canonical reference formula, expected mutant, mutation category, expected relation, or offline comparison witness. A deliberate source change is an input to preserve; feedback must not restore the original requirement because the evaluator knows it is a mutation.

`feedback_repair.json` and `feedback_attempts/` retain the active proposal history, per-rule reviews, context reviews, validation outcomes, and selected candidate. The source adapter compares the selected final TLR. To claim improved mutation sensitivity, compare explicitly budgeted frozen converters with baseline-reference agreement; use fresh held-out cases after tuning. The same trial's comparison findings must not return to its feedback loop.

Historical campaigns may retain `source_grounded_abstention_recovery/2`, `repair.json`, and `attempts/`, including separate diagnosis/proposal calls. Revision-1 `no_recoverable_abstentions` denotes a diagnosis-only stop; revision 2 reaches a proposal before `no_progress_after_proposal`. Preserve their recorded policies and call counts. Historical source-review/inventory gates remain binding when extracting those old candidates: source, TLR, fixed context, and review responses must still match, and rejected rules stay unavailable to comparison. These records do not add gates or calls to the current path.

A higher source-mode difference rate alone is insufficient: separate intended-change preservation, baseline mistakes, equivalent-control false alarms and collateral changes. The field `expected_mutation_preserved` counts controls as well as semantic mutants; report the two groups separately. `operational_detection_yield` is an offline reference-comparison yield, not evidence that C's runtime audits detected the source edit. The current campaign does not perform a uniform A/B/C mutation ablation or independent SysML semantic read-back.

## Saved evidence

A formal campaign writes this layout:

```text
out/mutations_demo/
  manifest.json                 normalized, frozen campaign definition
  execution.json                command and comparison configuration
  progress.json                 completed/planned variant counts
  comparisons/<variant-id>/
    inputs.json                 normalized context and the two formulas
    background.smt2             context feasibility query
    background.json             exact solver result and process diagnostics
    newly_permitted.smt2        candidate AND NOT canonical
    newly_permitted.json
    newly_forbidden.smt2        canonical AND NOT candidate
    newly_forbidden.json
    *_witness.smt2              separate get-value query, when SAT
    *_witness.json              valuation and its own solver outcome
    comparison.json             relation, evidence paths, limitations
  report.json                   full records, summary and evidence paths
  report.md                     readable overview
```

Later query files are absent when a prerequisite prevents comparison; inspect `comparison.json` for the reason. Witness retrieval is a separate solver call with its own outcome. A failed witness query does not erase an already established SAT result, and the report must not invent a valuation when extraction fails.

Source and replay campaigns add candidate snapshots, baseline/reference comparisons, target comparisons, and collateral-change evidence. Source mode also retains the actual workflow run artifacts, including generated SysML and available solver/compiler/provider records. Follow the evidence paths in `report.json` to associate each trial with the exact generation and comparison; the Markdown table is only an overview.

For source and replay campaigns, additional evidence lives under:

```text
candidates.json
reference_checks/<variant-id>/
canonical_bundle_consistency/
baseline_checks/<repetition>/<requirement-id>/
comparisons/<repetition>/<variant-id>/
  canonical/
  expected_mutation/
  generated_baseline/
  collateral/<untouched-requirement-id>/
  frozen_neighbor_consistency/
generations/<repetition>/<baseline-or-variant-id>/   # source mode only
  requirements.json
  invocation.json
  stdout.json
  stderr.log
  context.json
  run/                          # canonical CLI model/TLR/audit artifacts
```

`frozen_neighbor_consistency` checks the generated target conjoined with the *canonical* executable neighboring requirements under the frozen background. This isolates the target edit; it is not the actual regenerated model's runtime consistency result when neighboring interpretations changed. The separate `canonical_bundle_consistency` result checks the original executable reference bundle. Interpret an UNSAT isolation result as a newly introduced conflict only after establishing that this canonical bundle was consistent under the same background. If canonical consistency is UNSAT or inconclusive, that attribution is unavailable. Unsupported canonical neighbors are explicitly listed in `frozen_neighbor_unsupported_ids`; the canonical check likewise retains `canonical_bundle_unsupported_ids`. Neither result establishes consistency of unsupported clauses. Identical available sibling ASTs need no additional query; changed or unavailable sibling formulas appear in `collateral_changes`.

The CLI exits with status `0` when a report was produced and `2` for an invalid request or campaign setup failure. A completed report can contain inconsistent backgrounds, inconclusive solver outcomes, unsupported extraction, or unmet expected relations. Completion is not a passed semantic test. Existing output directories are rejected before they can be overwritten.

## Reading the results

Report positive mutants and equivalent controls separately. For each family, distinguish planned and eligible variants, attempted trials, successfully generated or constructed formulas, conclusive comparisons, detected differences, and inconclusive outcomes.

Two denominators answer different questions:

- **Conditional detection rate:** `difference_rate_comparable`, detections among fully classified positive trials. Full classification requires both directional results to be conclusive. A single SAT direction with an inconclusive other direction establishes a difference, but is excluded from this conditional denominator.
- **Operational detection yield:** `operational_detection_yield`, detected differences divided by all planned eligible positive trials. It includes a one-direction SAT detection even if full classification is unavailable. Missing generation, unsupported extraction, and unknown-without-SAT remain explicit without pretending they are proven nondetections.

Expected-mutation preservation has its own denominator and does not equal raw difference detection. Formal-control false alarms require a detected difference for an expected-equivalent formal pair; missing evidence remains unavailable. Source-control disagreement with the canonical reference can instead reflect a pre-existing baseline mistake, so it is not automatically a comparator false alarm. Report exclusions and incomplete trials beside rates, not only in a footnote. Zero-denominator outcomes are JSON `null`, never perfect scores. `inconclusive` in summary counts means that full directional classification is unavailable; inspect `difference_detected` because a difference can still be established by one SAT query.

The source report adds these fields:

| Field | Interpretation |
|---|---|
| `baseline_matches_reference` | Whether the target's baseline interpretation is equivalent to its canonical reference |
| `expected_reference_relation_verified` | Whether the constructed formal mutation has its declared relation under the context |
| `expected_mutation_preserved` | Whether the generated changed-source target is equivalent to the expected mutated formula |
| `attributable_preserved_change` | All three preceding checks are conclusively true; not a stakeholder-fidelity claim |
| `expected_relation_met` | Whether raw generated-versus-canonical direction matches the declared relation; weaker evidence than exact expected-mutation preservation |

Source controls have three separate counts in `source_metrics`:

- `control_reference_disagreements`: the control's generated formula differs from the canonical reference; this may include baseline errors.
- `control_generated_meaning_changes`: the control's generated formula differs from the original-source generated formula.
- `control_changes_from_equivalent_baseline`: a generated meaning change is detected after establishing that the original-source baseline agreed with the canonical reference.

Do not relabel the first count as false alarms caused by the paraphrase. A source comparator can correctly identify a pre-existing wrong formula, or a stochastic generation may introduce an unrelated defect. Source reports omit the formal summary's `false_alarms` field to avoid that attribution.

At the report level, `source_metrics.expected_mutation_preserved` counts successes across mutants and controls; `attributable_preserved_mutants` counts positive mutants only. `baseline_reference_mismatches` counts conclusive nonequivalent baseline requirement/repetition pairs, and `baseline_inconclusive` counts unavailable full classifications. These counts have different units from `planned_variant_trials`. Use the per-trial records to compute explicitly declared subgroup rates; do not divide unrelated totals.

The constructed demonstration exercises query execution and evidence retention. It does not establish the semantic fidelity of newly generated models. Reference interpretations and their review status are caller-supplied inputs.

## Verify the implementation

Run the focused offline regressions from the repository root:

```bash
python -m unittest discover -s tests -p 'test_mutation*.py' -v
```

The regressions exercise query directions, equivalent controls, applicability/type checks, infeasible contexts, unavailable evidence, replay attribution, and source-adapter behavior. Tests using real Z3 require the executable; process-level failure fixtures check timeout and error handling. They do not invoke an LLM provider. Passing these tests establishes implementation behavior on their fixtures, not empirical semantic fidelity.

## Relationship to conversion

Mutation campaigns are separate from [requirements conversion](CANONICAL_CLI.md). Source mode invokes the configured converter on original and changed inputs; formal mode compares authored formulas. Neither mode silently modifies a prior candidate, source packet, or admission policy. Expected reference formulas stay outside generation and repair prompts.

The batch `study` interface can generate paired conversion candidates, but the mutation utility does not create a matched batch experiment automatically. Callers select manifests, budgets, repetitions, and analysis procedures explicitly.
