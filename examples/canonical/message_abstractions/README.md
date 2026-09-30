# Messaging abstraction regression fixture

These three requirements and their context are authored project examples. They are not extracted from a research corpus, generated study results, or independently reviewed reference answers.

- `MSG-SEND` represents availability of a named unicast-sending operation.
- `MSG-RECIPIENT` constrains the receiving address of a selected message when receipt occurs. Its guard does not require delivery, and a generic support flag cannot replace the address relation.
- `MSG-TTL` represents availability of a multicast TTL configuration operation. TTL is explicitly a hop count; the fixture introduces no numeric range or elapsed-time guarantee.

The two integer address symbols represent opaque identities, so only equality has meaning. Both refer to the same selected message. Each TLR record states its abstraction and limitations, and the renderer includes them beside the generated SysML constraints. Solver checks concern the declared abstractions; they do not establish a deployed implementation's behavior.

Run with the solver and SysML compiler, without model calls, using a fresh output directory:

```bash
python scripts/requirements_pipeline.py run \
  --statement examples/canonical/message_abstractions/requirements.json \
  --tlr-file examples/canonical/message_abstractions/tlr.json \
  --context-file examples/canonical/message_abstractions/context.json \
  --condition BC --output-dir out/message_abstraction_example
```

Inspect `representation.json`, `candidate_content.json`, `model.sysml` and `audit/audit.json`. A SAT violatability query gives an abstract state missing a capability or receiving a message at the wrong address. It does not show a failure of a supplied design.

`tests/test_canonical_abstractions.py` also rejects missing symbol meanings, changed fixed meanings, negative capability flags, and recipient relations collapsed to a flag or tautology. See the [canonical CLI guide](../../../docs/CANONICAL_CLI.md) for representation limits and operating modes.
