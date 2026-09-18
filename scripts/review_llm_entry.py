"""Run the existing pipeline with auditable assumption instructions and capture.

This wrapper leaves the legacy implementation unchanged. It does not request an
extra model call or treat a generated assumption inventory as complete.
"""
from __future__ import annotations

from itertools import count
import asyncio
import json
import os
from pathlib import Path

import requirements_pipeline as legacy

ASSUMPTION_POLICY = '''Engineering assumption disclosure for this review run:
Preserve the requested output syntax and schema. Distinguish stakeholder text
from premises you introduce. Explicitly disclose choices about operating modes,
event identity/correlation, initial state, variable domains, units, time step,
horizon, scheduling/fairness, progress, disturbance bounds, and fault exclusions.
Do not invent stakeholder authority or report these choices as approved.
In JSON outputs, use existing assumptions/global_assumptions fields where the
schema permits them; attach source requirement IDs when the schema permits.
For SMT-LIB output, put each introduced premise in a separate legal comment:
; REVIEW_ASSUMPTION {"statement":"...","requirement_ids":["..."],"reason":"why this premise was needed"}
For SysML output, use the same JSON marker in a // REVIEW_ASSUMPTION comment.
Do not add new behavioral guarantees just to obtain SAT, UNSAT, or compilation.
Separate model assumptions from properties being checked. A feasible bounded
trace is not an unbounded liveness proof, and assert false is not safety evidence.
If meaning is unresolved, disclose the unresolved choice rather than concealing it.
'''


def install_capture(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'llm_assumption_policy.txt').write_text(ASSUMPTION_POLICY, encoding='utf-8')
    original = legacy._codex_chat_text
    calls = count(1)

    async def audited(system_prompt: str, user_prompt: str, model: str) -> str:
        number = next(calls)
        path = directory / f'llm-call-{number:03d}.json'
        record = {'schema': 'review_llm_call/1', 'model': model,
                  'system_prompt': system_prompt + '\n\n' + ASSUMPTION_POLICY,
                  'user_prompt': user_prompt, 'status': 'running'}
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        try:
            response = await original(record['system_prompt'], user_prompt, model)
            record.update(status='completed', response=response)
            return response
        except Exception as exc:
            record.update(status='failed', error=f'{type(exc).__name__}: {exc}')
            raise
        finally:
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')

    legacy._codex_chat_text = audited


def install_tlr_capture(directory: Path) -> None:
    """Retain native interpretation before any provider call can fail."""
    original = legacy._build_tlf_payload

    def captured(*args, **kwargs):
        try:
            payload = original(*args, **kwargs)
        except Exception as exc:
            (directory / 'tlr_preflight_error.json').write_text(json.dumps({
                'stage': 'native_tlr_preflight', 'status': 'failed',
                'error': f'{type(exc).__name__}: {exc}'
            }, indent=2) + '\n', encoding='utf-8')
            raise
        (directory / 'initial_tlr.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        return payload

    legacy._build_tlf_payload = captured


if __name__ == '__main__':
    location = os.environ.get('MBSE_REVIEW_CAPTURE_DIR')
    if not location:
        raise SystemExit('MBSE_REVIEW_CAPTURE_DIR must identify this run directory.')
    install_capture(Path(location).resolve())
    install_tlr_capture(Path(location).resolve())
    legacy.legacy_main()
    if (os.environ.get('MBSE_REVIEW_PROPOSE_BEHAVIOR') == '1'
            and os.environ.get('MBSE_REVIEW_ANALYSIS_MODE') == 'propose_design'):
        from review_behavior_proposal import propose_behavior
        requirements = json.loads((Path(location) / 'pipeline_source_requirements.json').read_text())
        asyncio.run(propose_behavior(requirements, Path(location), os.environ['CODEX_MBSE_MODEL']))
