"""Conservative preflight for the CLI's require-constraint SysML profile.

This is a lexical presence screen, not a SysML parser, source mapping, or proof.
It can avoid paying judges to inspect a wholly documentation-only artifact; it
cannot establish that a present constraint covers any particular requirement.
"""
import re


def requirement_content(sysml):
    from canonical_assertions import _code_lines
    code = ''.join(_code_lines(sysml))
    # Ignore quoted prose/identifiers so a string containing syntax isn't code.
    code = re.sub(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' ''', ' ', code, flags=re.X)
    sites = list(re.finditer(r'\brequire\s+(?:constraint\b|\{)', code))
    return {'schema': 'sysml_content_screen/1',
            'status': 'constraint_content_present' if sites else 'no_executable_requirement_content',
            'require_constraint_sites': len(sites),
            'scope': 'Lexical presence of require-constraint syntax in the declared CLI profile; no semantic coverage, source binding or implementation claim.'}
