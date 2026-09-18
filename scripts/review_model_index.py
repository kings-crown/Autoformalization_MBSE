"""Conservative navigation index over the exact SysML text shown to reviewers.

This is deliberately not a SysML parser, type checker, or equivalence checker.
Comments and quoted strings are lexically separated before recognizing a small
set of declaration headers. Unsupported blocks are left unindexed.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import hashlib
import re
from typing import Any


@dataclass(frozen=True)
class _Token:
    text: str
    start: int
    end: int
    kind: str = "word"


_LEX = re.compile(
    r"(?P<space>\s+)|(?P<line>//[^\n]*)|(?P<block>/\*[\s\S]*?\*/)"
    r"|(?P<quoted>'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
    r"|(?P<word>[A-Za-z_][A-Za-z_0-9]*)"
    r"|(?P<operator>::|:>>|:>|<=|>=|==|!=|->|=>)|(?P<char>.)",
    re.DOTALL,
)
_MODIFIERS = {"private", "public", "protected", "abstract", "individual", "ref", "in", "out", "inout", "standard", "library"}
_DECLARATIONS = {"package", "part", "port", "attribute", "requirement", "constraint", "subject"}


def _unquote(value: str) -> str:
    if len(value) > 1 and value[0] in "'\"" and value[-1] == value[0]:
        return re.sub(r"\\(['\"\\])", r"\1", value[1:-1])
    return value


def _tokenize(text: str) -> tuple[list[_Token], list[dict], list[_Token]]:
    tokens, comments, diagnostics = [], [], []
    for match in _LEX.finditer(text):
        kind = match.lastgroup
        raw = match.group()
        if kind == "space":
            continue
        if kind in {"line", "block"}:
            comments.append(_Token(raw, match.start(), match.end(), "comment"))
            # A documentation comment is a declaration without a terminating ';'.
            if tokens and tokens[-1].text == "doc" and (len(tokens) == 1 or tokens[-2].text in {";", "{", "}"}):
                tokens.pop()
            elif (len(tokens) >= 2 and tokens[-2].text == "doc" and tokens[-1].kind in {"word", "quoted"}
                  and (len(tokens) == 2 or tokens[-3].text in {";", "{", "}"})):
                tokens[-2:] = []
            continue
        # A block-comment opener unmatched by the lexer must not expose its body.
        if raw == "/" and text[match.start():match.start() + 2] == "/*":
            diagnostics.append({"code": "unterminated_comment", "offset": match.start(), "message": "Unterminated comment; remaining text was not indexed."})
            break
        if raw in {"'", '"'}:
            diagnostics.append({"code": "unterminated_quote", "offset": match.start(), "message": "Unterminated quoted value; remaining text was not indexed."})
            break
        tokens.append(_Token(raw, match.start(), match.end(), kind or "char"))
    return tokens, diagnostics, comments


def _name(tokens: list[_Token], index: int) -> tuple[str | None, int]:
    if index >= len(tokens) or tokens[index].kind not in {"word", "quoted"}:
        return None, index
    parts = [_unquote(tokens[index].text)]
    index += 1
    while index + 1 < len(tokens) and tokens[index].text in {"::", "."} and tokens[index + 1].kind in {"word", "quoted"}:
        parts.extend([tokens[index].text, _unquote(tokens[index + 1].text)])
        index += 2
    return "".join(parts), index


def _header(tokens: list[_Token]) -> dict | None:
    i = 0
    while i < len(tokens) and tokens[i].text in _MODIFIERS:
        i += 1
    if i >= len(tokens):
        return None
    role = "declaration"
    if tokens[i].text in {"assert", "require"}:
        role = tokens[i].text
        i += 1
    if i >= len(tokens):
        return None
    kind = tokens[i].text
    if kind == "satisfy":
        i += 1
        if i < len(tokens) and tokens[i].text == "requirement":
            i += 1
        target, i = _name(tokens, i)
        if target is None or i >= len(tokens) or tokens[i].text != "by":
            return None
        subject, end = _name(tokens, i + 1)
        if subject is None or end != len(tokens):
            return None
        return {"kind": kind, "declared_name": None, "name": f"satisfy {target}", "relationship_target": target, "relationship_subject": subject, "role": "satisfaction_claim"}
    if kind not in _DECLARATIONS:
        return None
    i += 1
    definition = i < len(tokens) and tokens[i].text == "def"
    if definition:
        i += 1
    short_name = None
    if i + 2 < len(tokens) and tokens[i].text == "<" and tokens[i + 2].text == ">":
        short_name = _unquote(tokens[i + 1].text)
        i += 3
    declared, i = _name(tokens, i)
    if declared is None and kind != "constraint":
        return None
    # A name is only optional for anonymous constraint usages.
    if declared is None and i < len(tokens):
        return None
    type_name = None
    if i < len(tokens) and tokens[i].text in {":", ":>"}:
        type_name, _ = _name(tokens, i + 1)
    return {"kind": kind, "declared_name": declared, "name": declared or f"{role} constraint", "short_name": short_name, "definition": definition, "type_name": type_name, "role": role}


def _list_ids(value: Any) -> list[str]:
    return [str(x) for x in value if isinstance(x, (str, int))] if isinstance(value, list) else []


def build_model_inspection(model: dict | str, requirements: list[dict], tlr: dict | None,
                           assumptions: list[dict] | None = None,
                           behavioral_analysis: dict | None = None) -> dict:
    """Index recognized declarations and explicit provenance in the supplied text.

    Line ranges are one-based and inclusive. Offsets are Python Unicode character
    offsets; ``text_sha256`` hashes exact UTF-8 bytes. Crosslinks express provenance
    associations, never a proof that model predicates match another representation.
    """
    model = {"text": model} if isinstance(model, str) else model
    text = str(model.get("text") or "")
    tokens, diagnostics, _comments = _tokenize(text)
    line_starts = [0] + [i + 1 for i, char in enumerate(text) if char == "\n"]
    line_count = len(text.splitlines())

    def location(offset: int) -> tuple[int, int]:
        line = bisect_right(line_starts, max(0, offset))
        return line, offset - line_starts[line - 1] + 1

    matching, stack = {}, []
    for i, token in enumerate(tokens):
        if token.text == "{":
            stack.append(i)
        elif token.text == "}":
            if stack:
                matching[stack.pop()] = i
            else:
                diagnostics.append({"code": "unmatched_brace", "offset": token.start, "message": "Closing brace has no matching opener."})
    for i in stack:
        diagnostics.append({"code": "unmatched_brace", "offset": tokens[i].start, "message": "Opening brace has no matching closer; its declaration was not indexed."})

    elements: list[dict] = []

    def walk(first: int, limit: int, parent: dict | None) -> None:
        cursor = first
        while cursor < limit:
            if tokens[cursor].text in {";", "}"}:
                cursor += 1
                continue
            stop = cursor
            while stop < limit and tokens[stop].text not in {";", "{", "}"}:
                stop += 1
            if stop == limit:
                if cursor < limit:
                    diagnostics.append({"code": "unindexed_statement", "offset": tokens[cursor].start, "message": "Trailing syntax has no recognized statement boundary."})
                break
            block = tokens[stop].text == "{"
            end = matching.get(stop) if block else stop
            if end is None or end >= limit:
                break
            header = _header(tokens[cursor:stop])
            if header is not None:
                start_offset, end_offset = tokens[cursor].start, tokens[end].end
                start_line, start_column = location(start_offset)
                end_line, end_column = location(max(start_offset, end_offset - 1))
                declared = header["declared_name"]
                parent_name = parent.get("qualified_name") if parent else None
                qualified = f"{parent_name}::{declared}" if parent_name and declared else declared
                element_id = "element_" + hashlib.sha256(f"{header['kind']}:{start_offset}:{qualified}".encode()).hexdigest()[:16]
                entry = {**header, "id": element_id, "qualified_name": qualified, "parent_id": parent["id"] if parent else None,
                         "children_ids": [], "start_line": start_line, "end_line": end_line, "start_column": start_column,
                         "end_column": end_column, "start_offset": start_offset, "end_offset": end_offset,
                         "source_requirement_ids": [], "tlr_symbols": [], "assumption_ids": [], "property_ids": [],
                         "formalization": "unestablished", "mapping_basis": "none", "expression": None,
                         "anonymous": declared is None, "index_basis": "recognized_text_header"}
                if block:
                    entry["body_start_line"] = location(tokens[stop].end)[0]
                    entry["body_end_line"] = location(tokens[end].start)[0]
                if header["kind"] == "constraint" and block:
                    inner = tokens[stop + 1:end]
                    # Only a single expression body is represented as an expression.
                    # Definitions containing declarations remain navigable as bodies.
                    if inner and not any(t.text in {";", "{"} for t in inner):
                        a, b = inner[0].start, inner[-1].end
                        entry.update(expression=text[a:b], expression_start_line=location(a)[0], expression_end_line=location(b - 1)[0])
                elements.append(entry)
                if parent:
                    parent["children_ids"].append(element_id)
                if block and header["kind"] != "constraint":
                    walk(stop + 1, end, entry)
            elif block:
                diagnostics.append({"code": "unindexed_block", "offset": tokens[cursor].start,
                                    "message": "Block is outside the supported navigation syntax; its contents were not inferred.",
                                    "header": text[tokens[cursor].start:tokens[stop].start].strip()[:200]})
            # Imports, bindings, documentation and other statements are not elements.
            cursor = end + 1

    walk(0, len(tokens), None)
    by_id = {entry["id"]: entry for entry in elements}
    by_name: dict[str, list[dict]] = {}
    for entry in elements:
        if entry.get("qualified_name"):
            by_name.setdefault(entry["qualified_name"], []).append(entry)
    source_by_id = {str(req["id"]): req for req in requirements if isinstance(req, dict) and req.get("id") is not None}
    tlr_by_id = {str(req["id"]): req for req in (tlr or {}).get("requirements", []) if isinstance(req, dict) and req.get("id") is not None}
    hints = {entry.get("name"): entry for entry in model.get("elements", []) if isinstance(entry, dict)}
    known_symbols = {str(s.get("name")): s for s in (tlr or {}).get("symbols", (tlr or {}).get("symbol_table", [])) if isinstance(s, dict) and s.get("name")}

    for entry in elements:
        hint = hints.get(entry.get("qualified_name"), {})
        req_id = str(hint.get("requirement_id", ""))
        hinted_ids = list(dict.fromkeys([req_id] + _list_ids(hint.get("requirement_ids"))))
        hinted_ids = [rid for rid in hinted_ids if rid in source_by_id]
        if hinted_ids:
            entry["source_requirement_ids"] = hinted_ids
            entry["mapping_basis"] = "generator_metadata"
        elif str(entry.get("short_name")) in source_by_id:
            entry["source_requirement_ids"] = [str(entry["short_name"])]
            entry["mapping_basis"] = "declaration_identifier"
        # Legacy trace rows carry requirementId as a String assignment. This is a
        # metadata association, not an assertion of requirement satisfaction.
        if not entry["source_requirement_ids"] and entry["kind"] == "part":
            nested_parts = [child for child in elements if child["kind"] == "part" and entry["start_offset"] < child["start_offset"] < entry["end_offset"]]
            body = [token for token in tokens if entry["start_offset"] <= token.start < entry["end_offset"]
                    and not any(child["start_offset"] <= token.start < child["end_offset"] for child in nested_parts)]
            for i in range(len(body) - 2):
                if body[i].text == "requirementId" and body[i + 1].text == "=" and body[i + 2].kind == "quoted":
                    candidate = _unquote(body[i + 2].text)
                    if candidate in source_by_id and candidate not in entry["source_requirement_ids"]:
                        entry["source_requirement_ids"].append(candidate)
                        entry["mapping_basis"] = "documentation_reference"
        hint_symbol = str(hint.get("symbol", ""))
        if hint_symbol in known_symbols:
            entry["tlr_symbols"] = [hint_symbol]
            if entry["mapping_basis"] == "none":
                entry["mapping_basis"] = "generator_metadata"
        elif entry["kind"] == "attribute" and entry.get("declared_name") in known_symbols:
            entry["tlr_symbols"] = [entry["declared_name"]]
            if entry["mapping_basis"] == "none":
                entry["mapping_basis"] = "symbol_identifier"
        if entry["source_requirement_ids"] and entry["mapping_basis"] == "documentation_reference":
            entry["formalization"] = "metadata_only"

    def resolve(name: str, entry: dict) -> dict | None:
        ancestor = by_id.get(entry.get("parent_id"))
        while ancestor:
            qualified = ancestor.get("qualified_name")
            matches = by_name.get(f"{qualified}::{name}", []) if qualified else []
            if len(matches) == 1:
                return matches[0]
            ancestor = by_id.get(ancestor.get("parent_id"))
        matches = by_name.get(name, [])
        return matches[0] if len(matches) == 1 else None

    symbol_requirements: dict[str, list[str]] = {}
    for req_id, clause in tlr_by_id.items():
        if req_id not in source_by_id:
            continue
        names = [str(clause["symbol"])] if clause.get("symbol") else [str(s["name"]) for s in clause.get("symbols", []) if isinstance(s, dict) and s.get("name")]
        for symbol in names:
            symbol_requirements.setdefault(symbol, []).append(req_id)

    # Trace quantity declarations and local domain constraints to clauses that
    # refer to their TLR symbols. Matching a name establishes association only.
    for entry in elements:
        if entry["kind"] == "constraint" and entry["expression"]:
            expression_tokens, _, _ = _tokenize(entry["expression"])
            for i, token in enumerate(expression_tokens):
                if token.kind not in {"word", "quoted"} or (i and expression_tokens[i - 1].text in {".", "::"}):
                    continue
                target = resolve(_unquote(token.text), entry)
                if target and target["kind"] == "attribute":
                    entry["tlr_symbols"] = list(dict.fromkeys(entry["tlr_symbols"] + target["tlr_symbols"]))
            if entry["tlr_symbols"] and entry["mapping_basis"] == "none":
                entry["mapping_basis"] = "expression_attribute_reference"
        if entry["kind"] == "attribute" or (entry["kind"] == "constraint" and entry["tlr_symbols"]):
            for symbol in entry["tlr_symbols"]:
                entry["source_requirement_ids"] = list(dict.fromkeys(entry["source_requirement_ids"] + symbol_requirements.get(symbol, [])))

    for entry in elements:
        parent = by_id.get(entry.get("parent_id"))
        if parent and parent["kind"] == "requirement" and not entry["source_requirement_ids"]:
            entry["source_requirement_ids"] = list(parent["source_requirement_ids"])
            entry["mapping_basis"] = "inherited_requirement" if entry["source_requirement_ids"] else "none"
        if entry["kind"] == "satisfy":
            target = resolve(entry["relationship_target"], entry)
            subject = resolve(entry["relationship_subject"], entry)
            entry["target_id"] = target["id"] if target else None
            entry["subject_id"] = subject["id"] if subject else None
            if target:
                entry["source_requirement_ids"] = list(target["source_requirement_ids"])
                entry["mapping_basis"] = "relationship_target"
            entry["formalization"] = "metadata_only"  # A satisfaction usage is a claim, not evidence.
        for req_id in entry["source_requirement_ids"]:
            clause = tlr_by_id.get(req_id, {})
            symbols = [str(clause["symbol"])] if clause.get("symbol") else [str(s["name"]) for s in clause.get("symbols", []) if isinstance(s, dict) and s.get("name")]
            entry["tlr_symbols"] = list(dict.fromkeys(entry["tlr_symbols"] + symbols))
        if entry["kind"] in {"requirement", "constraint"}:
            has_expression = bool(entry["expression"]) or any(by_id[c]["kind"] == "constraint" and by_id[c]["expression"] for c in entry["children_ids"])
            if has_expression:
                entry["formalization"] = "encoded"
            elif entry["kind"] == "requirement":
                entry["formalization"] = "metadata_only"
        entry["source"] = [source_by_id[r].get("source", {}) for r in entry["source_requirement_ids"]]

    assumptions = [a for a in (assumptions or []) if isinstance(a, dict) and a.get("id")]
    checks = [p for p in (behavioral_analysis or {}).get("checks", []) if isinstance(p, dict) and p.get("id")]
    for entry in elements:
        req_ids = set(entry["source_requirement_ids"])
        entry["assumption_ids"] = [str(a["id"]) for a in assumptions if req_ids.intersection(_list_ids(a.get("requirement_ids")))]
        entry["property_ids"] = [str(p["id"]) for p in checks if req_ids.intersection(_list_ids(p.get("requirement_ids", p.get("source_requirement_ids"))))]

    for item in diagnostics:
        if "offset" in item:
            item["line"], item["column"] = location(item.pop("offset"))
    represented = {r for element in elements for r in element["source_requirement_ids"]}
    return {
        "schema": "review_model_inspection/1", "parser": "conservative_text_index",
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "line_count": line_count,
        "elements": elements, "root_ids": [e["id"] for e in elements if e["parent_id"] is None],
        "unmapped_requirement_ids": [r for r in source_by_id if r not in represented],
        "unscoped_assumption_ids": [str(a["id"]) for a in assumptions if not _list_ids(a.get("requirement_ids"))],
        "diagnostics": diagnostics,
        "scope": "Navigation over recognized declarations in the displayed SysML text. Provenance links and encoded predicates are not proof of source fidelity, solver correspondence, or executable temporal behavior. Compilation is reported separately.",
        "formalization_meaning": {"encoded": "A constraint expression is present in this SysML declaration; equivalence to other artifacts is not established by this index.", "metadata_only": "Documentation or a relationship claim, without a checked predicate in this declaration.", "unestablished": "The index does not establish a requirement formalization for this structural element."},
    }
