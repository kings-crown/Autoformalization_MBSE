import test from 'node:test';
import assert from 'node:assert/strict';
import { logicalFormSchema } from '../src/tlf/schema.js';
import { canonicaliseLogicalForm } from '../src/tlf/transformers.js';
import { logicalFormToSmtlib } from '../src/tlf/smtlibGenerator.js';
import { getMockLogicalForm } from '../src/llm/mockResponses.js';

test('SMT-LIB generation produces expected sections', async () => {
  const mock = await getMockLogicalForm('door-controller');
  const validated = logicalFormSchema.parse(mock);
  const canonical = canonicaliseLogicalForm(validated);
  const smtlib = logicalFormToSmtlib(canonical);

  assert.match(smtlib, /\(declare-datatypes/);
  assert.match(smtlib, /\(declare-const door_state DoorState\)/);
  assert.match(smtlib, /\(declare-fun next_lock_engaged/);
  assert.match(smtlib, /\(assert/);
  assert.match(smtlib, /\(check-sat\)/);
});
