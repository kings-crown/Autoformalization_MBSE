"""Matched repair branches, exact solver feedback and retained failed proposals."""
from copy import deepcopy
from contextlib import redirect_stdout
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
from canonical_tlr import render_sysml, validate_tlr
from canonical_feedback import FEEDBACK_POLICY_VERSION
from review_sysml import compiler_capability


def fixture():
    sources = [{'id': 'R1', 'text': 'Battery voltage shall be at most 28 V.'},
               {'id': 'R2', 'text': 'Battery voltage shall be at least 20 V.'}]
    tlr = {'schema': 'mbse_tlr/1', 'abstraction_policy': 'mbse_abstraction/1',
        'variables': [{'name': 'voltage', 'type': 'Real', 'unit': 'V', 'description': 'Battery voltage.'}],
        'assumptions': [], 'requirements': []}
    for rid, op, value in [('R1', '<=', '28'), ('R2', '>=', '30')]:
        tlr['requirements'].append({'id': rid, 'status': 'supported',
            'formula': {'op': op, 'args': [{'var': 'voltage'}, {'value': value, 'unit': 'V'}]},
            'abstraction': {'kind': 'state_constraint', 'meaning': f'Battery voltage {op} {value} V.',
                            'scope': 'Battery voltage in an observation.', 'limitations': []}})
    return sources, tlr


def corrected(tlr):
    result = deepcopy(tlr)
    result['requirements'][1]['formula']['args'][1]['value'] = '20'
    result['requirements'][1]['abstraction']['meaning'] = 'Battery voltage is at least 20 V.'
    return result


def proposal(sources, before, after):
    old = {r['id']: r for r in validate_tlr(before, sources)['requirements']}
    new = {r['id']: r for r in validate_tlr(after, sources)['requirements']}
    return {'schema': 'semantic_repair_proposal/1', 'tlr': deepcopy(after), 'reviews': [
        {'id': s['id'], 'outcome': 'retained' if old[s['id']] == new[s['id']] else 'changed',
         'reason': 'The stated numeric bound is preserved, or corrected to the literal source threshold.',
         'source_basis': [{'source_id': s['id'], 'quote': s['text']}]}
        for s in sources]}


def compiled(*args, **kwargs):
    return {'status': 'passed', 'diagnostics': []}


class FeedbackExecutionTests(unittest.TestCase):
    def test_B_revises_supported_formula_without_calling_solver_and_stops_on_no_change(self):
        sources, tlr = fixture()
        fixed = corrected(tlr)
        calls = Mock(side_effect=[json.dumps(proposal(sources, tlr, fixed)),
                                  json.dumps(proposal(sources, fixed, fixed))])
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_audits.audit_tlr', side_effect=AssertionError('B must not audit')):
            path = Path(tmp) / 'B'
            result = cli.run_candidate(sources, path, 'B', model='fixture', tlr=tlr,
                                       compile_model=False, feedback_repairs=3, generator=calls)
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(result['tlr'], validate_tlr(fixed, sources))
            self.assertEqual(result['configuration']['repair_policy'], FEEDBACK_POLICY_VERSION)
            self.assertEqual(result['configuration']['semantic_repairs'], 1)
            self.assertEqual(result['feedback_repair']['stop_reason'], 'no_change_after_review')
            self.assertEqual(result['feedback_repair']['repair_attempts'], 2)
            self.assertEqual(cli.read_json(path / 'feedback_attempts/000/tlr.json'), validate_tlr(tlr, sources))
            self.assertEqual((path / 'model.sysml').read_bytes(), (path / 'feedback_attempts/001/model.sysml').read_bytes())
            self.assertEqual(cli.read_json(path / 'feedback_attempts/001/changes.json')['summary']['revised_supported_ids'], ['R2'])
            for call in calls.call_args_list:
                prompt = json.loads(call.args[1])
                self.assertEqual(prompt['source_packet'], sources)
                self.assertIsNone(prompt['solver_feedback'])
                self.assertNotIn('judgments', prompt)
                self.assertNotIn('reference_formulas', prompt)

    def test_bad_source_edit_consumes_budget_and_next_round_receives_error(self):
        sources, tlr = fixture()
        invalid = corrected(tlr)
        invalid['requirements'][1]['text'] = 'The specification has changed.'
        bad = proposal(sources, tlr, corrected(tlr))
        bad['tlr'] = invalid
        calls = Mock(side_effect=[json.dumps(bad), json.dumps(proposal(sources, tlr, corrected(tlr)))])
        with tempfile.TemporaryDirectory() as tmp:
            result = cli.run_candidate(sources, Path(tmp) / 'B', 'B', model='fixture', tlr=tlr,
                                       compile_model=False, feedback_repairs=2, generator=calls)
            self.assertEqual(result['configuration']['semantic_repairs'], 1)
            self.assertEqual(result['feedback_repair']['steps'][0]['status'], 'rejected')
            self.assertEqual(result['feedback_repair']['stop_reason'], 'budget_exhausted')
            self.assertIn('differs from prepared source', json.loads(calls.call_args_list[1].args[1])['previous_failure'])

    def test_transport_failures_and_malformed_responses_are_bounded_and_retained(self):
        sources, tlr = fixture()
        calls = Mock(side_effect=[TimeoutError('provider timeout'), '{broken JSON'])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'B'
            result = cli.run_candidate(sources, path, 'B', model='fixture', tlr=tlr,
                                       compile_model=False, feedback_repairs=2, generator=calls)
            self.assertEqual(result['tlr'], validate_tlr(tlr, sources))
            self.assertEqual(result['feedback_repair']['repair_attempts'], 2)
            self.assertEqual(result['feedback_repair']['accepted_repairs'], 0)
            self.assertEqual(result['feedback_repair']['stop_reason'], 'budget_exhausted')
            self.assertEqual((path / 'feedback_attempts/002/response.txt').read_text(), '{broken JSON')
            self.assertIn('provider timeout', result['feedback_repair']['steps'][0]['error'])

    def test_compiler_failure_keeps_last_candidate_and_stops_semantic_repair(self):
        sources, tlr = fixture()
        calls = Mock(return_value=json.dumps(proposal(sources, tlr, corrected(tlr))))
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_cli._compile', side_effect=[compiled(), {'status': 'failed'}]):
            result = cli.run_candidate(sources, Path(tmp) / 'B', 'B', model='fixture', tlr=tlr,
                                       feedback_repairs=3, generator=calls)
            self.assertEqual(result['feedback_repair']['stop_reason'], 'proposal_compilation_failed')
            self.assertEqual(result['tlr'], validate_tlr(tlr, sources))
            self.assertEqual(calls.call_count, 1)

    def test_initial_compile_failure_prevents_semantic_reinterpretation(self):
        sources, tlr = fixture()
        calls = Mock(side_effect=AssertionError('No semantic repair for renderer failure'))
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_cli._compile', return_value={'status': 'failed'}):
            result = cli.run_candidate(sources, Path(tmp) / 'B', 'B', model='fixture', tlr=tlr,
                                       feedback_repairs=2, generator=calls)
            self.assertEqual(result['feedback_repair']['stop_reason'], 'initial_compilation_failed')
            calls.assert_not_called()

    def test_unsupported_mode_and_mixed_policies_fail_before_output_creation(self):
        sources, _ = fixture()
        for kwargs in ({'condition': 'BC', 'feedback_repairs': 1}, {'condition': 'A', 'feedback_repairs': 1},
                       {'condition': 'C', 'feedback_repairs': 1, 'abstention_repairs': 1},
                       {'feedback_repairs': True}, {'feedback_repairs': -1}, {'feedback_repairs': 6}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'run'
                with self.assertRaises(ValueError):
                    cli.run_candidate(sources, path, model='fixture', **kwargs)
                self.assertFalse(path.exists())

    def test_cli_explicit_feedback_suppresses_implicit_abstention_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            sources = Path(tmp) / 'sources.json'
            sources.write_text(json.dumps(fixture()[0]))
            with patch('canonical_cli.run_candidate', return_value={'status': 'completed'}) as runner, redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['run', '--statement', str(sources), '--feedback-repairs', '2',
                                          '--output-dir', str(Path(tmp) / 'out')]), 0)
            self.assertEqual(runner.call_args.kwargs['feedback_repairs'], 2)
            self.assertEqual(runner.call_args.kwargs['abstention_repairs'], 0)

    def test_failed_shared_initial_blocks_B_and_C_without_independent_regeneration(self):
        sources, _ = fixture()
        calls = Mock(side_effect=['package Direct {}', '{broken initial'])
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_cli._compile', side_effect=compiled):
            path = Path(tmp) / 'study'
            result = cli.run_study(sources, path, repetitions=1, model='fixture', feedback_repairs=2, generator=calls)
            self.assertEqual(calls.call_count, 2)
            self.assertEqual(result['summary']['initial_generation_failures'], 1)
            for arm in ('B', 'C'):
                self.assertEqual(result['rows'][0][arm]['configuration']['repair_stop_reason'], 'initial_generation_failed')
                self.assertFalse((path / 'rep-001' / arm / 'model.sysml').exists())

    @unittest.skipUnless(shutil.which('z3'), 'Z3 required')
    def test_real_conflict_correction_reaudits_exact_source_and_changed_supported_formula(self):
        sources, tlr = fixture()
        seen = []
        def generate(system, prompt, model, directory, call_id):
            data = json.loads(prompt)
            seen.append(data)
            now = data['current_tlr']
            new = corrected(now)
            return json.dumps(proposal(sources, now, new))
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_cli._compile', side_effect=compiled):
            path = Path(tmp) / 'C'
            result = cli.run_candidate(sources, path, 'C', model='fixture', tlr=tlr,
                                       feedback_repairs=2, generator=generate)
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(seen[0]['solver_feedback']['consistency_status'], 'unsat')
            evidence = next(c for c in seen[0]['solver_feedback']['checks'] if c['name'] == 'consistency')
            self.assertIn('(>= v_voltage_0 30)', evidence['query_smt2'])
            self.assertEqual(evidence['result']['status'], 'unsat')
            self.assertEqual(seen[1]['solver_feedback']['consistency_status'], 'sat')
            self.assertEqual(result['analysis']['consistency_status'], 'sat')
            self.assertEqual(result['feedback_repair']['accepted_repairs'], 1)
            self.assertEqual(result['admission'], 'admitted_consistent_encoding')
            self.assertEqual(cli.read_json(path / 'feedback_attempts/000/analysis.json')['consistency_status'], 'unsat')
            self.assertEqual(cli.read_json(path / 'sources.json'), sources)
            self.assertIn('>= 20', (path / 'model.sysml').read_text())
            witness = next(c for c in seen[1]['solver_feedback']['checks'] if c['name'] == 'violatability')
            self.assertTrue(witness['witness']['query_smt2'])
            self.assertTrue(witness['witness']['values'])

    @unittest.skipUnless(shutil.which('z3'), 'Z3 required')
    def test_correct_source_can_introduce_UNSAT_without_being_rejected_to_seek_SAT(self):
        sources, tlr = fixture()
        sources[1]['text'] = 'Battery voltage shall be at least 30 V.'
        initial = corrected(tlr)
        calls = Mock(return_value=json.dumps(proposal(sources, initial, tlr)))
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_cli._compile', side_effect=compiled):
            result = cli.run_candidate(sources, Path(tmp) / 'C', 'C', model='fixture', tlr=initial,
                                       feedback_repairs=1, generator=calls)
            self.assertEqual(result['analysis']['consistency_status'], 'unsat')
            self.assertEqual(result['feedback_repair']['accepted_repairs'], 1)
            self.assertEqual(result['admission'], 'withheld')
            self.assertEqual(result['tlr'], validate_tlr(tlr, sources))

    @unittest.skipUnless(compiler_capability()['available'] and shutil.which('z3'), 'SysML compiler and Z3 required')
    def test_full_paired_study_real_tools_independent_branches_and_shared_seed(self):
        sources, tlr = fixture()
        calls = []
        def generate(system, prompt, model, directory, call_id):
            data = json.loads(prompt)
            calls.append((call_id, data))
            if system == cli.SYSML_INSTRUCTIONS:
                return render_sysml(validate_tlr(corrected(tlr), sources))
            if system == cli.TLR_INSTRUCTIONS:
                return json.dumps(tlr)
            current = data['current_tlr']
            revised = corrected(current) if data['solver_feedback'] is not None else current
            return json.dumps(proposal(sources, current, revised))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'study'
            result = cli.run_study(sources, path, repetitions=1, model='fixture', feedback_repairs=2, generator=generate)
            row = result['rows'][0]
            self.assertTrue(all(row[a]['compilation']['status'] == 'passed' for a in ('A', 'initial', 'B', 'C')), row)
            self.assertEqual(row['B']['analysis']['status'], 'not_run')
            self.assertFalse((path / 'rep-001/B/audit').exists())
            self.assertEqual(row['C']['analysis']['consistency_status'], 'sat')
            seed = cli.read_json(path / 'rep-001/initial/tlr.json')
            self.assertEqual(cli.read_json(path / 'rep-001/B/feedback_attempts/000/tlr.json'), seed)
            self.assertEqual(cli.read_json(path / 'rep-001/C/feedback_attempts/000/tlr.json'), seed)
            self.assertNotEqual(row['B']['tlr'], row['C']['tlr'])
            config = cli.read_json(path / 'study_configuration.json')
            self.assertFalse(config['shared_BC_candidate'])
            self.assertEqual(config['max_model_transport_invocations'], 6)
            self.assertEqual(config['actual_model_transport_invocations'], 5)
            self.assertEqual([c[0] for c in calls].count('generation'), 2)
            self.assertEqual(result['summary']['by_condition']['B']['repair_attempts'], 1)
            self.assertEqual(result['summary']['by_condition']['C']['repair_attempts'], 2)



if __name__ == '__main__':
    unittest.main()
