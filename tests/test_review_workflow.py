"""Shared transport-free creation and safe API/CLI store coexistence."""
from pathlib import Path
import multiprocessing
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from review_workflow import RunInput, RunStore, WorkflowError, create_run, process_identity, run_owner_active


def write_from_process(root, run_id, ready, acquired):
    store = RunStore(Path(root))
    ready.set()
    with store.lock:
        acquired.set()
        store.update(run_id, child_observation=store.get(run_id).get('parent_update'))


class SharedWorkflowTests(unittest.TestCase):
    def test_create_run_without_http_or_worker_uses_review_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            run = create_run(store, RunInput(text='battery.voltage <= 28 V'))
            self.assertEqual(run['status'], 'queued')
            self.assertEqual(run['analysis_mode'], 'requirements')
            self.assertIsNone(run['behavior'])
            self.assertTrue(run_owner_active(run))
            self.assertEqual(store.get(run['id']), run)
            self.assertTrue((store.directory(run['id']) / 'source.txt').is_file())
            self.assertFalse((store.directory(run['id']) / 'model.sysml').exists())

    def test_invalid_shared_request_does_not_allocate_a_run(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            with self.assertRaises(WorkflowError) as error:
                create_run(store, RunInput(text='battery.voltage <= 28 V', analysis_mode='check_design'))
            self.assertEqual(error.exception.status_code, 422)
            self.assertEqual(store.all(), [])
            with self.assertRaises(WorkflowError) as missing:
                store.get('../outside')
            self.assertEqual(missing.exception.status_code, 404)

    def test_process_lock_blocks_other_process_and_preserves_nested_updates(self):
        context = multiprocessing.get_context('spawn')
        ready, acquired = context.Event(), context.Event()
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            run = create_run(store, RunInput(text='battery.voltage <= 28 V'))
            process = context.Process(target=write_from_process, args=(directory, run['id'], ready, acquired))
            try:
                with store.lock:
                    with store.lock:
                        process.start()
                        self.assertTrue(ready.wait(5), 'Child did not attempt to acquire store lock')
                        self.assertFalse(acquired.wait(.2), 'Child crossed an active process transaction')
                        store.update(run['id'], parent_update='preserved')
                self.assertTrue(acquired.wait(5), 'Child remained blocked after outer release')
                process.join(5)
                self.assertEqual(process.exitcode, 0)
                self.assertEqual(store.get(run['id'])['child_observation'], 'preserved')
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(5)

    def test_process_birth_token_rejects_reused_or_dead_owner(self):
        identity = process_identity()
        self.assertEqual(identity['pid'], os.getpid())
        self.assertTrue(run_owner_active({'execution_owner': identity}))
        self.assertFalse(run_owner_active({'execution_owner': {**identity, 'start_ticks': 'different'}}))
        self.assertFalse(run_owner_active({'execution_owner': {'pid': -1, 'start_ticks': 'x'}}))
        self.assertFalse(run_owner_active({}))
        with patch('review_workflow.process_identity', return_value=None):
            self.assertFalse(run_owner_active({'execution_owner': identity}))

    def test_api_startup_keeps_live_cli_request_and_recovers_abandoned_run(self):
        from fastapi.testclient import TestClient
        from review_server import create_app
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory))
            live = create_run(store, RunInput(text='battery.voltage <= 28 V'))
            abandoned = create_run(store, RunInput(text='battery.mass <= 2 kg'))
            store.update(abandoned['id'], execution_owner={'pid': -1, 'start_ticks': 'dead'})
            with TestClient(create_app(Path(directory))) as client:
                self.assertEqual(client.get('/api/runs/' + live['id']).json()['status'], 'queued')
                recovered = client.get('/api/runs/' + abandoned['id']).json()
                self.assertEqual(recovered['status'], 'failed')
                self.assertIn('service stopped', recovered['errors'][0])


if __name__ == '__main__':
    unittest.main()
