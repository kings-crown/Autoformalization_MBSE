"""Shared execution policy, including the generator subprocess boundary."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import requirements_pipeline as generator
import review_configuration as config
import review_pipeline_adapter as adapter


class SharedConfigurationTests(unittest.TestCase):
    def test_local_profile_never_selects_an_llm(self):
        with patch.object(adapter, '_selected_model') as select:
            record = config.execution_configuration('local', 'requirements')
        select.assert_not_called()
        self.assertIsNone(record['generation']['provider'])
        self.assertFalse(record['intent_formalization']['enabled'])
        self.assertFalse(record['design_check']['enabled'])
        self.assertTrue(record['compilation']['enabled'])

    def test_pipeline_subprocess_uses_the_recorded_policy_and_solver(self):
        observed = []
        def child(command, **kwargs):
            observed.append((command, kwargs['env']))
            return Mock(poll=Mock(return_value=0), returncode=0)
        for backend in ('z3', 'cvc5', 'portfolio'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                with patch.object(generator, '_SOLVER_RUNNER', SimpleNamespace(name=backend)), patch.object(adapter, '_selected_model', return_value=('replay-model', 'fixture')), patch.object(adapter.subprocess, 'Popen', side_effect=child), patch.dict(os.environ, {'MBSE_SOLVER': 'different-parent-env'}):
                    policy = config.execution_configuration('pipeline', 'requirements')
                    adapter.run_existing_pipeline(Path(directory), 'Recorded model', [{'id': 'R1', 'text': 'battery.voltage <= 28 V'}])
                command, env = observed[-1]
                self.assertEqual(env['MBSE_SOLVER'], policy['solver']['backend'])
                self.assertEqual(env['CODEX_MBSE_MODEL'], policy['generation']['model'])
                self.assertEqual(env['SMT_MAX_SEMANTIC_REPAIRS'], str(policy['generation']['semantic_repair_attempts']))
                self.assertEqual(env['SMT_FIX_ATTEMPTS'], str(policy['generation']['smt_fix_attempts']))
                self.assertEqual(command[command.index('--sysml-mode') + 1], policy['generation']['sysml_mode'])
                for flag in ('--skip-intent-formalization', '--semantic-strict', '--skip-sysml-compile'):
                    self.assertIn(flag, command)

    def test_portfolio_capability_requires_both_configured_executables(self):
        runner = generator.PortfolioRunner(generator.Z3Runner('/fixture/z3'), generator.Cvc5Runner('/fixture/cvc5'))
        with patch.object(generator, '_SOLVER_RUNNER', runner), patch('shutil.which', side_effect=lambda path: path if path == '/fixture/z3' else None):
            result = config.solver_capability()
        self.assertFalse(result['available'])
        self.assertEqual(result['backend'], 'portfolio')
        self.assertEqual([tool['path'] for tool in result['tools']], ['/fixture/z3', '/fixture/cvc5'])

    def test_design_check_is_only_enabled_by_explicit_mode(self):
        for mode, expected in [('requirements', False), ('propose_design', False), ('check_design', True)]:
            self.assertIs(config.execution_configuration('local', mode)['design_check']['enabled'], expected)


if __name__ == '__main__':
    unittest.main()
