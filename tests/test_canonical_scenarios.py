"""Development expectations must not manufacture success from missing semantics."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from canonical_scenarios import (validate_scenario_suite, run_scenarios,
                                 compare_scenario_runs, compare_tlr_semantics)
from canonical_tlr import tlr_context


def var(name):
    return {'var': name}


def number(value):
    return {'value': str(value), 'unit': '1'}


def op(name, *args):
    return {'op': name, 'args': list(args)}


def fixture():
    source = {'id': 'R1', 'text': 'The value shall be at most 28.',
              'source': {'context': [{'id': 'DEF', 'quote': 'The value is an integer.'}]}}
    tlr = {'schema': 'mbse_tlr/1', 'variables': [{'name': 'value', 'type': 'Int', 'unit': '1',
             'description': 'The constrained value.'}], 'assumptions': [], 'requirements': [
                 {**deepcopy(source), 'status': 'supported', 'formula': op('<=', var('value'), number(28))}]}
    suite = {'schema': 'development_scenarios/1', 'role': 'development',
             'origin': {'kind': 'fixture', 'description': 'Synthetic source-grounded unit test.'},
             'context': tlr_context(tlr), 'symbol_meanings': {'value': 'The constrained value.'},
             'scenarios': []}
    for sid, value, expected in [('boundary', 28, 'sat'), ('violation', 29, 'unsat')]:
        suite['scenarios'].append({'id': sid, 'requirement_ids': ['R1'],
            'description': f'The value is {value}.', 'predicate': op('=', var('value'), number(value)),
            'expected': expected, 'rationale': 'Check the source limit and exact endpoint.',
            'source_basis': [{'source_id': 'R1', 'quote': source['text']},
                             {'source_id': 'DEF', 'quote': 'The value is an integer.'}]})
    return [source], tlr, suite


class ScenarioValidationTests(unittest.TestCase):
    def test_literal_target_and_nested_definition_quotes(self):
        sources, tlr, suite = fixture()
        validated = validate_scenario_suite(suite, sources, tlr)
        self.assertEqual(validated['schema'], suite['schema'])
        self.assertEqual(len(validated['scenarios']), 2)
        suite['scenarios'][0]['source_basis'][1]['quote'] = 'Invented meaning.'
        with self.assertRaisesRegex(ValueError, 'not literal'):
            validate_scenario_suite(suite, sources, tlr)

    def test_held_out_role_and_undeclared_origin_are_rejected(self):
        sources, tlr, suite = fixture()
        suite['role'] = 'evaluation'
        with self.assertRaisesRegex(ValueError, 'development role'):
            validate_scenario_suite(suite, sources, tlr)
        suite['role'] = 'development'
        suite['origin']['kind'] = 'automatically_true'
        with self.assertRaisesRegex(ValueError, 'origin'):
            validate_scenario_suite(suite, sources, tlr)

    def test_own_source_quote_cannot_be_replaced_with_context_only(self):
        sources, tlr, suite = fixture()
        suite['scenarios'][0]['source_basis'] = suite['scenarios'][0]['source_basis'][1:]
        with self.assertRaisesRegex(ValueError, 'own literal'):
            validate_scenario_suite(suite, sources, tlr)

    def test_definitions_and_background_cannot_silently_change(self):
        sources, tlr, suite = fixture()
        tlr['variables'][0]['description'] = 'A different physical value.'
        with self.assertRaisesRegex(ValueError, 'meaning'):
            validate_scenario_suite(suite, sources, tlr)
        sources, tlr, suite = fixture()
        tlr['assumptions'] = [{'id': 'A', 'text': 'An invented constraint.',
                              'predicate': op('<=', var('value'), number(28))}]
        with self.assertRaisesRegex(ValueError, 'background'):
            validate_scenario_suite(suite, sources, tlr)

    def test_unknown_targets_and_duplicate_scenarios_are_rejected(self):
        sources, tlr, suite = fixture()
        suite['scenarios'][0]['requirement_ids'] = ['unknown']
        with self.assertRaisesRegex(ValueError, 'source requirement IDs'):
            validate_scenario_suite(suite, sources, tlr)
        sources, tlr, suite = fixture()
        suite['scenarios'].append(deepcopy(suite['scenarios'][0]))
        with self.assertRaisesRegex(ValueError, 'Duplicate scenario'):
            validate_scenario_suite(suite, sources, tlr)

    def test_every_scenario_symbol_needs_an_explicit_meaning(self):
        sources, tlr, suite = fixture()
        suite['symbol_meanings'] = {}
        with self.assertRaisesRegex(ValueError, 'every declared'):
            validate_scenario_suite(suite, sources, tlr)

    def test_expected_unsat_is_not_legal_for_a_constant_false_scenario(self):
        sources, tlr, suite = fixture()
        suite['scenarios'][0]['predicate'] = False
        with self.assertRaisesRegex(ValueError, 'truth constant'):
            validate_scenario_suite(suite, sources, tlr)


@unittest.skipUnless(shutil.which('z3'), 'A local Z3 executable is required.')
class ScenarioSolverTests(unittest.TestCase):
    def run_suite(self, tlr=None, suite=None, sources=None, **kwargs):
        fsources, ftlr, fsuite = fixture()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        result = run_scenarios(tlr or ftlr, suite or fsuite, sources or fsources, directory.name, **kwargs)
        return result, Path(directory.name)

    def test_endpoint_and_violation_have_real_queries_and_static_witness(self):
        result, path = self.run_suite()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['counts']['passed'], 2)
        good, bad = result['scenarios']
        self.assertEqual(good['check']['status'], 'sat')
        self.assertEqual(good['check']['witness']['value'], '28')
        self.assertEqual(bad['check']['status'], 'unsat')
        self.assertEqual(bad['feasibility']['status'], 'sat')
        query = (path / bad['check']['artifacts']['query']).read_text()
        self.assertIn('(<= v_value_0 28)', query)
        self.assertIn('(= v_value_0 29)', query)
        self.assertEqual(bad['check']['query_smt2'], query)
        witness = good['check']['witness_evidence']
        self.assertEqual(witness['query_smt2'], (path / witness['artifacts']['query']).read_text())
        self.assertNotIn('sha256', json.dumps(result))

    def test_strict_boundary_defect_is_failed_and_repair_delta_improves(self):
        sources, tlr, suite = fixture()
        tlr['requirements'][0]['formula']['op'] = '<'
        before, _ = self.run_suite(tlr=tlr)
        after, _ = self.run_suite()
        self.assertEqual(before['scenarios'][0]['status'], 'failed')
        delta = compare_scenario_runs(before, after)
        self.assertTrue(delta['acceptable'])
        self.assertEqual(delta['improvements'], ['boundary'])
        self.assertEqual(delta['regressions'], [])
        reverse = compare_scenario_runs(after, before)
        self.assertFalse(reverse['acceptable'])
        self.assertEqual(reverse['regressions'], ['boundary'])

    def test_unknown_does_not_pass_or_become_a_semantic_defect(self):
        with patch('canonical_scenarios.solver_core._execute', return_value={'status': 'unknown', 'stdout': 'unknown', 'stderr': ''}):
            result, _ = self.run_suite()
        self.assertEqual(result['background_status'], 'unknown')
        self.assertEqual(result['status'], 'inconclusive')
        self.assertEqual(result['counts']['passed'], 0)
        self.assertEqual(result['counts']['failed'], 0)

    def test_candidate_conflict_cannot_make_prohibited_scenarios_pass(self):
        sources, tlr, suite = fixture()
        extra = {'id': 'R2', 'text': 'A conflicting source requirement.'}
        sources.append(extra)
        tlr['requirements'].append({**extra, 'status': 'supported', 'formula': op('>=', var('value'), number(30))})
        result, _ = self.run_suite(tlr=tlr, suite=suite, sources=sources)
        self.assertEqual(result['background_status'], 'sat')
        self.assertEqual(result['consistency_status'], 'unsat')
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['counts']['passed'], 0)
        self.assertEqual(result['scenarios'][1]['status'], 'blocked')
        self.assertNotIn('check', result['scenarios'][1])

    def test_background_conflict_blocks_scoring(self):
        sources, tlr, suite = fixture()
        tlr['assumptions'] = [{'id': 'A', 'text': 'Conflicting background.',
                              'predicate': op('and', op('<', var('value'), number(0)), op('>', var('value'), number(0)))}]
        suite['context'] = tlr_context(tlr)
        result, _ = self.run_suite(tlr=tlr, suite=suite)
        self.assertEqual(result['background_status'], 'unsat')
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['counts']['passed'], 0)

    def test_infeasible_scenario_does_not_earn_unsat_credit(self):
        sources, tlr, suite = fixture()
        suite['scenarios'][1]['predicate'] = op('and', op('=', var('value'), number(28)), op('=', var('value'), number(29)))
        result, _ = self.run_suite(suite=suite)
        self.assertEqual(result['scenarios'][1]['feasibility']['status'], 'unsat')
        self.assertEqual(result['scenarios'][1]['status'], 'blocked')
        self.assertEqual(result['counts']['passed'], 1)

    def test_missing_binding_stays_not_run_until_source_grounded_recovery(self):
        sources, tlr, suite = fixture()
        tlr['variables'] = []
        tlr['requirements'][0].pop('formula')
        tlr['requirements'][0].update(status='unresolved', reason='A typed value is not yet supplied.')
        before, _ = self.run_suite(tlr=tlr)
        self.assertEqual(before['status'], 'incomplete')
        self.assertEqual(before['counts']['not_run'], 2)
        self.assertEqual(before['missing_bindings'], ['value'])
        after, _ = self.run_suite()
        self.assertTrue(compare_scenario_runs(before, after)['acceptable'])
        self.assertEqual(compare_scenario_runs(before, after)['improvements'], ['boundary', 'violation'])

    def test_unrepresented_target_cannot_pass_due_to_another_constraint(self):
        sources, tlr, suite = fixture()
        tlr['requirements'][0].pop('formula')
        tlr['requirements'][0].update(status='unsupported', reason='Temporal meaning is not represented.')
        result, _ = self.run_suite(tlr=tlr)
        self.assertEqual(result['counts']['unsupported'], 2)
        self.assertEqual(result['counts']['passed'], 0)

    def test_scenario_expectations_cannot_be_changed_between_repair_attempts(self):
        before, _ = self.run_suite()
        after = deepcopy(before)
        after['suite']['scenarios'][0]['expected'] = 'unsat'
        with self.assertRaisesRegex(ValueError, 'expectations/context changed'):
            compare_scenario_runs(before, after)

    def test_definition_mismatch_is_actionable_and_not_a_rule_failure(self):
        sources, tlr, suite = fixture()
        tlr['variables'][0]['description'] = 'An unrelated integer.'
        result, _ = self.run_suite(tlr=tlr)
        self.assertEqual(result['status'], 'encoding_error')
        self.assertEqual(result['diagnostics'][0]['code'], 'definition_mismatch')

    def test_definition_failure_retains_suite_and_is_a_rejected_comparison(self):
        before, _ = self.run_suite()
        sources, tlr, suite = fixture()
        tlr['variables'][0]['description'] = 'An unrelated integer.'
        after, _ = self.run_suite(tlr=tlr)
        self.assertEqual(after['suite'], before['suite'])
        delta = compare_scenario_runs(before, after)
        self.assertFalse(delta['acceptable'])
        self.assertEqual(delta['regressions'], ['boundary', 'violation'])

    def test_unrepresented_after_candidate_is_rejected_even_without_previous_passes(self):
        sources, tlr, suite = fixture()
        tlr['variables'] = []
        tlr['requirements'][0].pop('formula')
        tlr['requirements'][0].update(status='unresolved', reason='Value missing.')
        before, _ = self.run_suite(tlr=tlr)
        after, _ = self.run_suite(tlr=tlr)
        delta = compare_scenario_runs(before, after)
        self.assertEqual(delta['regressions'], [])
        self.assertFalse(delta['acceptable'])

    def test_previous_evidence_is_never_overwritten(self):
        result, path = self.run_suite()
        sources, tlr, suite = fixture()
        with self.assertRaisesRegex(ValueError, 'must be empty'):
            run_scenarios(tlr, suite, sources, path)

    def test_semantic_diff_does_not_assume_candidate_or_neighbor_guarantees(self):
        sources, before, suite = fixture()
        after = deepcopy(before)
        after['requirements'][0]['formula'] = op('<=', var('value'), number(30))
        with tempfile.TemporaryDirectory() as directory:
            result = compare_tlr_semantics(before, after, directory)
            row = result['requirements'][0]
            self.assertEqual(row['classification'], 'weakened')
            evidence = row['evidence']['newly_permitted']
            self.assertEqual(evidence['status'], 'sat')
            self.assertGreater(int(evidence['witness']['value']), 28)
            query = (Path(directory) / row['artifacts']['directory'] / evidence['artifacts']['query']).read_text()
            self.assertNotIn('(assert (<= v_value_0 28))', query)
            self.assertNotIn('(assert (<= v_value_0 30))', query)

    def test_new_vocabulary_does_not_get_an_unreviewed_comparison_mapping(self):
        sources, before, suite = fixture()
        after = deepcopy(before)
        after['variables'].append({'name': 'enabled', 'type': 'Bool', 'description': 'Optional input.'})
        after['requirements'][0]['formula'] = op('implies', var('enabled'), after['requirements'][0]['formula'])
        with tempfile.TemporaryDirectory() as directory:
            result = compare_tlr_semantics(before, after, directory)
            self.assertEqual(result['status'], 'unsupported')
            self.assertEqual(result['requirements'][0]['status'], 'unsupported')
            self.assertFalse(list(Path(directory).rglob('*.smt2')))


if __name__ == '__main__':
    unittest.main()
