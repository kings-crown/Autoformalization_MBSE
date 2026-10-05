"""Keep conversion evidence separate from final source-to-SysML assessment."""
from __future__ import annotations


def conversion_assurance(result: dict, sources: list[dict]) -> dict:
    """Describe the selected conversion without manufacturing semantic approval.

    Source-review, compiler and solver success cannot populate final-fidelity
    outcomes. Those belong to a separately frozen source/SysML assessment.
    """
    tlr = result.get("tlr")
    review = result.get("source_review") or {}
    audit = result.get("analysis") or {}
    source_ids = [row["id"] for row in sources]
    model_available = bool(result.get("model_file"))
    structured = tlr is not None
    historical_gate = result.get("configuration", {}).get("source_review_required", bool(review))
    emitted = ((list(review.get("eligible_ids", [])) if historical_gate else
                [row["id"] for row in tlr["requirements"] if row["status"] == "supported"])
               if structured and model_available else [])
    feedback = result.get("feedback_repair") or {}
    embedded_review = any("changes" in step for step in feedback.get("steps", []))
    coverage = {
        "status": "recorded" if structured else "not_assessed",
        "source_requirement_ids": source_ids,
        "planned": len(source_ids),
        "emitted_requirement_ids": emitted if structured else None,
        "not_emitted_requirement_ids": [rid for rid in source_ids if rid not in emitted] if structured else None,
        "emitted": len(emitted) if structured else None,
        "scope": "Emission of draft abstractions, not established semantic coverage of the source. Direct SysML is not independently parsed for coverage.",
    }
    return {
        "schema": "conversion_assurance/1",
        "objective": "Preserve the contextualized source requirements in the final SysML.",
        "interpretation_authority": "The engineer-prepared source and context; generated TLR is a proposed interpretation, not a reference answer.",
        "model_available": model_available,
        "formal_coverage": coverage,
        "source_to_rule": {
            "status": (review.get("status", "not_run") if historical_gate else
                       "embedded_review_recorded" if embedded_review else
                       "unavailable" if feedback.get("repair_attempts", 0) else "not_run") if structured else "not_applicable",
            "complete": bool(review.get("complete")) if structured and historical_gate else None,
            "method": (("Historical separate source-to-rule review." if historical_gate else
                        "Source review recorded inside bounded feedback proposals; no separate acceptance gate.")
                       if structured else "Direct SysML route has no TLR review."),
            "scope": "Recorded proposal reviews may concern rejected attempts; they do not approve the selected model or supply independent final SysML assessment.",
        },
        "encoded_logic": {
            "status": audit.get("status", "not_run"),
            "background_status": audit.get("background_status", "not_run"),
            "consistency_status": audit.get("consistency_status", "not_run"),
            "requirement_ids": list(audit.get("supported_requirement_ids", [])),
            "scope": "The exact emitted TLR subset under declared premises; Z3 does not read the emitted SysML or original prose.",
        },
        "sysml_compilation": {
            "status": (result.get("compilation") or {}).get("status", "not_run"),
            "scope": "Compiler acceptance, not source fidelity or implemented behavior.",
        },
        "independent_tlr_to_sysml_preservation": {
            "status": "not_run" if structured else "not_applicable",
            "scope": "Shared AST rendering is not independent semantic equivalence checking; even equivalence would not validate an incorrect TLR interpretation.",
        },
        "source_to_sysml": {
            "status": "not_assessed",
            "required_evidence": "Separate assessment of actual final SysML against frozen contextualized source, including fidelity, completeness and invented restrictions.",
            "scope": "No conversion result, source-review pass, solver result or compiler success supplies this assessment. Honest unsupported disclosures cannot replace missing obligations.",
        },
        "machine_admission": {
            "status": result.get("admission", "not_assessed"),
            "scope": "Declared encoding-admission policy only; not source alignment or engineer sign-off.",
        },
    }
