# Synthetic executable-TLR example

These three authored clauses cover a voltage bound, a Boolean conditional obligation, and a conditional current bound. They exercise the static `mbse_tlr/1` profile; they do not model charging dynamics or establish controller correctness.

- `requirements.json`: original synthetic clauses with source labels.
- `obligation_inventory.json`: historical authored source-only inventory fixture for the optional module tests; ordinary conversion does not load it.
- `tlr.json`: authored executable interpretation; historical component coverage metadata may remain in the fixture. It is not an LLM response or an independently approved reference.
- `context.json`: the fixed typed vocabulary and background for a repeatable conversion demonstration or mutation comparison. Nonnegative voltage and current bounds are explicit background assumptions; requirement limits remain in the requirement formulas.

From the repository root, execute the structured path using the supplied TLR, with no model calls:

```bash
python scripts/requirements_pipeline.py run \
  --statement examples/canonical/requirements.json \
  --tlr-file examples/canonical/tlr.json \
  --context-file examples/canonical/context.json \
  --condition C --feedback-repairs 0 \
  --output-dir out/canonical_example
```

This validates the complete TLR, renders every supported formula, compiles SysML and executes real Z3 audits. Source context and unsupported rows remain attached. Existing output directories are rejected. To exercise LLM generation, omit `--tlr-file`; select `--feedback-repairs 2` for bounded source/solver feedback. Generated standalone B/C commands default to two opportunities unless a budget is explicitly supplied.

The fixtures validate execution mechanics only. Solver consistency and compiler acceptance are separate from semantic fidelity and engineer approval.

The [assertions.json](assertions.json) fixture contains fourteen source-grounded checks for these three requirements, including mandatory coverage and target-scoped assumption checks. It matches the exact `requirements.json` and `context.json` packet. It is a synthetic assessment example, not an empirically validated reference.

[bedrock_judges.example.json](bedrock_judges.example.json) supplies placeholder model IDs for two judge roles and an optional assertion author. Copy and configure it before making Bedrock calls. Use `python scripts/requirements_pipeline.py prepare-assertions --help` and `python scripts/requirements_pipeline.py judge --help` from the repository root for the preparation and assessment options.

The retained inventory and separate-review modules are not prerequisites for this example. Compilation and solver checks are local; independent assertion judging is a separate, explicitly requested inference phase.
