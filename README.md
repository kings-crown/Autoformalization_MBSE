# Autoformalization MBSE Toolkit

A working prototype for converting contextualized natural-language requirements into a typed logical representation (TLR), Z3 evidence, and SysML v2 models for engineer inspection. The CLI and GUI use the same conversion implementation.

This repository contains the reusable implementation, tests, synthetic regression fixtures, and usage documentation. Research protocols, source corpora, assessment references, and publication drafts are maintained separately and are not runtime dependencies.

## Requirements to SysML

```text
Prepared requirements + source context
                   |
                   v
             LLM interpretation
                   |
                   v
              Typed TLR AST <-------------------------+
                   |                                 |
           Schema / type / unit checks               |
                   |                                 |
           +-------+-----------+                     |
           |                   |                     |
           v                   v                     |
      SysML renderer       SMT encoder               |
           |                   |                     |
        Compiler            Z3 audits                |
           |                   |                     |
     Conformance          Exact queries /            |
     outcome              results / witnesses        |
           |                   |                     |
           +-------+-----------+                     |
                   |                                 |
          Candidate and evidence                     |
                   |                                 |
       Optional bounded TLR refinement --------------+
       using permitted source/solver feedback
                   |
                   v
           Engineer inspection
```

Both structured encoders consume the same validated AST. The [abstraction policy](docs/ABSTRACTION_POLICY.md) distinguishes state constraints, capability availability, and relations for a selected occurrence. Declared meanings and limits accompany each supported requirement. Compilation and satisfiability do not establish source fidelity or engineer approval. Compiler failures stop semantic repair.

Install the Python dependencies and configure the Codex CLI, Z3, and the SysML compiler as described in the [CLI guide](docs/CANONICAL_CLI.md):

```bash
python -m pip install -r requirements-review.txt

python scripts/requirements_pipeline.py run \
  --statement requirements.csv \
  --output-dir out/requirements_demo \
  --condition C \
  --model "$MODEL_ID"
```

Use a prepared JSON source packet when requirements depend on nested glossary entries, surrounding obligations, or assumptions. Input and output paths are caller-supplied; source preparation is separate from conversion. `run` is optional. Use a fresh output directory. Generation invokes the configured LLM unless an artifact is supplied.

For an offline fixture demonstration with no LLM call:

```bash
python scripts/requirements_pipeline.py run \
  --statement examples/canonical/requirements.json \
  --tlr-file examples/canonical/tlr.json \
  --output-dir out/canonical_example \
  --condition C
```

## Conversion options and repair

The default structured path validates TLR, renders SysML, runs Z3 audits, and retains scoped admission evidence. The CLI also supports direct SysML generation and structured rendering without solver audits. These are execution options; research comparisons and their protocols are maintained outside this toolkit. See the [CLI guide](docs/CANONICAL_CLI.md) for option names and behavior.

The [feedback guide](docs/SOLVER_FEEDBACK_REPAIR.md) documents `--feedback-repairs K`: structured conversions receive source review, while solver-audited conversion can also receive its current solver evidence. Both regenerate from revised TLR, preserve source/context and existing variable definitions, and retain attempts and stop reasons. The separate [abstention-recovery policy](docs/CANONICAL_CLI.md#bounded-abstention-recovery) handles unsupported or unresolved outcomes under its own budget.

For declared development assistance, use `--development-scenarios path/to/suite.json` with positive feedback budget. The [scenario guide](docs/DEVELOPMENT_SCENARIO_REFINEMENT.md) defines the schema, solver queries, regression gate, and failure behavior. Expected scenarios used during repair are development inputs, not held-out assessment.

## Validation and evaluation

The toolkit tests several different properties of a formalization. A well-formed, compiling, satisfiable model can still misinterpret the source. Automatic checks, controlled semantic comparisons, and source-fidelity review therefore produce separate evidence.

### Automatic representation, compiler, and solver checks

The default structured conversion performs the following checks. Direct SysML generation has compilation checks but no TLR-based Z3 audit; structured rendering without solver audits does not produce solver evidence.

| Check | What it tests | How to interpret the result |
|---|---|---|
| Source inventory and TLR validation | Source IDs/text, declared symbols, supported operators, expression types, and compatible units | Rejects malformed encodings and lost source records; does not determine whether a formula captures the source's meaning. Unsupported or unresolved clauses retain explicit reasons. |
| SysML compilation | Whether the emitted model is accepted by the configured compiler | Establishes compiler conformance, not source fidelity or design correctness. Diagnostics are retained. |
| Background feasibility | Whether declared domains and environmental assumptions admit a valuation | An inconsistent background blocks dependent conclusions; it cannot make a test pass vacuously. |
| Joint requirement consistency | Whether all supported formulas can hold together under that background | SAT means a consistent encoded specification; UNSAT identifies a conflict, not which source clause is wrong. |
| Conditional-trigger feasibility | Whether a top-level implication's trigger is possible under the background and, separately, the feasible full specification | Exposes unreachable triggers and triggers excluded by the full specification. Nested guards are not individually extracted. |
| Violatability | Whether the background permits a valuation violating a requirement, without assuming that requirement | A SAT witness illustrates what the requirement forbids. It is not a failing implementation trace. |
| Redundancy | Whether the remaining requirements entail the target, after establishing that the remaining specification is feasible | Exposes a logically redundant requirement under the recorded context; does not authorize deleting the source clause. |

For example, a requirement `voltage <= 28 V` can be consistent while its violatability query returns `29 V`. These answers are compatible: the second query deliberately omits the requirement to show its effect. A constraint that the background already guarantees, or an implication with an impossible trigger, requires a different interpretation.

Admission requires every source clause to be supported, background and full supported conjunction to be SAT, and compilation to pass. Redundancy and unreachable-trigger findings are reported, not universal admission vetoes. Unknown, timeout, solver/encoding error, unsupported, and blocked outcomes remain distinct. The checks concern the supported static Boolean/linear-numeric representation, not temporal behavior or a separately supplied design. See the [audit and admission guide](docs/CANONICAL_CLI.md#4-what-the-audits-mean).

### Mutation testing

[Mutation testing](docs/MUTATION_STRESS_TEST.md) asks whether a deliberately changed requirement or formula has the expected semantic effect. It is a separate comparison tool, not an extra runtime admission gate.

The supported fault families are:

| Family | Example edit |
|---|---|
| Required-response suppression | Remove the consequence required after a trigger; this removes an obligation, rather than requiring the response to be absent. |
| Operating-guard removal | Make a conditional obligation unconditional. |
| Value or binding mismatch | Change a specified value or bind a constraint to a different, type-compatible quantity. |
| Limit violation | Shift a numeric threshold or exchange a strict and inclusive endpoint. |

Equivalent controls include correctly converted units, reordered conjunctions, double negation, and explicitly equivalent source paraphrases. They check false alarms and unintended changes. Each manifest supplies the target, applicability, rationale, fixed context, and expected relation; the tool does not invent mutations merely to fill a category.

**Formal mode** compares a supplied reference expression with its explicit mutant. For background `Gamma`, original `F0`, and changed formula `F1`, it first establishes background feasibility, then asks:

```text
Newly permitted: Gamma AND F1 AND NOT F0
Newly forbidden: Gamma AND F0 AND NOT F1
```

SAT only in the first direction means weakening; SAT only in the second means strengthening; SAT in both means changes in both directions; UNSAT in both establishes equivalence within this fragment and background. Inconclusive queries do not establish equivalence. SAT queries retain distinguishing valuations. Changing `count >= 10` to `count >= 9`, for example, admits nine: both formulas are consistent, but the reference comparison exposes the weakening.

Run the supplied formal demonstration with Python and Z3, without an LLM call:

```bash
python scripts/mutation_stress.py validate \
  --manifest examples/mutations/demo.json

python scripts/mutation_stress.py formal \
  --manifest examples/mutations/demo.json \
  --output out/mutations_demo
```

**Source-regeneration mode** tests the converter, not just the comparator. Each repetition generates the complete original source bundle once, then generates a fresh complete bundle per eligible variant with only the target clause edited. The generator receives the source and fixed vocabulary/background, not the expected formulas or evaluation labels. The evaluator separately records:

1. Whether the baseline output agrees with its reference.
2. Whether the changed-source output differs from the original reference.
3. Whether it agrees with the intended changed reference; any difference alone is insufficient.
4. Whether untouched clauses changed unexpectedly.
5. The workflow's own compilation and consistency outcomes.

This mode invokes the configured LLM and consumes inference budget:

```bash
python scripts/mutation_stress.py source \
  --manifest examples/mutations/scalar_source.json \
  --output out/mutations_source \
  --model "$MODEL_ID" \
  --repetitions 1
```

Use new output directories. The formal fixture has four fault mutants and four equivalent controls; the source fixture has six variants, hence seven baseline/variant workflow invocations per repetition. These are synthetic examples, not empirical detection rates. Source mode defaults to zero abstention repairs; an explicit positive repair budget can add model calls. The `replay` command rechecks saved normalized candidates without generation.

Missing/unsupported formulas, failed generation, context drift, and inconclusive solver results are retained as such, not credited as detections or successful controls. Comparisons operate on TLR expressions, not independently recovered semantics from the emitted SysML. Reference quality and justified background assumptions remain essential.

### Development scenarios and repair regression

Optional [development scenarios](docs/DEVELOPMENT_SCENARIO_REFINEMENT.md) supply source-grounded predicates with expected SAT or UNSAT outcomes. For an inclusive upper bound of 28 V, allowed cases at 27 V and 28 V and a forbidden case at 29 V can expose an erroneous strict endpoint, assuming the background and other requirements permit the intended cases.

Before crediting a result, the checker requires a feasible background, a feasible candidate specification, executable target clauses, and a scenario feasible without the candidate requirements. It then checks the candidate together with the scenario. Expected SAT means at least one permitted completion, not that every completion or an eventual response is guaranteed.

Enable the suite through `--development-scenarios PATH` with a positive `--feedback-repairs K` in solver-audited conversion. Source/context and expectations remain fixed. A proposal cannot replace the selected candidate if it breaks a previously passing scenario or has invalid/inconclusive scenario checks; rejected attempts remain recorded. Before/after formula comparisons show weakening, strengthening, equivalence, or changes in both directions. Passing cases do not establish correctness outside the suite.

These scenarios intentionally guide development. They are not held-out final evaluation, and neither final judgments nor held-out mutation answers belong in repair inputs.

### Independent source-fidelity review

Inspect the actual generated SysML against the complete contextualized source, requirement by requirement: obligations, guards, quantities, units, boundaries, exceptions, omitted behavior, and invented assumptions. A compiler pass or solver witness cannot settle these natural-language questions. Record unsupported meaning and reviewer uncertainty rather than interpreting satisfiability as fidelity. Engineering approval remains separate from automated admission.

### Retained evidence

Conversion records include the normalized `tlr.json`, generated `model.sysml`, `compilation.json`, and `audit/audit.json`, with exact executed SMT queries, solver results, and available witnesses. Optional mutation and scenario runs preserve their supplied inputs, comparisons, outcomes, and rejected proposals in separate output directories. These generated JSON/result files are run evidence, unlike the small authored fixtures under `examples/`.

## GUI

```bash
python scripts/review_server.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Upload requirements and context, choose conversion options, and inspect SysML and evidence. The GUI defaults repair budgets to zero; explicit equivalent inputs and settings use the same checks and refinement services as the CLI.

The [GUI guide](docs/GUI_CANONICAL_WORKFLOW.md) documents inspection, separate evaluation jobs, recorded engineering opinions, and artifacts. Runs default to `out/review_workbench/canonical`. The [workbench guide](prototypes/review-workbench/README.md) documents compatibility tools at `/legacy`. Sharing the implementation does not establish GUI usability or reduced engineer effort.

## Implementation regression tests

```bash
python -m unittest discover -s tests -p 'test_canonical*.py' -v
python -m unittest discover -s tests -p 'test_mutation*.py' -v
python -m unittest discover -s tests -p 'test_encoding_preservation.py' -v
```

These tests check source inventory/order and unit/type validation; SysML/SMT rendering from the same normalized expressions; compiler and solver integration; non-vacuous audit, mutation, and scenario outcomes; equivalent controls; fixed-context and repair-regression rules; and explicit failure handling. Evidence-preservation tests ensure obligations are not silently discarded and that the pipeline does not manufacture an UNSAT result by inserting `(assert false)`. Raw model answers are retained; unavailable usage/cost is not invented.

Tests use small repository fixtures, temporary outputs, and mocked model calls, not an external research dataset. Solver/compiler integration tests skip when the required tools are absent, so inspect skip counts before claiming those paths were exercised. Passing regressions establishes behavior on those fixtures, not universal encoder equivalence, general source-to-model fidelity, or deployed-system correctness.

## License

MIT (see [LICENSE](https://github.com/kings-crown/Autoformalization_MBSE?tab=MIT-1-ov-file)).
