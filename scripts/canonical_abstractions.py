"""Shared requirements-abstraction contract, not an implementation proof.

The schema validates declared scope and bindings. Source grounding still needs
independent assessment; a nonempty description is not evidence of faithfulness.
"""
from copy import deepcopy

POLICY_VERSION = "mbse_abstraction/1"
PROFILE = "Typed Boolean and linear scalar requirement abstractions: state constraints, declared capability availability, and single-occurrence relations. No transition, temporal, probabilistic or quantified operators."
POLICY = {
    "version": POLICY_VERSION,
    "kinds": {
        "state_constraint": "A relation between defined quantities/conditions in one observation.",
        "capability": "Availability of a named operation to a named subject; not execution or successful completion.",
        "event_relation": "A guarded relation for a symbolic occurrence with explicit participant/quantity meanings; not event existence, order or eventuality.",
    },
    "reason_codes": {
        "profile_limit": "Meaning needs operators or structures outside this profile.",
        "source_ambiguity": "Multiple source interpretations change the obligation.",
        "missing_context": "A definition or external contract needed to determine the obligation is unavailable.",
        "resource_limit": "An encoding exceeds the declared implementation limits.",
    },
    "claim": "Constraints represent the stated abstraction. Neither SAT nor a capability predicate establishes an implementation, delivery, temporal behavior or stakeholder intent.",
}
POLICY_TEXT = '''Shared abstraction policy mbse_abstraction/1 (applies equally to direct SysML, TLR, and assessment):
1. Separate representing a requirement from proving an implementation realizes it. Boolean predicates are permitted when their subject, operation or observed condition, meaning and scope are explicit in the contextualized source and model. A predicate defined only as 'this requirement is satisfied' is not permitted, regardless of its spelling.
2. A pure 'shall support the ability to X' requirement may be represented by a declared capability predicate whose truth means that operation X is available to the named subject. Configuration availability (such as ability to set TTL) is also a capability. Constrain availability, not actual use. This is capability-level requirement coverage; it never proves execution, successful delivery, timing, security or a deployed implementation. Do not reject a pure capability solely because the source supplies no implementation. Do not use a support flag to replace obligations about actual behavior.
3. State constraints preserve defined conditions, guards, units and bounds. A Boolean observed condition (e.g. maintenance active) is not an opaque requirement-truth flag. An explicit prohibition of capability availability is a state_constraint negating the SAME defined availability symbol used by the positive capability; it is not a new independent 'prohibition supported' flag. A pure positive capability uses kind capability and asserts that symbol directly. Preserve both clauses when the source requires and prohibits the same availability in the same scope; a contradiction is evidence to report, not permission to weaken either clause or declare its meaning unexpressible. Distinguish a clear conflict from genuinely ambiguous scope. Do not invent bounds absent from the source; TTL is a hop count when the source defines it that way, not seconds.
4. A single-occurrence abstraction may express received -> actual_recipient = specified_recipient with named meanings and occurrence scope. Abstract identifiers need not have concrete IP addresses: use opaque symbolic identities and equality, never an invented numeric ordering. Such a restriction does not assert that receipt occurs, that messages arrive eventually, or that all events/history have been verified. Preserve the source's modality and best-effort exceptions. If a missing identity granularity changes the obligation, disclose the question instead of silently choosing it.
5. Ordering, before/after history, persistence, eventuality, cardinality over an unspecified population, cryptographic correctness and compatibility with an unspecified API are not established by these static predicates. A same-state encrypted -> decrypted formula cannot stand for automatic processing; a support flag cannot stand for an actual recipient restriction. Preserve such source clauses and explain the specific residual meaning. Do not call ordinary capabilities invalid requirements or call an implementation limit source ambiguity.
6. Record each supported abstraction's kind, meaning, scope and limitations. Capability records additionally identify subject, operation and bound Boolean symbol. Describe every symbol used by a new abstraction. If a clause has expressible and inexpressible conjuncts, do not give a weakened projection full coverage: retain it as unsupported in this profile until separately identified obligations are prepared without changing source meaning. No silent source splitting or new environmental assumptions.
7. Assessment concerns preservation at the source's abstraction level. A declared availability predicate can cover a pure capability assertion, but must fail coverage of an operational, temporal or relational obligation it omits. Honest limitation disclosure and absence of invented assumptions are separate outcomes, not executable-coverage credit. Inspect definitions, bindings and actual constraints; a name, comment, kind label, compiler success or solver success alone is insufficient evidence. Model-supplied scope cannot narrow the source to excuse a missing obligation.
8. Unspecified realization details alone do not block a pure capability. Distinguish a missing source meaning that changes the obligation from a missing implementation choice. Capability bindings must cover the whole obligation; availability never substitutes for an omitted guard, execution/history, timing or a residual conjunct. Source-grounded feedback reviews proposed abstractions; independent final assessment and engineer approval remain separate.
9. Generation and assessment use the same policy and any supplied fixed vocabulary. Any proposed interpretation change remains explicit; do not repair source conflicts by weakening requirements. Legacy artifacts without abstraction records remain readable but unclassified, not retrospectively approved under this policy.'''


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError(f"{label} needs nonempty text (at most 4000 characters)")
    return value


def validate_abstraction(row, variables, required=False):
    """Return scoped metadata; check binding shape without inferring NL meaning."""
    raw = row.get("abstraction")
    if row["status"] != "supported":
        if raw is not None:
            raise ValueError("Unsupported/unresolved rows cannot claim a supported abstraction")
        code = row.get("reason_code")
        if required or code is not None:
            codes = {"profile_limit", "resource_limit"} if row["status"] == "unsupported" else {"source_ambiguity", "missing_context"}
            if not isinstance(code, str) or code not in codes:
                raise ValueError(f"{row['id']}: reason_code must distinguish profile limits from missing/ambiguous source meaning")
        return None
    if "reason_code" in row:
        raise ValueError("Supported rows cannot have an abstention reason_code")
    if raw is None and not required:
        return None
    fields = {"kind", "meaning", "scope", "limitations"}
    if not isinstance(raw, dict) or not fields <= set(raw):
        raise ValueError(f"{row['id']}: supported abstraction needs kind, meaning, scope and limitations")
    kind = raw["kind"]
    if not isinstance(kind, str) or kind not in POLICY["kinds"]:
        raise ValueError("Unknown abstraction kind")
    allowed = fields | ({"subject", "operation", "symbol"} if kind == "capability" else set())
    if set(raw) != allowed:
        raise ValueError(f"{row['id']}: abstraction fields must be {sorted(allowed)}")
    for key in allowed - {"limitations"}:
        _text(raw[key], f"Abstraction {key}")
    limits = raw["limitations"]
    if not isinstance(limits, list) or len(limits) > 20:
        raise ValueError("Abstraction limitations must be a list of at most 20 explicit limits")
    for limit in limits:
        _text(limit, "Abstraction limitation")
    if kind in {"capability", "event_relation"} and not limits:
        raise ValueError("Capability/occurrence abstractions must explicitly disclose their limits")
    from canonical_tlr import _references
    refs = _references(row["formula"])
    by_name = {v["name"]: v for v in variables}
    for name in refs:
        _text(by_name[name].get("description"), f"Meaning of symbol {name}")
    if kind == "capability":
        symbol = raw["symbol"]
        formula = row["formula"]
        if (symbol not in by_name or by_name[symbol]["type"] != "Bool"
                or formula.get("var") != symbol or set(formula) - {"var", "at"}
                or formula.get("at", "current") != "current"):
            raise ValueError("A capability abstraction must require its declared Boolean availability symbol directly")
    if kind == "event_relation" and (row["formula"].get("op") != "implies" or len(refs) < 2):
        raise ValueError("An event relation needs a guarded relation over distinct declared symbols")
    return deepcopy(raw)


def representation_summary(tlr):
    rows = tlr["requirements"]
    kinds = {kind: 0 for kind in (*POLICY["kinds"], "unclassified")}
    for row in rows:
        if row["status"] == "supported":
            kinds[row.get("abstraction", {}).get("kind", "unclassified")] += 1
    count = sum(kinds.values())
    return {"policy": tlr.get("abstraction_policy"), "requirements": len(rows),
            "constraints_generated": count, "by_kind": kinds,
            "by_status": {s: sum(r["status"] == s for r in rows) for s in ("supported", "unsupported", "unresolved")},
            "formalization_status": "no_executable_formalization" if not count else "partial" if count < len(rows) else "constraints_generated",
            "semantic_fidelity": "unassessed", "implementation_verified": False,
            "claim": POLICY["claim"]}
