#!/usr/bin/env python3
"""Compatibility module for legacy `mbse_toolkit_core` usage.

`requirements_pipeline.py` is the authoritative implementation.
This shim preserves import-time symbols and CLI execution.
"""

from __future__ import annotations

import sys


if __name__ == "__main__":
    from requirements_pipeline import main as unified_main

    unified_main(list(sys.argv[1:]))
else:
    import requirements_pipeline as _pipeline  # type: ignore

    sys.modules[__name__] = _pipeline
