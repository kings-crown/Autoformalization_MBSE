import { translateRequirementsToLogicalForm } from './llm/translator.js';
import { validateLogicalForm } from './tlf/schema.js';
import { canonicaliseLogicalForm } from './tlf/transformers.js';
import { logicalFormToSmtlib } from './tlf/smtlibGenerator.js';
import { solveWithZ3 } from './solver/wasmSolver.js';

export async function runPipeline({ requirements, options = {} }) {
  const llmOptions = {
    model: options.model
  };

  const logicalFormDraft = await translateRequirementsToLogicalForm(requirements, llmOptions);
  const logicalFormValidated = validateLogicalForm(logicalFormDraft);
  const logicalForm = canonicaliseLogicalForm(logicalFormValidated);
  const smtlib = logicalFormToSmtlib(logicalForm);

  let solver = null;
  if (options.skipSolver) {
    solver = { status: 'skipped', reason: 'Solver disabled by CLI flag or environment variable.' };
  } else {
    solver = await solveWithZ3(smtlib, options.solverOptions);
  }

  return {
    logicalForm,
    smtlib,
    solver
  };
}
