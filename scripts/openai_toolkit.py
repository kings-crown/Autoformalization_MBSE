"""Compatibility module for legacy `openai_toolkit` usage."""

from __future__ import annotations

import sys

import requirements_pipeline as _core  # type: ignore


if __name__ == "__main__":
    from requirements_pipeline import main as unified_main

    unified_main(list(sys.argv[1:]))
else:
    # Preserve import-time behavior for existing tests and callers by exposing
    # the core toolkit module under the legacy name.
    sys.modules[__name__] = _core
