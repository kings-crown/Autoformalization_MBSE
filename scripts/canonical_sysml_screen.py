"""Conservative preflight for the CLI's require-constraint SysML profile.

This is a lexical presence screen, not a SysML parser, source mapping, or proof.
It identifies wholly documentation-only artifacts but cannot establish that a
present constraint covers any particular requirement.
"""
import re


def _code_lines(sysml):
    """Remove comments and annotation headers without interpreting SysML semantics.

    Remove // and /* */ comments while respecting quoted strings and identifiers.
    Also remove doc/comment annotation headers preceding their comment bodies, so
    ``doc /* ... */`` does not count as executable requirement content.
    Lexical state spans the whole artifact, including multiline comments.
    """
    lines = sysml.splitlines(keepends=True)
    result = [""] * len(lines)
    block = False
    quote = None
    escaped = False
    annotation = False
    for number, line in enumerate(lines):
        kept, index = [], 0
        while index < len(line):
            char = line[index]
            pair = line[index:index + 2]
            if block:
                if pair == "*/":
                    block = False
                    annotation = False
                    index += 2
                else:
                    index += 1
                continue
            if quote:
                if not annotation:
                    kept.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                index += 1
                continue
            if pair == "//":
                kept.append("\n")  # Removing a comment must not merge adjacent tokens.
                break
            if pair == "/*":
                kept.append(" ")
                block = True
                index += 2
                continue
            if char in {'"', "'"}:
                quote = char
                if not annotation:
                    kept.append(char)
                index += 1
                continue
            if char.isalpha() or char == "_":
                end = index + 1
                while end < len(line) and (line[end].isalnum() or line[end] == "_"):
                    end += 1
                token = line[index:end]
                if token in {"doc", "comment"}:
                    annotation = True
                elif not annotation:
                    kept.append(token)
                index = end
                continue
            if not annotation:
                kept.append(char)
            index += 1
        result[number] = "".join(kept)
    return result



def requirement_content(sysml):
    code = ''.join(_code_lines(sysml))
    # Ignore quoted prose/identifiers so a string containing syntax isn't code.
    code = re.sub(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' ''', ' ', code, flags=re.X)
    sites = list(re.finditer(r'\brequire\s+(?:constraint\b|\{)', code))
    return {'schema': 'sysml_content_screen/1',
            'status': 'constraint_content_present' if sites else 'no_executable_requirement_content',
            'require_constraint_sites': len(sites),
            'scope': 'Lexical presence of require-constraint syntax in the declared CLI profile; no semantic coverage, source binding or implementation claim.'}
