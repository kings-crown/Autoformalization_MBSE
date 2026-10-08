"""Lossless transport of prepared requirements with document context stored once.

The compact format changes serialization only. Conversion still receives the
existing requirement list with the same complete context on every source row.
Context roles remain documentary input; expansion does not create SMT premises.
"""
from __future__ import annotations

from copy import deepcopy


PREPARED_PACKET_SCHEMA = "prepared_source_packet/1"
CONTEXT_KINDS = frozenset({"definition", "scope", "constraint", "assumption",
                           "dependency", "exception", "informative", "unresolved"})


def _identifier(value, label):
    if (not isinstance(value, str) or not value.strip() or len(value) > 120
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f"{label} must be a nonempty source identifier of at most 120 characters.")
    return value


def expand_prepared_packet(payload):
    """Return independent, expanded requirement records from the explicit schema.

    Legacy JSON arrays and ordinary ``requirements`` objects are handled by the
    canonical importer, not inferred as this format. Refuse ambiguous dual
    context representations instead of choosing one and discarding the other.
    """
    if not isinstance(payload, dict) or payload.get("schema") != PREPARED_PACKET_SCHEMA:
        raise ValueError(f"Prepared source packet schema must be {PREPARED_PACKET_SCHEMA}.")
    if set(payload) != {"schema", "shared_context", "requirements"}:
        raise ValueError("Prepared source packet requires exactly schema, shared_context and requirements.")
    requirements, shared = payload["requirements"], payload["shared_context"]
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("Prepared source packet requirements must be a nonempty list.")
    if not isinstance(shared, list):
        raise ValueError("Prepared source packet shared_context must be a list of context records.")

    identifiers = set()
    for row in requirements:
        if not isinstance(row, dict):
            raise ValueError("Prepared source packet requirements must be objects.")
        rid = _identifier(row.get("id"), "Requirement ID")
        if rid in identifiers:
            raise ValueError(f"Duplicate source identifier: {rid}.")
        identifiers.add(rid)
        source = row.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("context"), dict):
            raise ValueError(f"Requirement {rid} needs source and source.context objects.")
        if "shared" in source["context"]:
            raise ValueError(f"Requirement {rid} has ambiguous inline shared context; use shared_context only.")

    for row in shared:
        if not isinstance(row, dict):
            raise ValueError("Shared context records must be objects.")
        sid = _identifier(row.get("id"), "Shared context ID")
        if sid in identifiers:
            raise ValueError(f"Duplicate or conflicting shared context identifier: {sid}.")
        identifiers.add(sid)
        if not isinstance(row.get("kind"), str) or row["kind"] not in CONTEXT_KINDS:
            raise ValueError(f"Shared context {sid} needs a non-requirement context kind.")
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ValueError(f"Shared context {sid} needs nonempty text.")

    expanded = deepcopy(requirements)
    for row in expanded:
        row["source"]["context"]["shared"] = deepcopy(shared)
    return expanded
