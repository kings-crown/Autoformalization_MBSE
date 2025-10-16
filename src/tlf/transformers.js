function byName(a, b) {
  return a.name.localeCompare(b.name);
}

function deepSortTerm(term) {
  if (!term || typeof term !== 'object') return term;

  switch (term.kind) {
    case 'and':
    case 'or':
    case 'distinct':
    case 'add':
    case 'mul': {
      const operands = term.operands.map(deepSortTerm);
      return { ...term, operands };
    }
    case 'forall':
    case 'exists': {
      return {
        ...term,
        quantifiers: term.quantifiers.map((q) => ({ ...q })),
        body: deepSortTerm(term.body)
      };
    }
    case 'let': {
      return {
        ...term,
        bindings: term.bindings.map((binding) => ({
          symbol: binding.symbol,
          value: deepSortTerm(binding.value)
        })),
        body: deepSortTerm(term.body)
      };
    }
    case 'implies':
    case 'iff':
    case 'eq':
    case 'lt':
    case 'le':
    case 'gt':
    case 'ge':
    case 'sub':
    case 'div': {
      return {
        ...term,
        left: term.left ? deepSortTerm(term.left) : undefined,
        right: term.right ? deepSortTerm(term.right) : undefined,
        antecedent: term.antecedent ? deepSortTerm(term.antecedent) : undefined,
        consequent: term.consequent ? deepSortTerm(term.consequent) : undefined,
        minuend: term.minuend ? deepSortTerm(term.minuend) : undefined,
        subtrahend: term.subtrahend ? deepSortTerm(term.subtrahend) : undefined,
        dividend: term.dividend ? deepSortTerm(term.dividend) : undefined,
        divisor: term.divisor ? deepSortTerm(term.divisor) : undefined
      };
    }
    case 'ite':
      return {
        ...term,
        condition: deepSortTerm(term.condition),
        then: deepSortTerm(term.then),
        else: deepSortTerm(term.else)
      };
    case 'not':
      return { ...term, operand: deepSortTerm(term.operand) };
    case 'apply':
    case 'predicate':
      return { ...term, args: term.args.map(deepSortTerm) };
    default:
      return { ...term };
  }
}

export function canonicaliseLogicalForm(form) {
  return {
    ...form,
    signature: {
      sorts: [...(form.signature.sorts || [])].sort(byName),
      constants: [...(form.signature.constants || [])].sort(byName),
      functions: [...(form.signature.functions || [])].sort(byName),
      predicates: [...(form.signature.predicates || [])].sort(byName)
    },
    axioms: [...form.axioms].map((axiom) => ({
      ...axiom,
      requirementIds: [...axiom.requirementIds].sort(),
      formula: deepSortTerm(axiom.formula)
    }))
  };
}
