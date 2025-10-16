const SIMPLE_IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_\-]*$/;

function smtSymbol(name) {
  return SIMPLE_IDENTIFIER.test(name) ? name : `|${name}|`;
}

function emitSort(sortName) {
  return smtSymbol(sortName);
}

function emitTerm(term) {
  switch (term.kind) {
    case 'identifier':
      return smtSymbol(term.name);
    case 'bool':
      return term.value ? 'true' : 'false';
    case 'int':
      return String(term.value);
    case 'real':
      return String(term.value);
    case 'not':
      return `(not ${emitTerm(term.operand)})`;
    case 'and':
      return `(and ${term.operands.map(emitTerm).join(' ')})`;
    case 'or':
      return `(or ${term.operands.map(emitTerm).join(' ')})`;
    case 'implies':
      return `(=> ${emitTerm(term.antecedent)} ${emitTerm(term.consequent)})`;
    case 'iff':
      return `(= ${emitTerm(term.left)} ${emitTerm(term.right)})`;
    case 'eq':
      return `(= ${emitTerm(term.left)} ${emitTerm(term.right)})`;
    case 'distinct':
      return `(distinct ${term.operands.map(emitTerm).join(' ')})`;
    case 'lt':
      return `(< ${emitTerm(term.left)} ${emitTerm(term.right)})`;
    case 'le':
      return `(<= ${emitTerm(term.left)} ${emitTerm(term.right)})`;
    case 'gt':
      return `(> ${emitTerm(term.left)} ${emitTerm(term.right)})`;
    case 'ge':
      return `(>= ${emitTerm(term.left)} ${emitTerm(term.right)})`;
    case 'add':
      return `(+ ${term.operands.map(emitTerm).join(' ')})`;
    case 'sub':
      return `(- ${emitTerm(term.minuend)} ${emitTerm(term.subtrahend)})`;
    case 'mul':
      return `(* ${term.operands.map(emitTerm).join(' ')})`;
    case 'div':
      return `(/ ${emitTerm(term.dividend)} ${emitTerm(term.divisor)})`;
    case 'ite':
      return `(ite ${emitTerm(term.condition)} ${emitTerm(term.then)} ${emitTerm(term.else)})`;
    case 'let':
      return `(let (${term.bindings
        .map((binding) => `(${smtSymbol(binding.symbol)} ${emitTerm(binding.value)})`)
        .join(' ')}) ${emitTerm(term.body)})`;
    case 'apply': {
      const args = term.args.map(emitTerm);
      return args.length
        ? `(${smtSymbol(term.name)} ${args.join(' ')})`
        : `(${smtSymbol(term.name)})`;
    }
    case 'predicate': {
      const args = term.args.map(emitTerm);
      return args.length
        ? `(${smtSymbol(term.name)} ${args.join(' ')})`
        : `(${smtSymbol(term.name)})`;
    }
    case 'forall':
      return `(forall (${term.quantifiers
        .map((q) => `(${smtSymbol(q.symbol)} ${emitSort(q.sort)})`)
        .join(' ')}) ${emitTerm(term.body)})`;
    case 'exists':
      return `(exists (${term.quantifiers
        .map((q) => `(${smtSymbol(q.symbol)} ${emitSort(q.sort)})`)
        .join(' ')}) ${emitTerm(term.body)})`;
    default:
      throw new Error(`Unsupported term kind: ${term.kind}`);
  }
}

function emitEnumDecls(enumSorts) {
  const datatypeBodies = enumSorts
    .map(
      (sort) =>
        `(${smtSymbol(sort.name)} ${sort.values.map((value) => `(${smtSymbol(value)})`).join(' ')})`
    )
    .join(' ');
  return `(declare-datatypes () (${datatypeBodies}))`;
}

function emitAliasDecl(sort) {
  return `(define-sort ${smtSymbol(sort.name)} () ${emitSort(sort.target)})`;
}

function emitUninterpretedDecl(sort) {
  return `(declare-sort ${smtSymbol(sort.name)} ${sort.arity ?? 0})`;
}

function emitConstantDecl(symbol) {
  return `(declare-const ${smtSymbol(symbol.name)} ${emitSort(symbol.sort)})`;
}

function emitFunctionDecl(fn) {
  const domain = fn.domain.map(emitSort).join(' ');
  return `(declare-fun ${smtSymbol(fn.name)} (${domain}) ${emitSort(fn.codomain)})`;
}

function emitPredicateDecl(pred) {
  const domain = pred.domain.map(emitSort).join(' ');
  return `(declare-fun ${smtSymbol(pred.name)} (${domain}) Bool)`;
}

export function logicalFormToSmtlib(logicalForm) {
  const parts = [];
  const header = `; Generated from requirement set ${logicalForm.metadata.requirementSetId}`;
  parts.push(header);
  if (logicalForm.metadata.title) {
    parts.push(`; ${logicalForm.metadata.title}`);
  }

  const enumSorts = (logicalForm.signature.sorts || []).filter((s) => s.kind === 'enum');
  const aliasSorts = (logicalForm.signature.sorts || []).filter((s) => s.kind === 'alias');
  const uninterpretedSorts = (logicalForm.signature.sorts || []).filter(
    (s) => s.kind === 'uninterpreted'
  );

  if (enumSorts.length) {
    parts.push(emitEnumDecls(enumSorts));
  }
  for (const sort of aliasSorts) {
    parts.push(emitAliasDecl(sort));
  }
  for (const sort of uninterpretedSorts) {
    parts.push(emitUninterpretedDecl(sort));
  }

  for (const constant of logicalForm.signature.constants || []) {
    parts.push(emitConstantDecl(constant));
  }

  for (const fn of logicalForm.signature.functions || []) {
    parts.push(emitFunctionDecl(fn));
  }

  for (const pred of logicalForm.signature.predicates || []) {
    parts.push(emitPredicateDecl(pred));
  }

  for (const axiom of logicalForm.axioms) {
    if (axiom.description) {
      parts.push(`; ${axiom.description}`);
    }
    parts.push(`; Traceability: ${axiom.requirementIds.join(', ')}`);
    parts.push(`(assert ${emitTerm(axiom.formula)})`);
  }

  parts.push('(check-sat)');
  parts.push('(get-model)');
  return parts.join('\n');
}
