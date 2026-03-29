"""
Canonical TLR contract helpers.

This repository uses two distinct "TLR-like" payloads:

1. requirements_tlr (requirements-centric intermediate form used by Python pipeline)
2. logical_form_tlr (signature/axioms solver-facing logical representation)

The helpers below make this distinction explicit and provide lossy converters so
pipeline boundaries can reason about one canonical contract type at a time.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

REQUIREMENTS_TLR_CONTRACT_NAME = "requirements_tlr"
REQUIREMENTS_TLR_CONTRACT_VERSION = "1.0"
LOGICAL_FORM_TLR_CONTRACT_NAME = "logical_form_tlr"
LOGICAL_FORM_TLR_CONTRACT_VERSION = "1.0"
# Legacy contract names still accepted during reads.
REQUIREMENTS_TLF_CONTRACT_NAME = "requirements_tlf"
LOGICAL_FORM_TLF_CONTRACT_NAME = "logical_form_tlf"


def _contract(name: str, version: str) -> Dict[str, str]:
    return {"name": name, "version": version}


def _looks_like_requirements_tlf(payload: Dict[str, Any]) -> bool:
    return (
        isinstance(payload.get("requirements"), list)
        and isinstance(payload.get("symbol_table"), list)
        and isinstance(payload.get("traceability"), list)
    )


def _looks_like_logical_form_tlf(payload: Dict[str, Any]) -> bool:
    return (
        isinstance(payload.get("metadata"), dict)
        and isinstance(payload.get("signature"), dict)
        and isinstance(payload.get("axioms"), list)
    )


def _is_requirements_contract_name(name: str) -> bool:
    return name in {REQUIREMENTS_TLR_CONTRACT_NAME, REQUIREMENTS_TLF_CONTRACT_NAME}


def _is_logical_contract_name(name: str) -> bool:
    return name in {LOGICAL_FORM_TLR_CONTRACT_NAME, LOGICAL_FORM_TLF_CONTRACT_NAME}


def detect_tlf_contract(payload: Dict[str, Any]) -> str:
    for key in ("tlr_contract", "tlf_contract"):
        contract = payload.get(key)
        if not isinstance(contract, dict):
            continue
        name = str(contract.get("name", "")).strip()
        if _is_requirements_contract_name(name):
            return REQUIREMENTS_TLR_CONTRACT_NAME
        if _is_logical_contract_name(name):
            return LOGICAL_FORM_TLR_CONTRACT_NAME
    if _looks_like_requirements_tlf(payload):
        return REQUIREMENTS_TLR_CONTRACT_NAME
    if _looks_like_logical_form_tlf(payload):
        return LOGICAL_FORM_TLR_CONTRACT_NAME
    return "unknown"


def ensure_requirements_tlf_contract(payload: Dict[str, Any]) -> Dict[str, Any]:
    tagged = dict(payload)
    tagged["tlr_contract"] = _contract(
        REQUIREMENTS_TLR_CONTRACT_NAME,
        REQUIREMENTS_TLR_CONTRACT_VERSION,
    )
    return tagged


def ensure_logical_form_tlf_contract(payload: Dict[str, Any]) -> Dict[str, Any]:
    tagged = dict(payload)
    tagged["tlr_contract"] = _contract(
        LOGICAL_FORM_TLR_CONTRACT_NAME,
        LOGICAL_FORM_TLR_CONTRACT_VERSION,
    )
    return tagged


def _sort_from_symbol_type(symbol_type: str) -> str:
    normalized = (symbol_type or "").strip().lower()
    if normalized in {"int", "integer"}:
        return "Int"
    if normalized == "real":
        return "Real"
    return "Bool"


def _symbol_type_from_sort(sort_name: str) -> str:
    normalized = (sort_name or "").strip().lower()
    if normalized == "int":
        return "Int"
    if normalized == "real":
        return "Real"
    return "Bool"


def convert_requirements_tlf_to_logical_form_tlf(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Lossy conversion from requirements_tlr -> logical_form_tlr."""
    requirements = [
        item
        for item in payload.get("requirements", [])
        if isinstance(item, dict)
    ]
    symbol_table = [
        item
        for item in payload.get("symbol_table", [])
        if isinstance(item, dict)
    ]

    constants: List[Dict[str, Any]] = []
    known_constant_names = set()
    for symbol in symbol_table:
        name = str(symbol.get("name", "")).strip()
        if not name or name in known_constant_names:
            continue
        known_constant_names.add(name)
        constants.append(
            {
                "name": name,
                "sort": _sort_from_symbol_type(str(symbol.get("type", ""))),
                "description": str(symbol.get("role", "derived")),
            }
        )

    axiom_entries: List[Dict[str, Any]] = []
    for idx, req in enumerate(requirements, start=1):
        req_id = str(req.get("id", f"R{idx}")).strip() or f"R{idx}"
        req_text = str(req.get("text", "")).strip() or f"Requirement {req_id}"
        req_symbols = req.get("symbols", [])
        primary_symbol: Optional[str] = None
        if isinstance(req_symbols, list):
            for item in req_symbols:
                if isinstance(item, dict):
                    candidate = str(item.get("name", "")).strip()
                    if candidate:
                        primary_symbol = candidate
                        break
        if primary_symbol and primary_symbol in known_constant_names:
            formula: Dict[str, Any] = {"kind": "identifier", "name": primary_symbol}
        else:
            formula = {"kind": "bool", "value": True}

        axiom_entries.append(
            {
                "id": f"axiom_{req_id}",
                "requirementIds": [req_id],
                "description": req_text,
                "formula": formula,
            }
        )

    source = payload.get("source", {})
    statement_path = ""
    statement_sha = ""
    if isinstance(source, dict):
        statement_path = str(source.get("statement_path", "")).strip()
        statement_sha = str(source.get("statement_sha256", "")).strip()

    logical_payload: Dict[str, Any] = {
        "metadata": {
            "requirementSetId": statement_sha[:12] or "requirements_tlr",
            "title": statement_path or "requirements_tlr",
            "system": "converted-from-requirements-tlr",
            "requirements": [
                {
                    "id": str(req.get("id", "")).strip(),
                    "text": str(req.get("text", "")).strip(),
                }
                for req in requirements
                if str(req.get("id", "")).strip() and str(req.get("text", "")).strip()
            ],
            "assumptions": [],
        },
        "signature": {
            "sorts": [],
            "constants": constants,
            "functions": [],
            "predicates": [],
        },
        "axioms": axiom_entries or [
            {
                "id": "axiom_r1",
                "requirementIds": ["R1"],
                "description": "Fallback axiom from converter.",
                "formula": {"kind": "bool", "value": True},
            }
        ],
    }
    return ensure_logical_form_tlf_contract(logical_payload)


def convert_logical_form_tlf_to_requirements_tlf(
    payload: Dict[str, Any],
    statement_path: str = "<logical_form>",
) -> Dict[str, Any]:
    """Lossy conversion from logical_form_tlr -> requirements_tlr."""
    metadata = payload.get("metadata", {})
    signature = payload.get("signature", {})
    requirements_meta = metadata.get("requirements", []) if isinstance(metadata, dict) else []
    constants = signature.get("constants", []) if isinstance(signature, dict) else []

    symbol_table: List[Dict[str, Any]] = []
    for item in constants if isinstance(constants, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        symbol_table.append(
            {
                "name": name,
                "type": _symbol_type_from_sort(str(item.get("sort", "Bool"))),
                "unit": None,
                "role": "derived",
            }
        )

    requirement_entries: List[Dict[str, Any]] = []
    traceability: List[Dict[str, Any]] = []
    for idx, item in enumerate(requirements_meta if isinstance(requirements_meta, list) else [], start=1):
        if not isinstance(item, dict):
            continue
        req_id = str(item.get("id", f"R{idx}")).strip() or f"R{idx}"
        req_text = str(item.get("text", "")).strip()
        if not req_text:
            continue
        requirement_entries.append(
            {
                "id": req_id,
                "text": req_text,
                "category": "functional",
                "temporal_kind": "event_triggered",
                "symbols": [],
                "ranges": [],
                "source_span": {"line_start": idx, "line_end": idx},
            }
        )
        traceability.append(
            {
                "requirement_id": req_id,
                "tlf_requirement_index": idx - 1,
                "source_path": statement_path,
                "source_span": {"line_start": idx, "line_end": idx},
            }
        )

    if not requirement_entries:
        requirement_entries.append(
            {
                "id": "R1",
                "text": "Converted fallback requirement from logical_form_tlr.",
                "category": "functional",
                "temporal_kind": "event_triggered",
                "symbols": [],
                "ranges": [],
                "source_span": {"line_start": 1, "line_end": 1},
            }
        )
        traceability.append(
            {
                "requirement_id": "R1",
                "tlf_requirement_index": 0,
                "source_path": statement_path,
                "source_span": {"line_start": 1, "line_end": 1},
            }
        )

    statement_blob = "\n".join(
        str(item.get("text", ""))
        for item in requirement_entries
        if isinstance(item, dict)
    )
    requirements_payload: Dict[str, Any] = {
        "schema_version": "1.0",
        "source": {
            "statement_path": statement_path,
            "statement_sha256": hashlib.sha256(statement_blob.encode("utf-8")).hexdigest(),
        },
        "requirements": requirement_entries,
        "symbol_table": symbol_table,
        "traceability": traceability,
    }
    return ensure_requirements_tlf_contract(requirements_payload)


def as_requirements_tlf(payload: Dict[str, Any], statement_path: str = "<unknown>") -> Dict[str, Any]:
    contract = detect_tlf_contract(payload)
    if contract == REQUIREMENTS_TLR_CONTRACT_NAME:
        return ensure_requirements_tlf_contract(payload)
    if contract == LOGICAL_FORM_TLR_CONTRACT_NAME:
        return convert_logical_form_tlf_to_requirements_tlf(payload, statement_path=statement_path)
    raise RuntimeError(
        "Unable to interpret payload as known TLR contract. "
        "Expected requirements_tlr or logical_form_tlr shape."
    )


# Canonical TLR-named API (legacy function names retained as compatibility aliases).
def detect_tlr_contract(payload: Dict[str, Any]) -> str:
    return detect_tlf_contract(payload)


def ensure_requirements_tlr_contract(payload: Dict[str, Any]) -> Dict[str, Any]:
    return ensure_requirements_tlf_contract(payload)


def ensure_logical_form_tlr_contract(payload: Dict[str, Any]) -> Dict[str, Any]:
    return ensure_logical_form_tlf_contract(payload)


def convert_requirements_tlr_to_logical_form_tlr(payload: Dict[str, Any]) -> Dict[str, Any]:
    return convert_requirements_tlf_to_logical_form_tlf(payload)


def convert_logical_form_tlr_to_requirements_tlr(
    payload: Dict[str, Any], statement_path: str = "<logical_form>"
) -> Dict[str, Any]:
    return convert_logical_form_tlf_to_requirements_tlf(payload, statement_path=statement_path)


def as_requirements_tlr(payload: Dict[str, Any], statement_path: str = "<unknown>") -> Dict[str, Any]:
    return as_requirements_tlf(payload, statement_path=statement_path)
