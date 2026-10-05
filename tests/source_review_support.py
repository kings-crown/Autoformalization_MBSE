"""Explicit offline source-review double; never imported by production code.

This accepts supported test fixtures without assessing semantics. Tests of actual
semantic withholding override dispositions/findings in their own fixtures.
"""
import json

from canonical_source_review import BACKGROUND_DIMENSIONS, DIMENSIONS, RESPONSE_SCHEMA


def review_response(payload):
    """Build a structurally valid response for the supplied reviewer prompt."""
    sources = {row["id"]: row for row in payload["source_packet"]}
    tlr = payload["candidate_tlr"]
    first = next(iter(sources.values()))

    def record(disposition, dimensions, paths, source):
        return {"disposition": disposition,
                "reason": "Explicit offline test double; no semantic assurance is inferred.",
                "dimensions": [{"dimension": name, "status": "pass",
                                "explanation": "This dimension is explicitly accepted by the test fixture."}
                               for name in dimensions],
                "source_basis": [{"source_id": source["id"], "quote": source["text"]}],
                "ast_paths": paths}

    result = {"schema": RESPONSE_SCHEMA,
              "background": record("pass", BACKGROUND_DIMENSIONS, ["/variables"], first),
              "requirements": []}
    for i, row in enumerate(tlr["requirements"]):
        disposition = {"supported": "pass", "unsupported": "unsupported",
                       "unresolved": "needs_clarification"}[row["status"]]
        path = f"/requirements/{i}" + ("/formula" if row["status"] == "supported" else "")
        result["requirements"].append({"id": row["id"], **record(disposition, DIMENSIONS, [path], sources[row["id"]])})
        if payload.get("obligation_inventory"):
            from canonical_source_review import COMPONENT_DIMENSIONS
            inventory_row = next(r for r in payload["obligation_inventory"]["requirements"] if r["id"] == row["id"])
            coverage = {c["obligation_id"]: c for c in row.get("coverage", [])}
            components = []
            for obligation in inventory_row["obligations"]:
                mapping = coverage.get(obligation["id"], {})
                represented = mapping.get("status") == "represented"
                component_path = f"/requirements/{i}" + (mapping["formula_path"] if represented else "")
                component = {"id": obligation["id"], **record("pass" if represented else "revise",
                    COMPONENT_DIMENSIONS, [component_path], sources[row["id"]])}
                component["source_basis"] = obligation["source_basis"]
                components.append(component)
            result["requirements"][-1]["obligations"] = components
    return result


def pass_source_review(system, prompt, model, directory, call_id):
    """Canonical reviewer callback for explicitly mocked offline workflows."""
    payload = json.loads(prompt)
    if "obligation_inventory" in payload and "candidate_tlr" not in payload:
        from canonical_obligations import REVIEW_SCHEMA, REVIEW_DIMENSIONS
        sources = {r["id"]: r for r in payload["source_packet"]}
        return json.dumps({"schema": REVIEW_SCHEMA, "requirements": [{
            "id": row["id"], "disposition": "pass", "reason": "Explicit offline fixture review, not semantic assurance.",
            "source_basis": [{"source_id": row["id"], "quote": sources[row["id"]]["text"]}],
            "dimensions": [{"dimension": d, "status": "pass", "explanation": "Accepted by the explicit offline fixture."}
                           for d in REVIEW_DIMENSIONS],
            "obligations": [{"id": o["id"], "disposition": "pass", "reason": "Accepted by the explicit offline fixture."}
                            for o in row["obligations"]]}
            for row in payload["obligation_inventory"]["requirements"]]})
    return json.dumps(review_response(payload))
