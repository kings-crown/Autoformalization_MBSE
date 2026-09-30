# Runtime abstraction policy

The canonical converter uses `mbse_abstraction/1` to describe what each generated requirement constraint means and what it leaves outside the model. This is an implementation guide; study design, judging protocols and literature rationale are maintained separately.

The policy is defined in [canonical_abstractions.py](../scripts/canonical_abstractions.py), enforced structurally by [canonical_tlr.py](../scripts/canonical_tlr.py), and used by the [canonical CLI](CANONICAL_CLI.md). The CLI and main GUI call the same conversion services.

## Supported meanings

The current `mbse_tlr/1` executable profile contains typed Boolean and linear scalar expressions describing one observation. New formalizations declare the root field `"abstraction_policy": "mbse_abstraction/1"`.

| `kind` | Meaning | Example | Limit |
|---|---|---|---|
| `state_constraint` | A relation between defined quantities or conditions in one observation. | `battery_voltage <= 28 V` | Does not describe state transitions or establish implemented behavior. |
| `capability` | Availability of a named operation to a named subject. | The framework supports configuring TTL. | Availability does not imply use, completion, delivery or successful implementation. |
| `event_relation` | A guarded relationship for one selected symbolic occurrence. | `received -> actual_recipient = specified_recipient` | Does not require reception, quantify over event histories or establish eventual delivery. |

A Boolean condition such as `maintenance_active` is permissible when its meaning is defined. A Boolean meaning only “this requirement is satisfied” is not an adequate abstraction. The validator rejects common requirement-truth placeholder names and whole-formula truth constants; this syntactic protection cannot detect every semantically empty predicate.

## Records and bindings

Every supported record under the policy needs:

- `status: "supported"` and an executable `formula` over declared variables.
- An `abstraction` object containing exactly `kind`, `meaning`, `scope` and `limitations`, plus the capability-specific fields below.
- A nonempty `description` for every variable referenced by that formula.

Capability records additionally require `subject`, `operation` and `symbol`. The symbol must name a declared `Bool` variable. The formula must directly require that current-observation variable; a capability label cannot be attached to an unrelated arithmetic or behavioral formula.

Event-relation formulas must have an outer `implies` operator and reference at least two distinct declared symbols. These are structural checks. They do not establish that the selected participants, guard or equality preserve the source meaning.

`limitations` is a list of at most 20 nonempty descriptions. Capability and event-relation records must include at least one limitation. A state constraint may supply an empty list, although meaningful limits should be documented whenever relevant.

For example, the following requirement record uses a previously declared Boolean variable with a description identifying whose configuration capability it denotes:

```json
{
  "id": "CFG-001",
  "status": "supported",
  "formula": {"var": "supports_ttl_configuration"},
  "abstraction": {
    "kind": "capability",
    "subject": "Messaging framework",
    "operation": "Configure the message TTL",
    "symbol": "supports_ttl_configuration",
    "meaning": "The framework provides the ability to configure TTL.",
    "scope": "Availability of the configuration operation.",
    "limitations": ["Does not establish execution or delivery behavior."]
  }
}
```

This is suitable only for a source obligation about configuration availability. If the source also constrains the actual TTL or delivery behavior, this single predicate does not cover those additional obligations.

## Types, units and source context

Variables use `Bool`, `Int` or `Real`. Numeric variables can declare units and lower/upper bounds; numeric literals include their units where needed. Validation checks expression sorts and dimensions and normalizes quantities to exact canonical-unit magnitudes. The [worked TLR fixture](../examples/canonical/tlr.json) includes voltage and current constraints.

The available sorts do not include a dedicated identity sort. If an occurrence relation uses scalar symbols for opaque participant identities, their descriptions must explain that interpretation. The policy calls for equality, not invented numeric ordering; the type checker cannot itself infer which integers denote identities.

Context determines meaning. For example, TTL must be modeled as a hop count when the supplied source defines it that way. Its name alone does not justify interpreting it as seconds or importing an undocumented protocol bound. Variable descriptions and abstraction text expose these choices but do not prove they are correct.

Background assumptions are separate from requirement guarantees. Declare each assumption with an ID, explanatory text and predicate. Do not put the target guarantee into the background merely to make a query pass, or silently add environmental restrictions to resolve a source conflict.

When original source records are supplied to `validate_tlr`, the generated requirement IDs must match exactly. The validator preserves their ordering and source records and rejects changed original wording. These checks preserve the input text; they do not prove that a generated formula expresses it.

## Unsupported and unresolved records

An obligation outside the supported profile remains in the model as source-linked documentation. Such a record carries a nonempty `reason`, no executable `formula`, and no supported `abstraction` object.

| `status` | Permitted `reason_code` | Intended use |
|---|---|---|
| `unsupported` | `profile_limit` | The meaning needs unavailable operators or structures. |
| `unsupported` | `resource_limit` | Encoding would exceed declared implementation limits. |
| `unresolved` | `source_ambiguity` | Alternative source readings change the obligation. |
| `unresolved` | `missing_context` | A necessary definition or external contract is unavailable. |

Do not label a clear requirement ambiguous simply because the current encoder cannot express it. Conversely, do not choose an unstated interpretation just to produce an executable constraint.

The canonical profile does not implement temporal operators, transitions, ordering across events, persistence, eventuality, probability or quantified populations. It does not establish cryptographic correctness or compatibility with an unspecified API. A same-state implication cannot substitute for “process this automatically afterwards.”

If one source record mixes expressible and inexpressible obligations, a weakened projection must not be marked fully supported. The policy retains the record as unsupported until separately identified obligations are prepared without changing the source meaning. Conversion does not silently split the source.

The present validator accepts 1–1000 requirement records, at most 24 variables and 40 assumptions, with expression depth limited to 16 and 1600 nodes per validation call. A packet containing only unsupported/unresolved records may have no variables or executable assumptions.

## What the resulting artifacts establish

```text
Source text + context
          |
          v
TLR formula + symbol meanings + abstraction limits
          |
          +--> typed/unit validation --> SMT queries and results
          |
          +--> shared renderer -------> SysML constraints and documentation
```

The SysML renderer preserves supported formulas as requirement constraints and includes source wording, declared meanings and limitations. Unsupported and unresolved requirements remain documented without executable obligations. Numeric attributes use scalar types with canonical units documented in metadata; the renderer does not claim native physical-quantity typing.

SAT establishes a satisfying assignment for the encoded query. Compiler acceptance establishes that the generated artifact passes that compiler. Neither outcome establishes source fidelity, implemented capability, temporal behavior or engineer approval.

The representation summary therefore leaves `semantic_fidelity` as `unassessed` and `implementation_verified` as `false`. It reports constraint coverage and abstraction kinds separately. Older supplied TLR artifacts without the policy declaration remain readable as legacy/unclassified records; loading them does not retrospectively approve their interpretation.

For execution options and generated evidence, see the [CLI guide](CANONICAL_CLI.md). For repair budgets and source-preservation guards, see [solver feedback repair](SOLVER_FEEDBACK_REPAIR.md).
