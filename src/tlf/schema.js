import { z } from 'zod';

const Identifier = z
  .string()
  .min(1)
  .regex(/^[A-Za-z_][A-Za-z0-9_\-]*$/, 'Identifiers must start with a letter/underscore and contain alphanumerics, _ or -');

const SortName = Identifier;

const EnumSort = z.object({
  name: SortName,
  kind: z.literal('enum'),
  values: z.array(Identifier).min(1)
});

const BuiltinSort = z.object({
  name: SortName,
  kind: z.literal('builtin'),
  builtin: z.enum(['Bool', 'Int', 'Real'])
});

const AliasSort = z.object({
  name: SortName,
  kind: z.literal('alias'),
  target: SortName
});

const UninterpretedSort = z.object({
  name: SortName,
  kind: z.literal('uninterpreted'),
  arity: z.number().int().nonnegative().default(0)
});

const SortSchema = z.discriminatedUnion('kind', [EnumSort, BuiltinSort, AliasSort, UninterpretedSort]);

const Term = z.lazy(() =>
  z.discriminatedUnion('kind', [
    z.object({
      kind: z.literal('identifier'),
      name: Identifier
    }),
    z.object({
      kind: z.literal('bool'),
      value: z.boolean()
    }),
    z.object({
      kind: z.literal('int'),
      value: z.number().int()
    }),
    z.object({
      kind: z.literal('real'),
      value: z.number()
    }),
    z.object({
      kind: z.literal('not'),
      operand: Term
    }),
    z.object({
      kind: z.literal('and'),
      operands: z.array(Term).min(2)
    }),
    z.object({
      kind: z.literal('or'),
      operands: z.array(Term).min(2)
    }),
    z.object({
      kind: z.literal('implies'),
      antecedent: Term,
      consequent: Term
    }),
    z.object({
      kind: z.literal('iff'),
      left: Term,
      right: Term
    }),
    z.object({
      kind: z.literal('eq'),
      left: Term,
      right: Term
    }),
    z.object({
      kind: z.literal('distinct'),
      operands: z.array(Term).min(2)
    }),
    z.object({
      kind: z.literal('lt'),
      left: Term,
      right: Term
    }),
    z.object({
      kind: z.literal('le'),
      left: Term,
      right: Term
    }),
    z.object({
      kind: z.literal('gt'),
      left: Term,
      right: Term
    }),
    z.object({
      kind: z.literal('ge'),
      left: Term,
      right: Term
    }),
    z.object({
      kind: z.literal('add'),
      operands: z.array(Term).min(2)
    }),
    z.object({
      kind: z.literal('sub'),
      minuend: Term,
      subtrahend: Term
    }),
    z.object({
      kind: z.literal('mul'),
      operands: z.array(Term).min(2)
    }),
    z.object({
      kind: z.literal('div'),
      dividend: Term,
      divisor: Term
    }),
    z.object({
      kind: z.literal('ite'),
      condition: Term,
      then: Term,
      else: Term
    }),
    z.object({
      kind: z.literal('let'),
      bindings: z
        .array(
          z.object({
            symbol: Identifier,
            value: Term
          })
        )
        .min(1),
      body: Term
    }),
    z.object({
      kind: z.literal('apply'),
      name: Identifier,
      args: z.array(Term)
    }),
    z.object({
      kind: z.literal('predicate'),
      name: Identifier,
      args: z.array(Term)
    }),
    z.object({
      kind: z.literal('forall'),
      quantifiers: z
        .array(
          z.object({
            symbol: Identifier,
            sort: SortName
          })
        )
        .min(1),
      body: Term
    }),
    z.object({
      kind: z.literal('exists'),
      quantifiers: z
        .array(
          z.object({
            symbol: Identifier,
            sort: SortName
          })
        )
        .min(1),
      body: Term
    })
  ])
);

const SymbolDoc = z.object({
  name: Identifier,
  sort: SortName,
  description: z.string().optional()
});

const FunctionDoc = z.object({
  name: Identifier,
  domain: z.array(SortName),
  codomain: SortName,
  description: z.string().optional()
});

const PredicateDoc = z.object({
  name: Identifier,
  domain: z.array(SortName),
  description: z.string().optional()
});

const AxiomDoc = z.object({
  id: Identifier,
  requirementIds: z.array(z.string().min(1)).min(1),
  description: z.string().optional(),
  formula: Term
});

export const logicalFormSchema = z.object({
  metadata: z.object({
    requirementSetId: z.string().min(1),
    title: z.string().min(1),
    system: z.string().optional(),
    requirements: z.array(
      z.object({
        id: z.string().min(1),
        text: z.string().min(1)
      })
    ),
    assumptions: z.array(z.string()).default([])
  }),
  signature: z.object({
    sorts: z.array(SortSchema).default([]),
    constants: z.array(SymbolDoc).default([]),
    functions: z.array(FunctionDoc).default([]),
    predicates: z.array(PredicateDoc).default([])
  }),
  axioms: z.array(AxiomDoc).min(1)
});

export function validateLogicalForm(candidate) {
  return logicalFormSchema.parse(candidate);
}

export const logicalFormShapeDescription = [
  '{',
  '  "metadata": {',
  '    "requirementSetId": string,',
  '    "title": string,',
  '    "system"?: string,',
  '    "requirements": [{ "id": string, "text": string }, ...],',
  '    "assumptions": string[]',
  '  },',
  '  "signature": {',
  '    "sorts": [',
  '      { "kind": "enum", "name": "SortName", "values": ["Label", ...] } |',
  '      { "kind": "builtin", "name": "Alias", "builtin": "Bool"|"Int"|"Real" } |',
  '      { "kind": "alias", "name": "Alias", "target": "ExistingSort" } |',
  '      { "kind": "uninterpreted", "name": "SortName", "arity": 0 }',
  '    ],',
  '    "constants": [{ "name": "symbol", "sort": "SortName", "description"?: string }],',
  '    "functions": [{ "name": "f", "domain": ["Sort"], "codomain": "Sort", "description"?: string }],',
  '    "predicates": [{ "name": "p", "domain": ["Sort"], "description"?: string }]',
  '  },',
  '  "axioms": [',
  '    {',
  '      "id": "AxiomId",',
  '      "requirementIds": ["FR1", ...],',
  '      "description"?: string,',
  '      "formula": { /* expression AST using kinds: identifier, bool, int, not, and, or, implies, forall, exists, eq, lt, le, gt, ge, add, sub, mul, div, apply, predicate, ite, let */ }',
  '    }',
  '  ]',
  '}'
].join('\n');
