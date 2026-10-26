"""Assemble and size-check Codex input text without importing model transports.

Limits count Python string characters, not encoded bytes or model tokens. Passing
this check establishes only that the composed text fits the transport character
ceiling; it does not establish that the model has enough context or output space.
"""
from __future__ import annotations

import os


HARD_MAX_INPUT_CHARS = 1_048_576
_LIMIT_ENV = "CODEX_MAX_INPUT_CHARS"


class RequestSizeError(ValueError):
    """A composed request exceeds its character limit; details contain no source."""

    def __init__(self, details: dict):
        self.details = dict(details)
        super().__init__(
            f"Codex request contains {self.details['chars']} characters; "
            f"the limit is {self.details['max_chars']} characters. "
            "The request was not submitted. This is a character limit, not a token budget."
        )


def _text(value, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string.")
    return value


def _limit(max_chars: int | None) -> int:
    raw = os.getenv(_LIMIT_ENV)
    configured = HARD_MAX_INPUT_CHARS
    if raw is not None:
        stripped = raw.strip()
        if not stripped or not stripped.isascii() or not stripped.isdecimal():
            raise ValueError(
                f"{_LIMIT_ENV} must be an integer between 1 and {HARD_MAX_INPUT_CHARS}."
            )
        configured = int(stripped)
        if not 1 <= configured <= HARD_MAX_INPUT_CHARS:
            raise ValueError(
                f"{_LIMIT_ENV} must be an integer between 1 and {HARD_MAX_INPUT_CHARS}."
            )
    if max_chars is None:
        return configured
    if type(max_chars) is not int or not 1 <= max_chars <= configured:
        raise ValueError(
            f"max_chars must be an integer between 1 and the configured limit ({configured})."
        )
    return max_chars


def compose_request(system: str, prompt: str) -> str:
    """Return the exact existing Codex system/user framing, including its newline."""
    return ("System instructions:\n" + _text(system, "system").strip()
            + "\n\nUser request:\n" + _text(prompt, "prompt").strip() + "\n")


def _preflight_text(prompt: str, max_chars: int | None = None) -> dict:
    chars = len(_text(prompt, "prompt"))
    limit = _limit(max_chars)
    return {
        "schema": "codex_request_preflight/1",
        "status": "passed" if chars <= limit else "oversize",
        "chars": chars,
        "max_chars": limit,
        "hard_max_chars": HARD_MAX_INPUT_CHARS,
        "unit": "characters",
        "scope": "Fully composed input text; not a token or model-context budget.",
    }


def request_preflight(system: str, prompt: str, max_chars: int | None = None) -> dict:
    """Record the complete framed size; oversize is returned for caller diagnosis."""
    return _preflight_text(compose_request(system, prompt), max_chars)


def validate_request_text(prompt: str, max_chars: int | None = None) -> dict:
    """Validate already composed input before allocation or process dispatch."""
    details = _preflight_text(prompt, max_chars)
    if details["status"] != "passed":
        raise RequestSizeError(details)
    return details
