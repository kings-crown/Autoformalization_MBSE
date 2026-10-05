"""Bounded review-format recovery; usable semantic decisions are immutable."""
from copy import deepcopy


RECOVERY_INSTRUCTIONS = """Repair only the malformed or unusable review records listed in review_recovery. This is review response completion, not candidate repair or semantic reconsideration. The complete original source and candidate/inventory remain fixed. Return the same review schema, containing only the requested requirement records (and background only if requested). Within a requested requirement, return its parent fields and component reviews; already usable parent/component decisions are immutable and will be retained by the controller. Fix JSON structure, missing records, IDs, literal source citations or AST pointers using the original source/candidate only. Do not change an already usable pass, revise, needs_clarification or unsupported decision or its evidence. Semantic rejection or uncertainty is not a malformed response. Do not rewrite the candidate, inventory, source or context, and do not invent quotations. No solver, final-judge or reference answer is available. Return JSON only.
"""


def validate_budget(value):
    if type(value) is not int or value not in (0, 1):
        raise ValueError("review_repairs must be 0 or 1")


def usable(record):
    return (isinstance(record, dict) and
            record.get("disposition") in {"pass", "revise", "needs_clarification", "unsupported"} and
            not record.get("validation_errors") and not record.get("review_validation_errors"))


def unique_record(records, rid):
    matches = [row for row in records if isinstance(row, dict) and row.get("id") == rid] if isinstance(records, list) else []
    return matches[0] if len(matches) == 1 else None


def recovery_request(review, failure=None):
    """Request only invalid records; derived parent withholding isn't invalidity."""
    affected = []
    for row in review.get("requirements", []):
        parent = row.get("parent_review", row)
        components = [c["id"] for c in row.get("obligations", []) if not usable(c)]
        if not usable(parent) or components or row.get("component_review_errors"):
            affected.append({"id": row["id"], "parent": not usable(parent),
                             "obligation_ids": components,
                             "validation_errors": deepcopy(parent.get("validation_errors", [])),
                             "component_review_errors": deepcopy(row.get("component_review_errors", [])),
                             "component_errors": [{"id": c["id"], "errors": deepcopy(c.get("validation_errors", []))}
                                                  for c in row.get("obligations", []) if not usable(c)]})
    return {"requirements": affected,
            "background": "background" in review and not usable(review["background"]),
            "envelope_errors": deepcopy(review.get("validation_errors", [])),
            "failure": deepcopy(failure)}


def needs_recovery(request):
    return bool(request["requirements"] or request["background"] or request["envelope_errors"] or request["failure"])


def merge_response(original, correction, review, schema):
    """Keep independently usable original records verbatim, even on rejection.

    The correction must have its proper envelope. Duplicate/missing records stay
    malformed rather than allowing a best-answer choice. Only expected records
    enter the merged response; extra correction records cannot overwrite a
    usable source decision.
    """
    keys = {"schema", "requirements"} | ({"background"} if "background" in review else set())
    if not isinstance(correction, dict) or correction.get("schema") != schema or set(correction) - keys or not isinstance(correction.get("requirements"), list):
        return deepcopy(original), "Recovery response has an invalid review envelope"
    old = original if isinstance(original, dict) else {}
    result = {"schema": schema, "requirements": []}
    if "background" in review:
        result["background"] = deepcopy(old.get("background") if usable(review["background"]) else correction.get("background"))
    for row in review["requirements"]:
        rid = row["id"]
        before = unique_record(old.get("requirements"), rid)
        after = unique_record(correction["requirements"], rid)
        parent = row.get("parent_review", row)
        selected = deepcopy(before if usable(parent) else after)
        components = row.get("obligations")
        if components is not None:
            if not isinstance(selected, dict):
                selected = {"id": rid}
            selected["obligations"] = []
            for component in components:
                source = before if usable(component) else after
                found = unique_record(source.get("obligations") if isinstance(source, dict) else None, component["id"])
                if found is not None:
                    selected["obligations"].append(deepcopy(found))
        if selected is not None:
            result["requirements"].append(selected)
    return result, None
