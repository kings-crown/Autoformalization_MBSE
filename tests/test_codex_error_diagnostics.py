"""Offline regression tests for failures after a large echoed source packet."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import requirements_pipeline as transport


class CodexErrorDiagnosticsTests(unittest.TestCase):
    def test_omits_echoed_packet_and_preserves_actual_error(self):
        prompt = 'System instructions:\n' + ('private source text\n' * 10000)
        stderr = 'Codex banner\nuser\n' + prompt + '\nERROR: request too large'
        details = transport._codex_failure_details('', stderr, prompt)
        self.assertIn('ERROR: request too large', details)
        self.assertIn('[source prompt omitted]', details)
        self.assertNotIn('private source text', details)
        self.assertLess(len(details), 300)

    def test_long_logs_keep_tail_and_both_streams(self):
        details = transport._codex_failure_details(
            'progress\n' * 2000 + '{"type":"error","message":"final error"}',
            'details\n' * 2000 + '\x1b[31mERROR: exit reason\x1b[0m', 'request')
        self.assertIn('stdout:', details)
        self.assertIn('stderr:', details)
        self.assertIn('"message":"final error"', details)
        self.assertIn('ERROR: exit reason', details)
        self.assertNotIn('\x1b', details)
        self.assertLess(len(details), 12200)

    def test_missing_diagnostics_do_not_guess_authentication_failure(self):
        self.assertEqual(transport._codex_failure_details(None, '', 'request'),
                         'Codex returned no diagnostic output.')

    def test_nonzero_exit_reports_tail_through_transport(self):
        prompt = 'request\n' * 1000
        process = MagicMock()
        process.__enter__.return_value = process
        process.returncode = 1
        process.communicate.return_value = ('', 'banner\nuser\n' + prompt + '\nERROR: provider limit')
        with patch.object(transport.shutil, 'which', return_value='/fake/codex'), \
                patch.object(transport.subprocess, 'Popen', return_value=process), \
                patch.dict(os.environ, {'CODEX_STREAM': 'false'}):
            with self.assertRaisesRegex(RuntimeError, 'ERROR: provider limit') as raised:
                transport._run_codex_exec(prompt, 'model')
        self.assertIn('exit 1', str(raised.exception))
        self.assertNotIn('authenticated', str(raised.exception))
        self.assertNotIn(prompt.strip(), str(raised.exception))

    @unittest.skipUnless(os.name == 'posix', 'POSIX cleanup')
    def test_timeout_preserves_last_progress_after_echo(self):
        prompt = 'request\n' * 1000
        process = MagicMock()
        process.pid = 12345
        process.__enter__.return_value = process
        process.communicate.side_effect = [subprocess.TimeoutExpired('codex', 10),
            ('', ('user\n' + prompt + '\nReconnecting: connection closed').encode())]
        with patch.object(transport.shutil, 'which', return_value='/fake/codex'), \
                patch.object(transport.subprocess, 'Popen', return_value=process), \
                patch.object(transport.os, 'killpg'), \
                patch.dict(os.environ, {'CODEX_STREAM': 'false'}):
            with self.assertRaisesRegex(RuntimeError, 'Reconnecting: connection closed') as raised:
                transport._run_codex_exec(prompt, 'model', timeout_seconds=10)
        self.assertIn('after 10s', str(raised.exception))
        self.assertNotIn(prompt.strip(), str(raised.exception))


if __name__ == '__main__':
    unittest.main()
