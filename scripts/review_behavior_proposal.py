"""Ask the configured generator for a reviewable, strictly validated behavior candidate."""
from __future__ import annotations

import json
from pathlib import Path

import requirements_pipeline as legacy
from review_behavior import BEHAVIOR_GUIDE, validate_behavior


async def propose_behavior(requirements: list[dict], directory: Path, model: str) -> dict:
    system = '''Propose a small candidate transition model for engineering review.
Return strict JSON with exactly two keys: behavior (a model object or null), and
reason (a concise explanation). If no meaningful supported temporal/robustness
property can be modeled from these requirements, return behavior:null and explain.
The candidate is a proposal, never an approved interpretation. Use exact source
requirement IDs. Keep initial conditions, dynamics, environmental assumptions,
and target properties separate. Do not assume a target guarantee to prove it.
Declare every added engineering premise in the model's assumptions with a text
description and the actual predicate, and disclose modeling choices in reason.
Include unbounded eventual_response requirements as unbounded properties; never
replace them with a finite deadline that the source did not state. Respect the
bounded schema below. If a faithful candidate needs unsupported constructs,
return null rather than omitting requirements or inventing unsupported syntax.
'''
    record = {'schema': 'review_behavior_proposal/1', 'origin': 'llm', 'model': model,
              'status': 'failed', 'summary': '', 'candidate': None,
              'source_fidelity': 'unestablished; requires engineer review'}
    try:
        result = await legacy._codex_chat_json(system + '\n\n' + BEHAVIOR_GUIDE,
                                               json.dumps(requirements, ensure_ascii=False, indent=2), model)
        record['raw'] = result
        if not isinstance(result, dict) or set(result) != {'behavior', 'reason'} or not isinstance(result['reason'], str):
            raise ValueError('Proposal must contain behavior and a text reason.')
        if result['behavior'] is None:
            record.update(status='not_proposed', summary=result['reason'])
        else:
            candidate = validate_behavior(result['behavior'], [r['id'] for r in requirements])
            record.update(status='proposed', summary=result['reason'], candidate=candidate)
    except (ValueError, TypeError, KeyError) as exc:
        record.update(status='invalid', summary=f'Candidate did not satisfy the supported behavior schema: {exc}')
    except Exception as exc:
        record.update(status='failed', summary=f'Behavior proposal was unavailable: {type(exc).__name__}: {exc}')
    (directory / 'llm_behavior_proposal.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return record
