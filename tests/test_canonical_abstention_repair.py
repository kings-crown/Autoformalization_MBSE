"""Bounded recovery execution, frozen baseline isolation, and actual tool evidence."""
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
from canonical_repair import DIAGNOSIS_INSTRUCTIONS, REPAIR_INSTRUCTIONS, RECOVERY_POLICY_VERSION
from canonical_tlr import validate_tlr, render_sysml, tlr_context
from review_sysml import compiler_capability


def fixture():
    sources = [
        {'id': 'R0', 'text': 'The battery voltage shall be at most 28 V.', 'source': {'document': 'fixture'}},
        {'id': 'R1', 'text': 'The system shall support multicast.', 'source': {'document': 'fixture'}},
        {'id': 'R2', 'text': 'The pump shall stop and remain stopped.', 'source': {'document': 'fixture'}}]
    tlr = {'schema': 'mbse_tlr/1', 'abstraction_policy': 'mbse_abstraction/1',
           'variables': [{'name': 'voltage', 'type': 'Real', 'unit': 'V', 'description': 'Battery voltage.'}],
           'assumptions': [], 'requirements': [
        {'id': 'R0', 'status': 'supported', 'formula': {'op': '<=', 'args': [{'var': 'voltage'}, {'value': '28', 'unit': 'V'}]},
         'abstraction': {'kind': 'state_constraint', 'meaning': 'Voltage is at most 28 V.', 'scope': 'Battery state.', 'limitations': []}},
        {'id': 'R1', 'status': 'unresolved', 'reason_code': 'missing_context', 'reason': 'Implementation architecture is unspecified.'},
        {'id': 'R2', 'status': 'unsupported', 'reason_code': 'profile_limit', 'reason': 'Persistence requires temporal history.'}]}
    return sources, tlr


def recover(tlr):
    proposed = deepcopy(tlr)
    proposed['variables'].append({'name': 'multicast_available', 'type': 'Bool', 'description': 'Availability of multicast support.'})
    proposed['requirements'][1] = {'id': 'R1', 'status': 'supported', 'formula': {'var': 'multicast_available'},
        'abstraction': {'kind': 'capability', 'meaning': 'Multicast support is available.', 'scope': 'Named operation availability.',
                        'limitations': ['No delivery or runtime invocation guarantee.'], 'subject': 'system',
                        'operation': 'multicast', 'symbol': 'multicast_available'}}
    return proposed


def diagnose(sources, tlr, repair_ids=('R1',), rule='capability'):
    table = {r['id']: r for r in sources}
    return {'schema': 'abstention_diagnosis/1', 'requirements': [
        {'id': r['id'], 'decision': 'repair' if r['id'] in repair_ids else 'retain_unsupported',
         'reason': 'Named availability is representable.' if r['id'] in repair_ids else 'Requires unsupported temporal history.',
         'rule': rule if r['id'] in repair_ids else None,
         'source_basis': [{'source_id': r['id'], 'quote': table[r['id']]['text']}] if r['id'] in repair_ids else [],
         'repair_instruction': 'Represent the stated obligation at the permitted abstraction level.' if r['id'] in repair_ids else None}
        for r in tlr['requirements'] if r['status'] != 'supported']}


def proposal(sources, before, after):
    texts = {row['id']: row['text'] for row in sources}
    statuses = {row['id']: row['status'] for row in after['requirements']}
    reviews = []
    for row in before['requirements']:
        if row['status'] == 'supported':
            continue
        recovered = statuses[row['id']] == 'supported'
        reviews.append({'id': row['id'], 'outcome': 'proposed' if recovered else 'retained',
            'considered_rules': {
                'state_constraint': 'Consider the complete source obligation over the current state.',
                'capability': 'Consider whether the source explicitly requires availability of a named operation.',
                'event_relation': 'Consider whether a guarded current-state event relation captures the entire obligation.'},
            'reason': 'The proposal applies a permitted abstraction.' if recovered else 'The complete obligation still requires temporal history.',
            'source_basis': [{'source_id': row['id'], 'quote': texts[row['id']]}],
            'blocking_detail': None if recovered else 'Remaining stopped refers to later states that this profile cannot express.'})
    return {'schema': 'abstention_proposal/1', 'tlr': deepcopy(after), 'reviews': reviews}


def response_mock(*responses):
    return Mock(side_effect=[json.dumps(r) if isinstance(r, dict) else r for r in responses])


class RecoveryExecutionTests(unittest.TestCase):
    def run_fixture(self, tmp, responses, budget=2, condition='B', tlr=None, **kwargs):
        sources, initial = fixture()
        generator = response_mock(*responses)
        result = cli.run_candidate(sources, Path(tmp) / 'run', condition, model='offline-test-model',
            tlr=tlr, compile_model=False, generator=generator, abstention_repairs=budget, **kwargs)
        return result, generator, Path(tmp) / 'run'

    def test_recovers_capability_then_stops_at_genuine_temporal_limit_with_no_Z3_in_B(self):
        sources, tlr = fixture()
        proposed = recover(tlr)
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_audits.audit_tlr', side_effect=AssertionError('B cannot use Z3')):
            result, generator, directory = self.run_fixture(tmp, [tlr, diagnose(sources, tlr), proposal(sources, tlr, proposed),
                diagnose(sources, proposed, ()), proposal(sources, proposed, proposed)])
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(result['repair']['recovered_ids'], ['R1'])
            self.assertEqual(result['repair']['retained_ids'], ['R2'])
            self.assertEqual(result['repair']['stop_reason'], 'no_progress_after_proposal')
            self.assertEqual(result['repair']['repair_attempts'], 2)
            self.assertEqual(result['repair']['diagnosis_calls'], 2)
            self.assertEqual(result['configuration']['semantic_repairs'], 1)
            self.assertEqual(result['tlr'], validate_tlr(proposed, sources))
            self.assertEqual(cli.read_json(directory / 'sources.json'), sources)
            self.assertEqual(cli.read_json(directory / 'attempts/000/tlr.json'), validate_tlr(tlr, sources))
            self.assertEqual((directory / 'model.sysml').read_bytes(), (directory / 'attempts/001/model.sysml').read_bytes())
            self.assertEqual(cli.read_json(directory / 'candidate_tlr.json'), proposed)
            self.assertEqual(json.loads((directory / 'generation_response.txt').read_text()), tlr)
            self.assertEqual(generator.call_count, 5)
            self.assertEqual([c.args[4] for c in generator.call_args_list], ['generation', 'diagnosis_call', 'repair_call', 'diagnosis_call', 'repair_call'])
            self.assertTrue(generator.call_args_list[2].args[0].startswith(REPAIR_INSTRUCTIONS))
            self.assertEqual(result['repair']['policy'], RECOVERY_POLICY_VERSION)
            self.assertEqual(cli.read_json(directory / 'attempts/001/proposal_reviews.json'), proposal(sources, tlr, proposed)['reviews'])
            for call in generator.call_args_list[1:]:
                self.assertEqual(call.args[2], 'offline-test-model')
                prompt = json.loads(call.args[1])
                self.assertEqual(prompt['source_packet'], sources)
                for key in ('judge', 'assertions', 'mutation_results', 'analysis', 'solver_feedback', 'condition'):
                    self.assertNotIn(key, prompt)

    def test_no_progress_stops_early(self):
        sources, tlr = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            result, generator, directory = self.run_fixture(tmp, [tlr, diagnose(sources, tlr), proposal(sources, tlr, tlr)], budget=5)
            self.assertEqual(result['repair']['stop_reason'], 'no_progress_after_proposal')
            self.assertEqual(generator.call_count, 3)
            self.assertEqual(result['configuration']['semantic_repairs'], 0)
            self.assertFalse((directory / 'attempts/001/model.sysml').exists())
            self.assertEqual(result['tlr'], validate_tlr(tlr, sources))

    def test_invalid_proposals_consume_global_budget_and_local_error_is_reused(self):
        sources, tlr = fixture()
        invalid = recover(tlr)
        invalid['requirements'][0]['formula']['args'][1]['value'] = '30'
        with tempfile.TemporaryDirectory() as tmp:
            result, generator, directory = self.run_fixture(tmp, [tlr, diagnose(sources, tlr), proposal(sources, tlr, invalid),
                diagnose(sources, tlr), '{invalid'])
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(result['repair']['stop_reason'], 'budget_exhausted')
            self.assertEqual(result['repair']['repair_attempts'], 2)
            self.assertEqual(result['repair']['accepted_repairs'], 0)
            self.assertEqual(generator.call_count, 5)
            self.assertIn('supported records are frozen', result['repair']['steps'][0]['error'])
            self.assertIn('previous_failure', json.loads(generator.call_args_list[3].args[1]))
            self.assertEqual(result['tlr'], validate_tlr(tlr, sources))
            self.assertEqual((directory / 'attempts/002/repair_response.txt').read_text(), '{invalid')

    def test_oversized_validation_error_does_not_prevent_remaining_calls(self):
        sources, tlr = fixture()
        invalid = recover(tlr)
        invalid['variables'][0]['x' * 13000] = 'unexpected'
        with tempfile.TemporaryDirectory() as tmp:
            result, generator, directory = self.run_fixture(tmp,
                [tlr, diagnose(sources, tlr), proposal(sources, tlr, invalid),
                 diagnose(sources, tlr, ()), proposal(sources, tlr, recover(tlr))])
            self.assertEqual(generator.call_count, 5)
            self.assertEqual(result['repair']['accepted_repairs'], 1, result)
            error = cli.read_json(directory / 'attempts/001/attempt.json')['error']
            self.assertGreater(len(error), 12000)
            for call in generator.call_args_list[3:]:
                feedback = json.loads(call.args[1])['previous_failure']
                self.assertLessEqual(len(feedback), 12000)
                self.assertIn('truncated', feedback)

    def test_later_failed_repair_keeps_first_accepted_candidate(self):
        sources, tlr = fixture()
        # Test controller selection on a second repair proposal; the diagnosis is
        # intentionally a model mistake, illustrating why recovery is not proof.
        proposed = recover(tlr)
        with tempfile.TemporaryDirectory() as tmp:
            result, generator, directory = self.run_fixture(tmp, [tlr, diagnose(sources, tlr), proposal(sources, tlr, proposed),
                diagnose(sources, proposed, ('R2',)), '{invalid'])
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(result['repair']['accepted_repairs'], 1)
            self.assertEqual(result['repair']['selected_attempt'], 'attempts/001')
            self.assertEqual(result['tlr'], validate_tlr(proposed, sources))
            self.assertEqual((directory / 'model.sysml').read_text(), render_sysml(result['tlr']))

    def test_invalid_diagnosis_is_recorded_but_does_not_veto_clean_proposal(self):
        sources, tlr = fixture()
        invalid = diagnose(sources, tlr)
        invalid['requirements'][0]['source_basis'][0]['quote'] = 'Invented permission'
        for response in (invalid, RuntimeError('transport failed'), '{invalid'):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as tmp:
                result, generator, directory = self.run_fixture(tmp,
                    [tlr, response, proposal(sources, tlr, recover(tlr))], budget=1)
                self.assertEqual(result['status'], 'completed', result)
                self.assertEqual(result['repair']['stop_reason'], 'budget_exhausted')
                self.assertEqual(result['repair']['diagnosis_calls'], 1)
                self.assertEqual(result['repair']['repair_attempts'], 1)
                self.assertEqual(result['repair']['accepted_repairs'], 1)
                self.assertEqual(generator.call_count, 3)
                self.assertIsNone(json.loads(generator.call_args_list[2].args[1])['diagnosis'])
                step = cli.read_json(directory / 'attempts/001/attempt.json')
                self.assertEqual(step['diagnosis_status'], 'failed')
                self.assertIn('diagnosis_error', step)
                self.assertEqual(step['proposal_status'], 'accepted')
                self.assertEqual(result['repair']['overridden_diagnosis_ids'], [])

    def test_retention_clarification_and_different_rule_advice_cannot_veto_recovery(self):
        sources, tlr = fixture()
        for decision in ('retain_unsupported', 'needs_clarification', 'repair'):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as tmp:
                advice = diagnose(sources, tlr, (), rule='state_constraint')
                for row in advice['requirements']:
                    row['decision'] = decision
                    if decision == 'repair':
                        row.update(rule='state_constraint', repair_instruction='Consider a state-level constraint.',
                                   source_basis=[{'source_id': row['id'], 'quote': next(s['text'] for s in sources if s['id'] == row['id'])}])
                result, generator, _ = self.run_fixture(tmp,
                    [tlr, advice, proposal(sources, tlr, recover(tlr))], budget=1)
                self.assertEqual(result['repair']['accepted_repairs'], 1, result)
                self.assertEqual(result['repair']['recovered_ids'], ['R1'])
                self.assertEqual(result['repair']['overridden_diagnosis_ids'], [] if decision == 'repair' else ['R1'])
                self.assertEqual(result['repair']['steps'][0]['eligible_ids'], ['R1', 'R2'])
                self.assertEqual(generator.call_count, 3)

    def test_missing_or_misleading_proposal_reviews_consume_budget_without_acceptance(self):
        sources, tlr = fixture()
        valid = proposal(sources, tlr, recover(tlr))
        wrong_status = deepcopy(valid)
        wrong_status['reviews'][0].update(outcome='retained', blocking_detail='Missing details change interpretation.')
        missing_review = deepcopy(valid)
        missing_review['reviews'].pop()
        for invalid in (recover(tlr), missing_review, wrong_status):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as tmp:
                result, generator, _ = self.run_fixture(tmp,
                    [tlr, diagnose(sources, tlr, ()), invalid], budget=1)
                self.assertEqual(result['repair']['repair_attempts'], 1)
                self.assertEqual(result['repair']['accepted_repairs'], 0)
                self.assertEqual(result['repair']['stop_reason'], 'budget_exhausted')
                self.assertEqual(result['tlr'], validate_tlr(tlr, sources))
                self.assertEqual(generator.call_count, 3)

    def test_transport_failure_counts_as_a_repair_attempt(self):
        sources, tlr = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            result, generator, _ = self.run_fixture(tmp, [tlr, diagnose(sources, tlr), RuntimeError('transport failed')], budget=1)
            self.assertEqual(result['repair']['repair_attempts'], 1)
            self.assertEqual(result['repair']['stop_reason'], 'budget_exhausted')
            self.assertEqual(result['status'], 'completed')

    def test_repair_cannot_hide_source_edits_in_normalization(self):
        sources, tlr = fixture()
        for field, value in (('text', 'The system may support multicast.'), ('source', {'document': 'invented'})):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                proposed = recover(tlr)
                proposed['requirements'][1][field] = value
                result, _, _ = self.run_fixture(tmp, [tlr, diagnose(sources, tlr), proposal(sources, tlr, proposed)], budget=1)
                self.assertEqual(result['repair']['accepted_repairs'], 0)
                self.assertIn('differs from prepared source', result['repair']['steps'][0]['error'])

    def test_fixed_context_prevents_adding_recovery_symbols(self):
        sources, tlr = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = self.run_fixture(tmp, [tlr, diagnose(sources, tlr), proposal(sources, tlr, recover(tlr))], budget=1, context=tlr_context(tlr))
            self.assertEqual(result['repair']['accepted_repairs'], 0)
            self.assertIn('fixed study context', result['repair']['steps'][0]['error'])

    def test_compiler_rejection_stops_and_retains_initial_candidate(self):
        sources, tlr = fixture()
        generator = response_mock(tlr, diagnose(sources, tlr), proposal(sources, tlr, recover(tlr)))
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_cli._compile', side_effect=[{'status': 'passed'}, {'status': 'failed'}]):
            result = cli.run_candidate(sources, Path(tmp) / 'run', 'B', generator=generator, abstention_repairs=2)
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(result['compilation']['status'], 'passed')
            self.assertEqual(result['repair']['stop_reason'], 'proposal_compilation_failed')
            self.assertEqual(result['repair']['accepted_repairs'], 0)
            self.assertEqual(result['tlr'], validate_tlr(tlr, sources))
            self.assertEqual(generator.call_count, 3)

    def test_tool_exception_is_not_fed_back_for_semantic_repair(self):
        sources, tlr = fixture()
        generator = response_mock(tlr, diagnose(sources, tlr), proposal(sources, tlr, recover(tlr)))
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_audits.audit_tlr',
            side_effect=[{'status': 'passed', 'admitted': False}, RuntimeError('audit infrastructure failure')]):
            result = cli.run_candidate(sources, Path(tmp) / 'run', 'BC', generator=generator,
                                       compile_model=False, abstention_repairs=2)
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(result['repair']['stop_reason'], 'candidate_check_failed')
            self.assertEqual(result['repair']['accepted_repairs'], 0)
            self.assertEqual(result['tlr'], validate_tlr(tlr, sources))
            self.assertEqual(generator.call_count, 3)

    def test_initial_compilation_failure_is_a_separate_repair_stop(self):
        sources, tlr = fixture()
        generator = response_mock(tlr)
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_cli._compile', return_value={'status': 'failed'}):
            result = cli.run_candidate(sources, Path(tmp) / 'run', 'B', generator=generator, abstention_repairs=2)
            self.assertEqual(result['repair']['stop_reason'], 'initial_compilation_failed')
            self.assertEqual(result['repair']['diagnosis_calls'], 0)
            generator.assert_called_once()

    def test_all_abstained_empty_vocabulary_can_recover_without_a_dummy_symbol(self):
        sources, tlr = fixture()
        sources, tlr['requirements'], tlr['variables'] = sources[1:], tlr['requirements'][1:], []
        proposed = recover(fixture()[1])
        proposed['requirements'] = proposed['requirements'][1:]
        proposed['variables'] = proposed['variables'][1:]
        generator = response_mock(tlr, diagnose(sources, tlr), proposal(sources, tlr, proposed), diagnose(sources, proposed, ()), proposal(sources, proposed, proposed))
        with tempfile.TemporaryDirectory() as tmp:
            result = cli.run_candidate(sources, Path(tmp) / 'run', 'B', generator=generator,
                                       compile_model=False, abstention_repairs=2)
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(result['repair']['recovered_ids'], ['R1'])
            self.assertEqual([v['name'] for v in result['tlr']['variables']], ['multicast_available'])

    def test_already_supported_TLR_does_not_spend_recovery_budget(self):
        sources, tlr = fixture()
        sources, tlr['requirements'] = sources[:1], tlr['requirements'][:1]
        generator = response_mock(tlr)
        with tempfile.TemporaryDirectory() as tmp:
            result = cli.run_candidate(sources, Path(tmp) / 'run', 'B', generator=generator,
                                       compile_model=False, abstention_repairs=2)
            self.assertEqual(result['repair']['stop_reason'], 'no_abstentions')
            self.assertEqual(result['repair']['diagnosis_calls'], 0)
            generator.assert_called_once()

    def test_zero_budget_supplied_fixtures_and_legacy_recovery_are_call_free(self):
        sources, tlr = fixture()
        for budget, candidate, reason in ((0, tlr, 'disabled_by_policy'), (2,
            {'schema': 'mbse_tlr/1', 'variables': [], 'requirements': [
                {'id': s['id'], 'status': 'unsupported', 'reason': 'Legacy fixture'} for s in sources]},
            'legacy_profile_requires_explicit_conversion')):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as tmp:
                result, generator, _ = self.run_fixture(tmp, [], budget=budget, tlr=candidate)
                self.assertEqual(result['status'], 'completed', result)
                self.assertEqual(result['repair']['stop_reason'], reason)
                generator.assert_not_called()

    def test_study_recovery_is_explicit_and_BC_share_the_selected_model(self):
        sources, tlr = fixture()
        for budget, expected_calls in ((0, 2), (1, 4)):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as tmp:
                generator = response_mock('package Direct {}', tlr, diagnose(sources, tlr), proposal(sources, tlr, recover(tlr)))
                with patch('canonical_audits.audit_tlr', return_value={'status': 'passed', 'admitted': False}):
                    result = cli.run_study(sources, Path(tmp) / 'study', repetitions=1, generator=generator,
                        abstention_repairs=budget, compile_model=False)
                self.assertEqual(generator.call_count, expected_calls)
                row = result['rows'][0]
                self.assertEqual(row['A']['configuration']['abstention_repair_budget'], 0)
                self.assertEqual(row['BC']['arm_views']['B']['model_file'], row['BC']['arm_views']['C']['model_file'])
                self.assertEqual(row['BC']['configuration']['semantic_repairs'], budget)

    def test_budget_validation_precedes_output_creation(self):
        sources, tlr = fixture()
        for budget in (-1, 6, True, 1.5):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ValueError):
                    cli.run_candidate(sources, Path(tmp) / 'not-created', tlr=tlr, abstention_repairs=budget)
                self.assertFalse((Path(tmp) / 'not-created').exists())

    def test_CLI_defaults_generated_to_two_but_study_and_supplied_to_zero(self):
        sources, tlr = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            source_file, tlr_file = Path(tmp) / 'sources.json', Path(tmp) / 'tlr.json'
            cli.write_json(source_file, sources)
            cli.write_json(tlr_file, tlr)
            for command, extra, expected in (
                ('run', [], 2), ('run', ['--condition', 'B'], 2), ('run', ['--condition', 'A'], 0),
                ('run', ['--abstention-repairs', '0'], 0), ('run', ['--tlr-file', str(tlr_file)], 0),
                ('study', [], 0), ('study', ['--abstention-repairs', '2'], 2)):
                with self.subTest(command=command, extra=extra), redirect_stdout(io.StringIO()), \
                    patch('canonical_cli.run_candidate', return_value={'status': 'completed'}) as run, \
                    patch('canonical_cli.run_study', return_value={'status': 'completed'}) as study:
                    self.assertEqual(cli.main([command, '--statement', str(source_file)] + extra), 0)
                    callback = study if command == 'study' else run
                    self.assertEqual(callback.call_args.kwargs['abstention_repairs'], expected)

    @unittest.skipUnless(compiler_capability()['available'] and shutil.which('z3'), 'Real compiler and Z3 required')
    def test_recovered_contradiction_is_retained_with_real_compiler_and_solver(self):
        sources, tlr = fixture()
        sources[1]['text'] = 'The battery voltage shall be at least 29 V.'
        sources, tlr['requirements'] = sources[:2], tlr['requirements'][:2]
        proposed = deepcopy(tlr)
        proposed['requirements'][1] = {'id': 'R1', 'status': 'supported',
            'formula': {'op': '>=', 'args': [{'var': 'voltage'}, {'value': '29', 'unit': 'V'}]},
            'abstraction': {'kind': 'state_constraint', 'meaning': 'Battery voltage is at least 29 V.',
                            'scope': 'Battery state.', 'limitations': []}}
        generator = response_mock(tlr, diagnose(sources, tlr, rule='state_constraint'), proposal(sources, tlr, proposed))
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'run'
            result = cli.run_candidate(sources, directory, 'BC', generator=generator, abstention_repairs=2)
            self.assertEqual(result['status'], 'completed', result)
            self.assertEqual(result['repair']['stop_reason'], 'all_supported', result['repair'])
            self.assertEqual(result['compilation']['status'], 'passed')
            self.assertEqual(result['analysis']['background_status'], 'sat')
            self.assertEqual(result['analysis']['consistency_status'], 'unsat')
            self.assertEqual(result['admission'], 'withheld')
            self.assertEqual(result['repair']['accepted_repairs'], 1)
            self.assertEqual(cli.read_json(directory / 'attempts/000/analysis.json')['consistency_status'], 'sat')
            self.assertEqual((directory / 'audit/consistency.smt2').read_bytes(),
                             (directory / 'attempts/001/audit/consistency.smt2').read_bytes())
            self.assertEqual((directory / 'model.sysml').read_text(), render_sysml(result['tlr']))
            self.assertEqual(cli.read_json(directory / 'audit/tlr.json'), result['tlr'])
            self.assertEqual(generator.call_count, 3)


if __name__ == '__main__':
    unittest.main()
