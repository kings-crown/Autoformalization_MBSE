"""HTTP transport for the canonical requirements CLI, with unchanged artifacts.

Source preparation, conversion and independent assessment remain separate.
Reviews record user opinions; they do not rewrite solver evidence or admission.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Literal
from urllib.parse import quote
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import canonical_cli as canonical
from canonical_abstractions import POLICY, PROFILE

RUN_ID = re.compile(r'run-[0-9a-f]{16}\Z')
STAGES = [('extraction', 'Read requirements and context'), ('interpretation', 'Build typed meaning'),
          ('generation', 'Generate SysML v2'), ('analysis', 'Check encoded requirements'),
          ('refinement', 'Review proposed corrections'), ('scenarios', 'Check development scenarios'),
          ('compilation', 'Compile model')]
COPY_FIELDS = ('tlr', 'representation', 'analysis', 'compilation', 'candidate_content', 'admission',
               'configuration', 'feedback_repair', 'repair', 'errors', 'limitations', 'source_fidelity',
               'source_review', 'assurance', 'obligation_preparation', 'obligation_inventory')


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def _owner():
    try:
        fields = Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': os.getpid(), 'start_ticks': fields[19]}
    except (OSError, IndexError):
        return {'pid': os.getpid(), 'start_ticks': None}


def _owner_active(owner):
    if not isinstance(owner, dict) or type(owner.get('pid')) is not int or not owner.get('start_ticks'):
        return False
    try:
        fields = Path(f"/proc/{owner['pid']}/stat").read_text().rsplit(')', 1)[1].split()
        return fields[0] not in {'Z', 'X'} and fields[19] == owner['start_ticks']
    except (OSError, IndexError):
        return False


class CanonicalRunInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='Requirements model', min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=512000)
    format: Literal['text', 'csv', 'json'] = 'text'
    condition: Literal['A', 'B', 'C', 'BC'] = 'C'
    model: str | None = Field(default=None, max_length=200)
    source_review_model: str | None = Field(default=None, max_length=200)
    feedback_repairs: int = Field(default=0, ge=0, le=5, strict=True)
    abstention_repairs: int = Field(default=0, ge=0, le=5, strict=True)
    format_repairs: int = Field(default=0, ge=0, le=1, strict=True)
    context: dict | None = None
    obligation_inventory: dict | None = None
    development_scenarios: dict | None = None
    tlr: dict | None = None
    sysml_text: str | None = Field(default=None, max_length=2000000)
    parent_run_id: str | None = None
    revision_rationale: str | None = Field(default=None, max_length=6000)


class CanonicalReviewInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reviewer: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=6000)
    decision: Literal['recommend', 'defer', 'approve']


def safe_artifact(root: Path, relative: str) -> Path:
    """Reject traversal and symlinks at every path component, including parents."""
    if not isinstance(relative, str) or not relative or '\x00' in relative or '\\' in relative:
        raise HTTPException(404, 'Artifact not found.')
    path = Path(relative)
    if path.is_absolute() or any(part in {'.', '..'} for part in relative.split('/')):
        raise HTTPException(404, 'Artifact not found.')
    root = Path(root)
    if root.is_symlink():
        raise HTTPException(404, 'Artifact not found.')
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise HTTPException(404, 'Artifact not found.')
    if not current.resolve().is_relative_to(root.resolve()) or not current.is_file():
        raise HTTPException(404, 'Artifact not found.')
    return current


class CanonicalRunStore:
    """Persistent run metadata; canonical output remains in its own directory."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def directory(self, run_id):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise HTTPException(404, 'Run not found.')
        path = self.root / run_id
        if path.is_symlink():
            raise HTTPException(404, 'Run not found.')
        return path

    def get(self, run_id):
        with self.lock:
            path = self.directory(run_id) / 'run.json'
            if path.is_symlink() or not path.is_file():
                raise HTTPException(404, 'Run not found.')
            try:
                return canonical.read_json(path)
            except (ValueError, OSError) as exc:
                raise HTTPException(409, 'Run metadata is unavailable or malformed.') from exc

    def save(self, run):
        with self.lock:
            directory = self.directory(run['id'])
            directory.mkdir(exist_ok=True)
            if (directory / 'run.json').is_symlink() or (directory / 'run.json.tmp').is_symlink():
                raise HTTPException(409, 'Run metadata path is not a regular artifact.')
            write_json(directory / 'run.json', run)

    def update(self, run_id, **changes):
        with self.lock:
            run = self.get(run_id)
            run.update(changes)
            run['updated_at'] = now()
            self.save(run)
            return run

    def all(self):
        rows = []
        with self.lock:
            for path in self.root.glob('run-*/run.json'):
                if path.is_symlink() or path.parent.is_symlink():
                    continue
                try:
                    rows.append(self.get(path.parent.name))
                except HTTPException:
                    continue
        return sorted(rows, key=lambda row: row.get('created_at', ''), reverse=True)

    def artifacts(self, run_id):
        directory = self.directory(run_id)
        artifacts = []
        for path in sorted(directory.rglob('*')):
            if path.name.endswith('.tmp') or path.name.startswith('.') or path == directory / 'run.json':
                continue
            relative = path.relative_to(directory).as_posix()
            try:
                checked = safe_artifact(directory, relative)
            except HTTPException:
                continue
            artifacts.append({'path': relative, 'name': relative, 'label': relative,
                'url': f'/api/workflow/runs/{run_id}/artifacts/{quote(relative, safe="/")}',
                'bytes': checked.stat().st_size})
        return artifacts

    def stage(self, run_id, stage_id, status, detail):
        with self.lock:
            run = self.get(run_id)
            for stage in run['stages']:
                if stage['id'] == stage_id:
                    stage.update(status=status, detail=detail)
            run['log'] += f'[{now()}] {stage_id}: {status} — {detail}\n'
            run['updated_at'] = now()
            self.save(run)

    def presented(self, run_id):
        run = self.get(run_id)
        run['artifacts'] = self.artifacts(run_id)
        return run

    def recover_interrupted(self):
        for run in self.all():
            if run.get('status') in {'queued', 'running'} and not _owner_active(run.get('execution_owner')):
                error = 'The process stopped before this run completed. Partial artifacts are retained; generation is not restarted automatically.'
                run.update(status='failed', completed_at=now(), updated_at=now(),
                           errors=[*run.get('errors', []), error])
                for stage in run.get('stages', []):
                    if stage['status'] in {'pending', 'running'}:
                        stage.update(status='not_run', detail=error)
                run['log'] = run.get('log', '') + f'[{now()}] interrupted: {error}\n'
                self.save(run)


def _validate(payload: CanonicalRunInput, store: CanonicalRunStore):
    serialized = json.dumps(payload.model_dump(), ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(serialized) > 4_000_000:
        raise ValueError('The complete source and auxiliary request exceeds 4,000,000 bytes.')
    if (not payload.name.strip() or payload.model is not None and not payload.model.strip()
            or payload.source_review_model is not None and not payload.source_review_model.strip()):
        raise ValueError('Model name and any selected generation model must be nonempty.')
    if payload.feedback_repairs and payload.abstention_repairs:
        raise ValueError('abstention_repairs is a deprecated alias for feedback_repairs; supply only one budget.')
    feedback_repairs = payload.feedback_repairs or payload.abstention_repairs
    if feedback_repairs and payload.condition not in {'B', 'C'}:
        raise ValueError('Feedback repair requires a separate B or C candidate.')
    if payload.source_review_model is not None or payload.obligation_inventory is not None:
        raise ValueError('Separate source-review models and obligation inventories are historical options; new runs use source-grounded feedback without an eligibility gate.')
    if payload.tlr is not None and payload.condition == 'A':
        raise ValueError('Condition A accepts a supplied SysML candidate, not TLR.')
    if payload.sysml_text is not None and payload.condition != 'A':
        raise ValueError('A supplied SysML candidate requires condition A.')
    if payload.sysml_text is not None and not payload.sysml_text.strip():
        raise ValueError('Supplied SysML must be nonempty.')
    if payload.development_scenarios is not None and (payload.condition != 'C' or not feedback_repairs):
        raise ValueError('Development scenarios require condition C and a positive feedback repair budget.')
    if payload.parent_run_id:
        parent = store.get(payload.parent_run_id)
        if parent['status'] in {'queued', 'running'}:
            raise ValueError('Wait for the parent run to finish before creating its revision.')
    # Use the exact CLI source importer, which preserves nested JSON context and
    # rejects duplicate JSON keys. The temporary input is not a generated run.
    with tempfile.TemporaryDirectory(prefix='mbse-source-') as temporary:
        source = Path(temporary) / ('source.' + {'text': 'txt', 'csv': 'csv', 'json': 'json'}[payload.format])
        source.write_text(payload.text, encoding='utf-8')
        sources = canonical.sources_from_file(source, payload.format)
    context = canonical._fixed_context(payload.context)
    tlr = canonical._normalize_candidate(payload.tlr, sources, context, False) if payload.tlr is not None else None
    suite = payload.development_scenarios
    if suite is not None:
        from canonical_scenarios import validate_scenario_suite
        suite = validate_scenario_suite(suite, sources, tlr)
    return sources, context, tlr, suite


def create_run(store, payload):
    try:
        sources, context, tlr, suite = _validate(payload, store)
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError) as exc:
        raise HTTPException(422, str(exc)) from exc
    with store.lock:
        if sum(run['status'] in {'queued', 'running'} for run in store.all()) >= 6:
            raise HTTPException(429, 'Six runs are active or queued; wait for a run to finish.')
        run_id = 'run-' + uuid.uuid4().hex[:16]
        directory = store.directory(run_id)
        directory.mkdir()
        input_file = 'source.' + {'text': 'txt', 'csv': 'csv', 'json': 'json'}[payload.format]
        (directory / input_file).write_text(payload.text, encoding='utf-8')
        write_json(directory / 'sources.json', sources)
        request = payload.model_dump()
        request.update(context=context, tlr=tlr, development_scenarios=suite)
        write_json(directory / 'request.json', request)
        if context is not None:
            write_json(directory / 'context.json', context)
        if suite is not None:
            write_json(directory / 'development_suite.json', suite)
        config = {key: deepcopy(request[key]) for key in ('condition', 'model', 'format_repairs')}
        config.update(model=canonical._model(payload.model), compile_model=True, solver='z3', timeout_seconds=10,
            feedback_repairs=payload.feedback_repairs or payload.abstention_repairs,
            deprecated_abstention_alias_used=bool(payload.abstention_repairs),
            source_review_required=False, obligation_inventory_required=False,
            supplied_tlr=tlr is not None, supplied_sysml=payload.sysml_text is not None,
            fixed_context=context is not None, development_scenarios=suite is not None,
            budget_policy='Explicit GUI budgets; the same values are passed to the canonical CLI core.')
        write_json(directory / 'request_configuration.json', config)
        stamp = now()
        run = {'id': run_id, 'name': payload.name.strip(), 'workflow': 'canonical', 'status': 'queued',
            'created_at': stamp, 'updated_at': stamp, 'completed_at': None, 'execution_owner': _owner(),
            'parent_run_id': payload.parent_run_id, 'revision_rationale': payload.revision_rationale,
            'input_file': input_file, 'format': payload.format, 'condition': payload.condition,
            'request_configuration': config, 'fixed_context': deepcopy(context),
            'development_scenarios': deepcopy(suite), 'sources': sources, 'requirements': sources,
            'source_packet': sources, 'canonical_result': None, 'model_text': None,
            'tlr': None, 'analysis': {'status': 'not_run'}, 'compilation': {'status': 'not_run'},
            'source_review': {'status': 'not_run'}, 'source_fidelity': 'not_assessed',
            'admission': 'not_assessed', 'representation': None, 'configuration': config,
            'development_results': None, 'reviews': [], 'evaluations': [], 'artifacts': [], 'errors': [],
            'stages': [{'id': sid, 'label': label, 'status': 'pending', 'detail': ''} for sid, label in STAGES],
            'log': ''}
        run['stages'][0].update(status='passed', detail=f'Preserved {len(sources)} source requirements and their supplied context.')
        store.save(run)
        return run


def _apply_result(store, run_id, result):
    directory = store.directory(run_id)
    changes = {key: deepcopy(result[key]) for key in COPY_FIELDS if key in result}
    model_path = directory / 'canonical' / 'model.sysml'
    changes.update(canonical_result=deepcopy(result), status=result['status'], completed_at=now(),
                   model_text=model_path.read_text(encoding='utf-8') if model_path.is_file() and not model_path.is_symlink() else None)
    scenario_path = directory / 'canonical' / 'development_scenarios' / 'scenarios.json'
    if scenario_path.is_file() and not scenario_path.is_symlink():
        changes['development_results'] = canonical.read_json(scenario_path)
    store.update(run_id, **changes)
    representation = result.get('representation') or {}
    tlr = result.get('tlr')
    if tlr is not None:
        complete = representation.get('formalization_status') == 'constraints_generated'
        detail = (f"{representation.get('constraints_generated', 0)}/{len(tlr['requirements'])} generated executable abstractions; "
                  "Schema-valid supported rows are emitted as a draft. Source fidelity is unassessed; "
                  "independent final assessment and engineer approval remain separate.")
        store.stage(run_id, 'interpretation', 'passed' if complete else 'partial', detail)
    else:
        store.stage(run_id, 'interpretation', 'not_run' if result.get('condition') == 'A' else 'failed',
                    'Direct SysML generation has no TLR.' if result.get('condition') == 'A' else 'No validated TLR was produced; inspect the retained error and response.')
    store.stage(run_id, 'generation', 'passed' if changes['model_text'] else 'failed',
                'Selected SysML artifact retained unchanged from the canonical conversion.' if changes['model_text'] else 'No SysML candidate was emitted.')
    audit = result.get('analysis') or {}
    verdict = audit.get('consistency_status', 'not_run')
    stage_status = 'passed' if audit.get('status') == 'passed' else 'failed' if verdict == 'unsat' else 'not_run' if audit.get('status') == 'not_run' else 'partial'
    store.stage(run_id, 'analysis', stage_status,
                f"Supported-set consistency: {verdict}; audit status: {audit.get('status', 'not_run')}. This is not design verification or source fidelity.")
    repair = result.get('feedback_repair') or result.get('repair') or {}
    repair_failed = repair.get('stop_reason') in {'context_revision_required', 'development_scenario_encoding_error',
        'initial_compilation_failed', 'proposal_compilation_failed', 'candidate_check_failed'}
    store.stage(run_id, 'refinement', 'failed' if repair_failed else 'completed' if repair.get('repair_attempts') else 'not_run',
                f"{repair.get('repair_attempts', 0)} proposal attempts; {repair.get('accepted_repairs', 0)} accepted; stop: {repair.get('stop_reason', 'not enabled')}. Selection does not constitute engineering approval.")
    scenarios = changes.get('development_results')
    store.stage(run_id, 'scenarios', scenarios.get('status', 'not_run') if scenarios else 'not_run',
                json.dumps(scenarios.get('counts', {})) + '; declared development assistance, not independent assessment.' if scenarios else 'No development scenario suite was supplied.')
    compilation = result.get('compilation') or {}
    store.stage(run_id, 'compilation', compilation.get('status', 'not_run'),
                'Compiler outcome for the selected SysML artifact; inspect diagnostics separately from logical and semantic assessment.')


def run_job(store, run_id, generator=None, reviewer=None):
    store.update(run_id, status='running', execution_owner=_owner())
    directory = store.directory(run_id)
    try:
        request = canonical.read_json(directory / 'request.json')
        sources = canonical.read_json(directory / 'sources.json')
        config = store.get(run_id)['request_configuration']
        ask = generator or canonical._ask
        def tracked(system, prompt, model, attempt_dir, call_id):
            stage = 'interpretation' if call_id == 'generation' or 'format' in call_id else 'refinement'
            store.stage(run_id, stage, 'running', f'Canonical {call_id}: {attempt_dir.relative_to(directory).as_posix()}.')
            return ask(system, prompt, model, attempt_dir, call_id)
        store.stage(run_id, 'interpretation', 'running', 'Execute the canonical CLI core with the declared source packet and repair policy.')
        result = canonical.run_candidate(sources, directory / 'canonical', condition=request['condition'],
            model=config['model'], name=request['name'], context=request['context'], tlr=request['tlr'],
            sysml_text=request['sysml_text'], compile_model=True, solver=config['solver'], timeout_seconds=config['timeout_seconds'],
            generator=tracked, feedback_repairs=config['feedback_repairs'], format_repairs=config['format_repairs'],
            development_scenarios=request['development_scenarios'])
        _apply_result(store, run_id, result)
    except Exception as exc:
        message = f'{type(exc).__name__}: {exc}'
        run = store.get(run_id)
        store.update(run_id, status='failed', completed_at=now(), errors=[*run.get('errors', []), message])
        for stage in run['stages']:
            if stage['status'] in {'pending', 'running'}:
                store.stage(run_id, stage['id'], 'failed' if stage['status'] == 'running' else 'not_run', message)
    finally:
        store.update(run_id, artifacts=store.artifacts(run_id))


def create_router(data_dir: Path, executor, *, generator=None, reviewer=None):
    store = CanonicalRunStore(data_dir)
    store.recover_interrupted()
    router = APIRouter(prefix='/api/workflow')
    router.store = router.workflow_store = store
    router.workflow_executor = executor

    @router.get('/config')
    def config():
        from review_sysml import compiler_capability
        model = canonical._model()
        return {'workflow': 'canonical', 'model': model, 'default_model': model,
            'source_fidelity': 'not_assessed',
            'profile': PROFILE, 'abstraction_policy': deepcopy(POLICY),
            'conditions': ['A', 'B', 'C', 'BC'], 'default_condition': 'C',
            'repair_policies': ['none', 'feedback'],
            'default_feedback_repairs': 2, 'default_format_repairs': 0,
            'capabilities': {'generator': {'available': bool(shutil.which('codex')), 'provider': 'codex'},
                'solver': {'available': bool(shutil.which('z3')), 'backend': 'z3'}, 'compiler': compiler_capability()},
            'limits': {'max_requirements': 500, 'max_bytes': 512000, 'max_requirement_characters': 4000,
                       'max_variables': 24, 'max_assumptions': 40, 'max_repairs': 5, 'max_request_bytes': 4000000},
            'scope': 'The same canonical CLI implementation and explicit options; no generated design behavior or automatic engineer approval.'}

    @router.get('/runs')
    def list_runs():
        return store.all()

    @router.post('/runs', status_code=202)
    def submit(payload: CanonicalRunInput):
        run = create_run(store, payload)
        try:
            executor.submit(run_job, store, run['id'], generator, reviewer)
        except Exception as exc:
            store.update(run['id'], status='failed', completed_at=now(), errors=[f'Could not start the worker: {exc}'])
            raise HTTPException(503, 'Could not start the worker; the request record was retained.') from exc
        return store.presented(run['id'])

    @router.get('/runs/{run_id}')
    def get_run(run_id: str):
        return store.presented(run_id)

    @router.get('/runs/{run_id}/artifacts/{path:path}')
    def artifact(run_id: str, path: str):
        store.get(run_id)
        allowed = {row['path'] for row in store.artifacts(run_id)}
        if path not in allowed:
            raise HTTPException(404, 'Artifact not found.')
        target = safe_artifact(store.directory(run_id), path)
        return FileResponse(target, filename=target.name, media_type='application/octet-stream')

    @router.get('/runs/{run_id}/packet')
    def packet(run_id: str):
        run = store.presented(run_id)
        files = {}
        for artifact in run['artifacts']:
            path = safe_artifact(store.directory(run_id), artifact['path'])
            files[artifact['path']] = {'text': path.read_text(encoding='utf-8', errors='replace'), 'bytes': path.stat().st_size}
        return JSONResponse({'schema': 'canonical_review_packet/1', 'exported_at': now(), 'run': run, 'files': files,
            'scope': 'Recorded source, selected candidate and exact attempt evidence; user review is separate from machine admission.'},
            headers={'Content-Disposition': f'attachment; filename="{run_id}-packet.json"'})

    @router.post('/runs/{run_id}/reviews', status_code=201)
    def review(run_id: str, payload: CanonicalReviewInput):
        if not payload.reviewer.strip() or not payload.rationale.strip():
            raise HTTPException(422, 'Provide a reviewer and a nonempty explanation.')
        with store.lock:
            run = store.get(run_id)
            if run['status'] in {'queued', 'running'}:
                raise HTTPException(409, 'Wait for a frozen candidate before recording its review.')
            record = {**payload.model_dump(), 'reviewer': payload.reviewer.strip(), 'rationale': payload.rationale.strip(),
                'id': 'review-' + uuid.uuid4().hex[:12], 'created_at': now(),
                'selected_attempt': (run.get('feedback_repair') or run.get('repair') or {}).get('selected_attempt', 'initial'),
                'scope': 'Recorded engineering opinion, separate from solver admission and independent LLM assessment.'}
            run['reviews'].append(record)
            write_json(store.directory(run_id) / 'reviews.json', run['reviews'])
            store.save(run)
        return store.presented(run_id)

    return router
