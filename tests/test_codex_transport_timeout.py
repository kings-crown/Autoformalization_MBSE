"""Offline coverage for per-call settings and owned Codex process cleanup."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import canonical_cli
import requirements_pipeline as transport


class CodexReasoningEffortTests(unittest.TestCase):
    def test_default_environment_and_explicit_effort(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(transport._codex_reasoning_effort(), 'low')
        with patch.dict(os.environ, {'CODEX_REASONING_EFFORT': 'medium'}):
            self.assertEqual(transport._codex_reasoning_effort(), 'medium')
            for effort in transport.CODEX_REASONING_EFFORTS:
                self.assertEqual(transport._codex_reasoning_effort(effort), effort)
            self.assertEqual(os.environ['CODEX_REASONING_EFFORT'], 'medium')

    def test_invalid_effort_never_starts_a_process(self):
        for effort in ('', 'HIGH', 'unknown', 'high"\nsandbox_mode="danger-full-access', True, 3, [], {}):
            with self.subTest(effort=effort), patch.object(transport.shutil, 'which') as which:
                with self.assertRaisesRegex(ValueError, 'reasoning effort must'):
                    transport._run_codex_exec('source', 'model', reasoning_effort=effort)
                which.assert_not_called()
        with patch.dict(os.environ, {'CODEX_REASONING_EFFORT': 'invalid'}):
            with self.assertRaises(ValueError):
                transport._codex_reasoning_effort()
            self.assertEqual(transport._codex_reasoning_effort('high'), 'high')

    def test_ask_preserves_two_argument_transport_and_records_effective_default(self):
        def old_transport(prompt, model):
            return 'old transport answer'
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'CODEX_REASONING_EFFORT': 'medium'}), \
                patch.object(transport, '_run_codex_exec', side_effect=old_transport) as run:
            self.assertEqual(canonical_cli._ask('system', 'source', 'model', Path(directory), 'proposal'),
                             'old transport answer')
            self.assertEqual(run.call_args.kwargs, {})
            self.assertEqual(json.loads((Path(directory) / 'proposal.json').read_text())['reasoning_effort'],
                             'medium')

    def test_ask_forwards_explicit_settings_and_records_failure(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'CODEX_REASONING_EFFORT': 'low'}), \
                patch.object(transport, '_run_codex_exec', side_effect=RuntimeError('transport failed')) as run:
            with self.assertRaisesRegex(RuntimeError, 'transport failed'):
                canonical_cli._ask('system', 'source', 'model', Path(directory), 'proposal',
                                   timeout_seconds=900, reasoning_effort='high')
            self.assertEqual(run.call_args.kwargs, {'timeout_seconds': 900.0, 'reasoning_effort': 'high'})
            receipt = json.loads((Path(directory) / 'proposal.json').read_text())
            self.assertEqual(receipt['reasoning_effort'], 'high')
            self.assertEqual(receipt['status'], 'failed')
            self.assertEqual(os.environ['CODEX_REASONING_EFFORT'], 'low')

    def test_ask_rejects_invalid_effort_before_transport(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(transport, '_run_codex_exec') as run:
            with self.assertRaisesRegex(ValueError, 'reasoning effort must'):
                canonical_cli._ask('system', 'source', 'model', Path(directory), 'proposal',
                                   reasoning_effort='unknown')
            run.assert_not_called()

    def test_concurrent_calls_keep_effort_separate_in_command_and_receipt(self):
        rendezvous = threading.Barrier(2)
        commands = []

        def launch(command, **kwargs):
            commands.append(command)
            setting = next(value for value in command if value.startswith('model_reasoning_effort='))
            Path(command[command.index('--output-last-message') + 1]).write_text(setting)
            process = MagicMock()
            process.__enter__.return_value = process
            process.returncode = 0
            def communicate(prompt, timeout):
                rendezvous.wait(timeout=5)
                return '', ''
            process.communicate.side_effect = communicate
            return process

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'CODEX_REASONING_EFFORT': 'low'}), \
                patch.object(transport.shutil, 'which', return_value='/fake/codex'), \
                patch.object(transport.subprocess, 'Popen', side_effect=launch), \
                ThreadPoolExecutor(max_workers=2) as executor:
            futures = {effort: executor.submit(canonical_cli._ask, 'system', 'source', 'model',
                                              Path(directory), effort, reasoning_effort=effort)
                       for effort in ('medium', 'high')}
            for effort, future in futures.items():
                self.assertEqual(future.result(timeout=10), f'model_reasoning_effort="{effort}"')
                receipt = json.loads((Path(directory) / (effort + '.json')).read_text())
                self.assertEqual(receipt['reasoning_effort'], effort)
                self.assertEqual(receipt['status'], 'completed')
            self.assertEqual(os.environ['CODEX_REASONING_EFFORT'], 'low')
            self.assertEqual(len(commands), 2)
            for command in commands:
                self.assertFalse(Path(command[command.index('--output-last-message') + 1]).exists())


class CodexTimeoutTests(unittest.TestCase):
    def test_default_environment_and_explicit_timeout(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(transport._codex_timeout_seconds(), 180)
        with patch.dict(os.environ, {'CODEX_EXEC_TIMEOUT': '240'}):
            self.assertEqual(transport._codex_timeout_seconds(), 240)
            self.assertEqual(transport._codex_timeout_seconds(900), 900)
            self.assertEqual(os.environ['CODEX_EXEC_TIMEOUT'], '240')

    def test_invalid_deadlines_are_rejected(self):
        for value in (0, -1, True, '900', float('inf'), float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                transport._codex_timeout_seconds(value)

    def test_ask_logs_explicit_deadline_without_mutating_environment(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'CODEX_EXEC_TIMEOUT': '180'}), \
                patch.object(transport, '_run_codex_exec', return_value='answer') as run:
            actual = canonical_cli._ask('system', 'source', 'model', Path(directory), 'proposal', timeout_seconds=900)
            self.assertEqual(actual, 'answer')
            self.assertEqual(run.call_args.kwargs, {'timeout_seconds': 900.0})
            self.assertEqual(json.loads((Path(directory) / 'proposal.json').read_text())['timeout_seconds'], 900)
            self.assertEqual(os.environ['CODEX_EXEC_TIMEOUT'], '180')

    def test_ask_failure_keeps_deadline_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(transport, '_run_codex_exec', side_effect=RuntimeError('timeout')):
            with self.assertRaisesRegex(RuntimeError, 'timeout'):
                canonical_cli._ask('system', 'source', 'model', Path(directory), 'proposal', timeout_seconds=901)
            receipt = json.loads((Path(directory) / 'proposal.json').read_text())
            self.assertEqual(receipt['status'], 'failed')
            self.assertEqual(receipt['timeout_seconds'], 901)

    def test_temp_output_removed_on_process_start_failure(self):
        with patch.object(transport.shutil, 'which', return_value='/fake/codex'), \
                patch.object(transport.subprocess, 'Popen', side_effect=OSError('start failed')) as popen:
            with self.assertRaisesRegex(OSError, 'start failed'):
                transport._run_codex_exec('prompt', 'model', timeout_seconds=900)
            command = popen.call_args.args[0]
            self.assertFalse(Path(command[command.index('--output-last-message') + 1]).exists())

    @unittest.skipUnless(os.name == 'posix', 'POSIX process group ownership')
    def test_timeout_kills_group_reaps_launcher_and_removes_output(self):
        process = MagicMock()
        process.pid = 54321
        process.__enter__.return_value = process
        process.communicate.side_effect = [subprocess.TimeoutExpired('codex', 900), ('', 'model startup')]
        with patch.object(transport.shutil, 'which', return_value='/fake/codex'), \
                patch.object(transport.subprocess, 'Popen', return_value=process) as popen, \
                patch.object(transport.os, 'killpg') as killpg, \
                patch.dict(os.environ, {'CODEX_STREAM': 'false'}):
            with self.assertRaisesRegex(RuntimeError, 'after 900s'):
                transport._run_codex_exec('prompt', 'model', timeout_seconds=900)
            killpg.assert_called_once_with(54321, signal.SIGKILL)
            self.assertEqual(process.communicate.call_count, 2)
            self.assertEqual(process.communicate.call_args_list[0].kwargs, {'timeout': 900.0})
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            command = popen.call_args.args[0]
            self.assertFalse(Path(command[command.index('--output-last-message') + 1]).exists())

    def test_offline_success_preserves_answer_and_cleans_file(self):
        process = MagicMock()
        process.__enter__.return_value = process
        process.returncode = 0
        def launch(command, **kwargs):
            Path(command[command.index('--output-last-message') + 1]).write_text('response')
            return process
        process.communicate.return_value = ('diagnostic stdout', '')
        with patch.object(transport.shutil, 'which', return_value='/fake/codex'), \
                patch.object(transport.subprocess, 'Popen', side_effect=launch) as popen:
            self.assertEqual(transport._run_codex_exec('prompt', 'model', timeout_seconds=900), 'response')
            command = popen.call_args.args[0]
            self.assertFalse(Path(command[command.index('--output-last-message') + 1]).exists())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process status inspection')
    def test_real_timeout_stops_launcher_and_native_child_without_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_pid = root / 'child.pid'
            launcher = root / 'fake-codex'
            child_code = 'import time; time.sleep(30)'
            launcher.write_text(
                '#!' + sys.executable + '\n'
                'import pathlib, subprocess, sys\n'
                'sys.stdin.read()\n'
                f'child = subprocess.Popen([sys.executable, "-c", {child_code!r}])\n'
                f'pathlib.Path({str(child_pid)!r}).write_text(str(child.pid))\n'
                'child.wait()\n')
            launcher.chmod(0o700)
            with patch.object(transport.shutil, 'which', return_value=str(launcher)):
                try:
                    with self.assertRaisesRegex(RuntimeError, 'after 1s'):
                        transport._run_codex_exec('offline fixture', 'unused', timeout_seconds=1)
                    pid = int(child_pid.read_text())
                    status = Path(f'/proc/{pid}/stat')
                    # An orphan already killed by the group can briefly remain
                    # a zombie until the host's init reaps it; it is not running.
                    self.assertTrue(not status.exists() or status.read_text().split()[2] in {'Z', 'X'})
                finally:
                    if child_pid.exists():
                        try:
                            os.kill(int(child_pid.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == '__main__':
    unittest.main()
