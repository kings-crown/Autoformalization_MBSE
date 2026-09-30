# Synthetic executable-TLR example

These three authored clauses cover a voltage bound, a Boolean conditional obligation, and a conditional current bound. They exercise the static `mbse_tlr/1` profile; they do not model charging dynamics or establish controller correctness.

- `requirements.json`: original synthetic clauses with source labels.
- `tlr.json`: authored executable interpretation. It is not an LLM response or an independently approved reference.
- `context.json`: the fixed typed vocabulary and background for a repeatable conversion demonstration or mutation comparison. Nonnegative voltage and current bounds are explicit background assumptions; requirement limits remain in the requirement formulas.

From the repository root, execute the complete structured path without a generation call:

```bash
python scripts/requirements_pipeline.py run \
  --statement examples/canonical/requirements.json \
  --tlr-file examples/canonical/tlr.json \
  --context-file examples/canonical/context.json \
  --condition C \
  --output-dir out/canonical_example
```

This renders a SysML candidate from the supplied TLR, compiles it, and executes real Z3 audits. Existing output directories are rejected. To exercise actual LLM generation, omit `--tlr-file`; that invokes the configured generation transport.

The fixtures validate execution mechanics only. Solver consistency and compiler acceptance are separate from semantic fidelity and engineer approval.
