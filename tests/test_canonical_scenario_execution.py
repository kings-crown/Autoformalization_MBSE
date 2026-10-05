"""Development scenario CLI isolation and bounded repair with real local Z3.

Model responses and compilation are controlled fixtures. Assertions exercise
actual scenario queries, rejection/selection, saved evidence and prompt routing.
No provider transport or independent evaluation answers are involved.
"""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import canonical_cli as cli
from canonical_feedback import validate_feedback_proposal
from canonical_tlr import tlr_context, validate_tlr


def fixture(bound='28'):
    sources = [{'id': 'R1', 'text': 'Battery voltage shall be at most 28 V.',
                'source': {'context': {'shared_document_context': {'source_excerpts': [
                    {'id': 'DEF_LIMIT', 'quote': 'At most includes the boundary value.'}]}}}}]
    raw = {'schema': 'mbse_tlr/1', 'abstraction_policy': 'mbse_abstraction/1',
           'variables': [{'name': 'voltage', 'type': 'Real', 'unit': 'V',
                          'description': 'Observed battery voltage.'}],
           'assumptions': [], 'requirements': [{
               'id': 'R1', 'status': 'supported',
               'formula': {'op': '<=', 'args': [{'var': 'voltage'}, {'value': bound, 'unit': 'V'}]},
               'abstraction': {'kind': 'state_constraint', 'meaning': 'Battery voltage has an upper bound.',
                               'scope': 'One voltage observation.', 'limitations': []}}]}
    tlr = validate_tlr(raw, sources)
    suite = {'schema': 'development_scenarios/1', 'role': 'development',
             'origin': {'kind': 'fixture', 'description': 'Source-based boundary examples for controller tests.'},
             'context': tlr_context(tlr), 'symbol_meanings': {'voltage': 'Observed battery voltage.'},
             'scenarios': []}
    for sid, value, expected in [('BOUNDARY', '28', 'sat'), ('OVER_LIMIT', '29', 'unsat')]:
        suite['scenarios'].append({'id': sid, 'requirement_ids': ['R1'],
            'description': f'Observed battery voltage equals {value} V.',
            'predicate': {'op': '=', 'args': [{'var': 'voltage'}, {'value': value, 'unit': 'V'}]},
            'expected': expected,
            'source_basis': [{'source_id': 'R1', 'quote': sources[0]['text']},
                             {'source_id': 'DEF_LIMIT', 'quote': 'At most includes the boundary value.'}],
            'rationale': 'The literal upper bound permits equality and prohibits a larger voltage.'})
    return sources, tlr, suite


def bound_changed(tlr, value):
    result = deepcopy(tlr)
    result['requirements'][0]['formula']['args'][1]['value'] = value
    return result


def proposal(sources, before, after):
    old = validate_tlr(before, sources)['requirements'][0]
    new = validate_tlr(after, sources)['requirements'][0]
    return {'schema': 'semantic_repair_proposal/1', 'tlr': deepcopy(after), 'reviews': [{
        'id': 'R1', 'outcome': 'retained' if old == new else 'changed',
        'reason': 'Review the stated upper bound and preserve its inclusive interpretation.',
        'source_basis': [{'source_id': 'R1', 'quote': sources[0]['text']},
                         {'source_id': 'DEF_LIMIT', 'quote': 'At most includes the boundary value.'}]}]}


def compiled(*args, **kwargs):
    return {'status': 'passed', 'diagnostics': []}


from source_review_support import pass_source_review

@patch("canonical_cli._review_ask", new=pass_source_review)
class ScenarioConfigurationTests(unittest.TestCase):
    def test_development_assistance_requires_C_and_positive_budget_before_writes_or_calls(self):
        sources, tlr, suite = fixture()
        for arm, budget in [('A', 0), ('A', 1), ('B', 0), ('B', 1), ('BC', 0), ('BC', 1), ('C', 0)]:
            with self.subTest(arm=arm, budget=budget), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'forbidden'
                provider = Mock(side_effect=AssertionError('Invalid configuration must not call the model'))
                with self.assertRaises(ValueError):
                    cli.run_candidate(sources, path, arm, model='fixture', generator=provider,
                                      feedback_repairs=budget, development_scenarios=suite)
                self.assertFalse(path.exists())
                provider.assert_not_called()

    def test_invalid_suite_is_rejected_before_output_for_run_and_study(self):
        sources, tlr, suite = fixture()
        suite['role'] = 'held_out_evaluation'
        for runner in (cli.run_candidate, cli.run_study):
            with self.subTest(runner=runner.__name__), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'invalid'
                with self.assertRaises(ValueError):
                    runner(sources, path, model='fixture', tlr=tlr,
                           feedback_repairs=1, development_scenarios=suite)
                self.assertFalse(path.exists())

    def test_study_requires_positive_budget_before_output(self):
        sources, _, suite = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'invalid'
            with self.assertRaisesRegex(ValueError, 'positive feedback'):
                cli.run_study(sources, path, model='fixture', development_scenarios=suite)
            self.assertFalse(path.exists())

    def test_cli_rejects_B_scenario_flag_before_any_output(self):
        sources, _, suite = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli.write_json(root / 'sources.json', sources)
            cli.write_json(root / 'suite.json', suite)
            provider = Mock(side_effect=AssertionError('No paid call'))
            with patch('canonical_cli._ask', provider), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(['run', '--statement', str(root / 'sources.json'), '--model', 'fixture',
                    '--condition', 'B', '--feedback-repairs', '1', '--development-scenarios', str(root / 'suite.json'),
                    '--output-dir', str(root / 'output')])
            self.assertEqual(code, 2)
            self.assertFalse((root / 'output').exists())
            provider.assert_not_called()

    def test_nested_source_quote_is_resolved_but_does_not_replace_own_requirement_quote(self):
        sources, before, _ = fixture('27')
        after = bound_changed(before, '28')
        raw = proposal(sources, before, after)
        accepted = validate_feedback_proposal(raw, sources, before)
        self.assertEqual(accepted['changes']['revised_supported_ids'], ['R1'])
        raw['reviews'][0]['source_basis'] = raw['reviews'][0]['source_basis'][1:]
        with self.assertRaisesRegex(ValueError, 'own source text quote'):
            validate_feedback_proposal(raw, sources, before)
        raw = proposal(sources, before, after)
        raw['reviews'][0]['source_basis'][1]['quote'] = 'This definition is invented.'
        with self.assertRaisesRegex(ValueError, 'not literal'):
            validate_feedback_proposal(raw, sources, before)


@unittest.skipUnless(shutil.which('z3'), 'Local Z3 required; provider calls remain mocked')
@patch("canonical_cli._review_ask", new=pass_source_review)
class ScenarioExecutionTests(unittest.TestCase):
    def test_invalid_initial_development_context_stops_before_any_model_call(self):
        for defect in ('definition', 'background', 'encoding'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp, \
                    patch('canonical_cli._compile', side_effect=compiled):
                sources, initial, suite = fixture()
                kwargs = {}
                if defect == 'definition':
                    suite['symbol_meanings']['voltage'] = 'A different voltage definition.'
                elif defect == 'background':
                    suite['context']['background'] = [{'id': 'NEW_ENV',
                        'text': 'An environmental assumption absent from the fixed candidate.',
                        'predicate': {'op': '>=', 'args': [{'var': 'voltage'}, {'value': '0', 'unit': 'V'}]}}]
                else:
                    # The scenario runner records invalid solver settings as an
                    # encoding error; no proposal can repair those settings.
                    kwargs['timeout_seconds'] = 0
                provider = Mock(side_effect=AssertionError('Fixed input errors must not trigger a model call'))
                path = Path(tmp) / 'C'
                result = cli.run_candidate(sources, path, 'C', model='fixture', tlr=initial,
                    feedback_repairs=2, generator=provider, development_scenarios=suite, **kwargs)
                provider.assert_not_called()
                self.assertEqual(result['status'], 'completed', result)
                ledger = result['feedback_repair']
                self.assertEqual(ledger['repair_attempts'], 0)
                self.assertEqual(ledger['accepted_repairs'], 0)
                self.assertEqual(ledger['steps'], [])
                self.assertEqual(ledger['selected_attempt'], 'feedback_attempts/000')
                expected = 'development_scenario_encoding_error' if defect == 'encoding' else 'context_revision_required'
                self.assertEqual(ledger['stop_reason'], expected)
                self.assertEqual(ledger['initial_development_results']['status'], 'encoding_error')
                self.assertEqual(ledger['final_development_results'], ledger['initial_development_results'])
                self.assertEqual(result['tlr'], initial)
                self.assertEqual((path / 'model.sysml').read_bytes(),
                                 (path / 'feedback_attempts/000/model.sysml').read_bytes())
                self.assertEqual(cli.read_json(path / 'development_scenarios/scenarios.json')['status'], 'encoding_error')

    def test_passing_scenario_regression_is_rejected_and_next_review_receives_evidence(self):
        sources, initial, suite = fixture()
        stronger = bound_changed(initial, '27')
        calls = Mock(side_effect=[json.dumps(proposal(sources, initial, stronger)),
                                  json.dumps(proposal(sources, initial, initial))])
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_cli._compile', side_effect=compiled):
            path = Path(tmp) / 'C'
            result = cli.run_candidate(sources, path, 'C', model='fixture', tlr=initial,
                feedback_repairs=2, generator=calls, development_scenarios=suite)
            self.assertEqual(result['status'], 'completed', result)
            ledger = result['feedback_repair']
            self.assertEqual(ledger['accepted_repairs'], 0, ledger)
            self.assertEqual(ledger['repair_attempts'], 2)
            self.assertEqual(ledger['steps'][0]['status'], 'rejected')
            self.assertEqual(ledger['steps'][0]['development_gate']['regressions'], ['BOUNDARY'])
            self.assertEqual(ledger['steps'][1]['status'], 'unchanged')
            self.assertEqual(result['tlr'], initial)
            self.assertEqual(ledger['final_development_results']['counts']['passed'], 2)
            self.assertEqual((path / 'model.sysml').read_bytes(),
                             (path / 'feedback_attempts/000/model.sysml').read_bytes())
            saved = cli.read_json(path / 'feedback_attempts/001/development_gate.json')
            self.assertFalse(saved['acceptable'])
            comparison = cli.read_json(path / 'feedback_attempts/001/semantic_comparison/semantic_changes.json')
            self.assertEqual(comparison['requirements'][0]['classification'], 'strengthened')
            query = (path / 'feedback_attempts/001/development_scenarios/scenario_0001_candidate.smt2').read_text()
            self.assertIn('(<= v_voltage_0 27)', query)
            self.assertIn('(= v_voltage_0 28)', query)
            followup = json.loads(calls.call_args_list[1].args[1])
            self.assertEqual(followup['current_tlr']['requirements'][0]['formula'], initial['requirements'][0]['formula'])
            self.assertIn('BOUNDARY', followup['previous_failure'])
            self.assertEqual(followup['development_results']['counts']['passed'], 2)
            self.assertEqual(followup['previous_semantic_comparison']['requirements'][0]['classification'], 'strengthened')
            self.assertEqual(cli.read_json(path / 'sources.json'), sources)

    def test_valid_boundary_correction_is_selected_and_both_artifacts_regenerated(self):
        sources, initial, suite = fixture('27')
        corrected = bound_changed(initial, '28')
        calls = Mock(return_value=json.dumps(proposal(sources, initial, corrected)))
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_cli._compile', side_effect=compiled) as compiler:
            path = Path(tmp) / 'C'
            result = cli.run_candidate(sources, path, 'C', model='fixture', tlr=initial,
                feedback_repairs=1, generator=calls, development_scenarios=suite)
            self.assertEqual(result['status'], 'completed', result)
            ledger = result['feedback_repair']
            self.assertEqual(ledger['accepted_repairs'], 1, ledger)
            self.assertEqual(ledger['initial_development_results']['counts']['failed'], 1)
            self.assertEqual(ledger['final_development_results']['counts']['passed'], 2)
            self.assertEqual(ledger['steps'][0]['development_gate']['improvements'], ['BOUNDARY'])
            self.assertEqual(result['tlr'], validate_tlr(corrected, sources))
            self.assertEqual(compiler.call_count, 2)
            self.assertIn('<= 28', (path / 'model.sysml').read_text())
            self.assertIn('(<= v_voltage_0 28)', (path / 'audit/consistency.smt2').read_text())
            self.assertEqual(cli.read_json(path / 'development_scenarios/scenarios.json')['counts']['passed'], 2)
            comparison = ledger['steps'][0]['semantic_comparison']['requirements'][0]
            self.assertEqual(comparison['classification'], 'weakened')
            self.assertEqual(comparison['evidence']['newly_permitted']['status'], 'sat')
            self.assertEqual(calls.call_count, 1)

    def test_cli_study_exposes_suite_only_to_C_review_not_A_initial_or_B(self):
        sources, initial, suite = fixture('27')
        inventory = {'schema': 'mbse_obligation_inventory/1', 'requirements': [{
            'id': 'R1', 'context': [{'source_id': 'DEF_LIMIT', 'role': 'definition',
                'reason': 'Defines inclusion of the boundary.'}], 'obligations': [{
                'id': 'R1.O1', 'meaning': sources[0]['text'], 'kind': 'state_constraint',
                'source_basis': [{'source_id': 'R1', 'quote': sources[0]['text']}],
                'slots': {'subject': 'battery', 'scope': 'one observation', 'quantity': 'voltage',
                          'operator': '<=', 'bound': '28', 'unit': 'V'},
                'selection_reason': 'An inclusive source-defined scalar limit.', 'limitations': []}]}]}
        initial['requirements'][0]['coverage'] = [{'obligation_id': 'R1.O1', 'status': 'represented',
            'formula_path': '/formula', 'slots': {'quantity': ['/formula/args/0'], 'operator': ['/formula'],
                                                'bound': ['/formula/args/1'], 'unit': ['/formula/args/1']}}]
        seen = []
        def generate(system, prompt, model, directory, call_id):
            payload = json.loads(prompt)
            arm = directory.name if call_id == 'generation' else directory.parents[1].name
            seen.append((arm, call_id, payload))
            if system == cli.SYSML_INSTRUCTIONS:
                return 'package DirectCandidate {}'
            if system.startswith(cli.TLR_INSTRUCTIONS):
                return json.dumps(initial)
            current = payload['current_tlr']
            after = bound_changed(current, '28') if arm == 'C' else current
            return json.dumps(proposal(sources, current, after))
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_cli._compile', side_effect=compiled), \
                patch('canonical_cli._ask', side_effect=generate), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            root = Path(tmp)
            cli.write_json(root / 'sources.json', sources)
            cli.write_json(root / 'suite.json', suite)
            cli.write_json(root / 'inventory.json', inventory)
            code = cli.main(['study', '--statement', str(root / 'sources.json'), '--model', 'fixture',
                '--repetitions', '1', '--feedback-repairs', '1', '--development-scenarios', str(root / 'suite.json'),
                '--output-dir', str(root / 'study')])
            self.assertEqual(code, 0)
            result = cli.read_json(root / 'study/study.json')
            self.assertEqual(result['summary']['generation_failures'], 0, result)
            self.assertEqual(len(seen), 4)
            self.assertEqual([arm for arm, _, _ in seen], ['A', 'initial', 'B', 'C'])
            for arm, call_id, payload in seen:
                if arm == 'C':
                    self.assertIn('development_scenarios', payload)
                    self.assertEqual(payload['development_results']['status'], 'failed')
                    self.assertIsNotNone(payload['solver_feedback'])
                    self.assertNotIn('source_review', payload)
                else:
                    self.assertNotIn('development_scenarios', payload)
                    self.assertNotIn('development_results', payload)
                    if arm == 'B':
                        self.assertIsNone(payload['solver_feedback'])
                self.assertNotIn('judge_verdicts', payload)
            for arm in ('A', 'initial', 'B'):
                directory = root / 'study/rep-001' / arm
                self.assertFalse((directory / 'development_suite.json').exists())
                self.assertFalse((directory / 'development_scenarios').exists())
                self.assertFalse((directory / 'audit').exists())
            self.assertTrue((root / 'study/rep-001/C/development_suite.json').exists())
            self.assertEqual(result['rows'][0]['B']['configuration']['semantic_repairs'], 0)
            self.assertEqual(result['rows'][0]['C']['configuration']['semantic_repairs'], 1)
            config = cli.read_json(root / 'study/study_configuration.json')
            self.assertEqual(config['feedback_modes']['B'], 'source')
            self.assertEqual(config['feedback_modes']['C'], 'solver_and_development_scenarios')
            self.assertIn('not a solver-only ablation', config['treatment_note'])


if __name__ == '__main__':
    unittest.main()
