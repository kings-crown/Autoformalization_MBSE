import { logicalFormShapeDescription } from '../tlf/schema.js';

export function buildPrompt(requirementsDoc) {
  const summary = [
    `Project: ${requirementsDoc.title || requirementsDoc.id || 'Unnamed project'}`,
    requirementsDoc.system ? `System: ${requirementsDoc.system}` : null,
    requirementsDoc.assumptions?.length
      ? `Assumptions:\n${requirementsDoc.assumptions.map((a, idx) => `  ${idx + 1}. ${a}`).join('\n')}`
      : null,
    'Functional requirements:',
    ...(requirementsDoc.requirements || []).map((req, idx) => `  ${req.id || `R${idx + 1}`}: ${req.text}`)
  ]
    .filter(Boolean)
    .join('\n');

  const instructions = [
    'Transform the requirements into a Typed Logical Form (TLF) that captures the signature and axioms needed for verification with Z3.',
    'Follow these rules:',
    '- Provide only JSON with no additional commentary.',
    '- Prefer enumerations for qualitative states (e.g. door state OPEN vs CLOSED).',
    '- Include requirement IDs in each axiom for traceability.',
    '- Introduce intermediate functions or predicates when required to capture temporal or conditional behaviour.',
    '- Assume standard SMT-LIB built-in sorts (Bool, Int, Real) are available.',
    '- Use multi-step reasoning only to arrive at a clean, deterministic JSON output.'
  ].join('\n');

  const schemaReminder = `The JSON MUST conform to the following shape:\n${logicalFormShapeDescription}`;

  return [
    {
      role: 'system',
      content:
        'You are a formal methods assistant that converts natural-language requirements into machine-checkable logical specifications. ' +
        'You produce deterministic JSON that matches the requested schema exactly.'
    },
    {
      role: 'user',
      content: `${summary}\n\n${instructions}\n\n${schemaReminder}`
    }
  ];
}
