"""Evidence-profile regressions: no proxy becomes semantic correctness."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from review_quality import assess_formalization
from review_behavior import validate_behavior
from review_candidate import inspect_candidate
from review_profile import interpret, parse_requirements


def local_run():
    sources = parse_requirements('R1: The battery shall have a voltage of at most 28 V.', 'text', 'Fixture')
    return {'schema': 'fixture', 'engine': 'local', 'analysis_mode': 'requirements', 'review_workflow_version': 2,
            'requirements': sources, 'source_hash': 'source-hash', 'input_file': 'source.txt',
            'tlr': interpret(sources), 'analysis': {'solver_status': 'sat', 'status': 'sat', 'checked_ids': ['R1']},
            'compilation': {'status': 'passed', 'model_sha256': 'model-hash'}, 'model': {'text': 'package Fixture {}'},
            'integrity': {'status': 'passed', 'issues': []},
            'assumptions': [{'id': 'A1'}, {'id': 'A1'}],
            'contracts': {'contracts': [{'id': 'C1', 'scalar': {'symbol': 'voltage'}}]},
            'artifacts': [{'name': name} for name in ('source.txt', 'requirements.json', 'interpretation.json', 'constraints.smt2', 'solver_result.json', 'analysis.json', 'model.sysml', 'compilation.json')]}


def dims(run):
    return {d['id']: d for d in assess_formalization(run)['dimensions']}


def add_behavior(run):
    candidate = {'schema': 'review_behavior/1', 'horizon': 2, 'step': {'value': '1', 'unit': 's'},
                 'variables': [{'name': 'voltage', 'type': 'Real', 'role': 'state', 'unit': 'V'}],
                 'initial': [], 'transitions': [], 'assumptions': [],
                 'properties': [{'id': 'P1', 'kind': 'always', 'requirement_ids': ['R1'],
                                 'predicate': {'op': '<=', 'args': [{'var': 'voltage'}, {'value': '28', 'unit': 'V'}]}}]}
    run['behavior'] = validate_behavior(candidate, ['R1'])
    run['analysis_mode'] = 'check_design'
    run['candidate_inspection'] = inspect_candidate(run['behavior'], run['requirements'])
    run['behavioral_analysis'] = {'status': 'bounded_pass', 'model_feasibility': {'verdict': 'sat'},
                                  'scope': {'horizon': 2, 'step': {'value': '1', 'unit': 's'}},
                                  'checks': [{'id': 'P1', 'verdict': 'bounded_pass', 'requirement_ids': ['R1']}]}
    return run


class QualityTests(unittest.TestCase):
    def test_empty_and_failed_runs_never_report_coverage_or_missing_checks_as_pass(self):
        result = assess_formalization({})
        d = {row['id']: row for row in result['dimensions']}
        self.assertEqual(d['source_coverage']['status'], 'unavailable')
        self.assertEqual(d['interpretation_coverage']['counts']['requirements'], 0)
        self.assertEqual(d['consistency']['status'], 'not_run')
        self.assertEqual(d['behavior']['status'], 'not_run')
        self.assertEqual(d['compilation']['status'], 'not_run')
        self.assertEqual(d['integrity']['status'], 'not_run')
        self.assertNotIn('score', result)
        self.assertNotIn('confidence', result)
        run = local_run()
        run.update(status='failed', analysis={}, compilation={'status': 'failed'}, integrity={'status': 'failed', 'issues': ['artifact changed']})
        d = dims(run)
        self.assertEqual(d['consistency']['status'], 'not_run')
        self.assertEqual(d['compilation']['status'], 'failed')
        self.assertEqual(d['integrity']['details']['issues'], ['artifact changed'])

    def test_supported_local_source_has_separate_consistency_compilation_and_meaning(self):
        d = dims(local_run())
        self.assertEqual(d['source_coverage']['status'], 'preserved')
        self.assertEqual(d['interpretation_coverage']['counts']['supported'], 1)
        self.assertEqual(d['types_units']['status'], 'recorded_structure')
        self.assertIsNone(d['types_units']['details']['native_typecheck'])
        self.assertEqual(d['consistency']['status'], 'sat')
        self.assertEqual(d['semantic_probes']['status'], 'not_run')
        self.assertEqual(d['compilation']['status'], 'passed')
        self.assertEqual(d['review_obligations']['details']['source_fidelity'], 'unestablished')
        self.assertEqual(d['review_obligations']['details']['architecture_behavior_equivalence'], 'unestablished')

    def test_coverage_partitions_known_ids_and_never_counts_duplicate_candidates_twice(self):
        run = local_run()
        run['requirements'] += [{'id': rid, 'text': rid} for rid in ('R2', 'R3', 'R4', 'R5')]
        run['tlr']['requirements'] += [{'id': 'R2', 'status': 'pending_review'}, {'id': 'R3', 'status': 'unsupported'},
                                       {'id': 'R4', 'status': 'supported'}, {'id': 'R4', 'status': 'supported'},
                                       {'id': 'EXTRA', 'status': 'supported'}]
        d = dims(run)['interpretation_coverage']
        self.assertEqual(d['requirement_ids']['supported'], ['R1'])
        self.assertEqual(d['requirement_ids']['pending_review'], ['R2'])
        self.assertEqual(d['requirement_ids']['needs_interpretation'], ['R3'])
        self.assertEqual(d['requirement_ids']['ambiguous'], ['R4'])
        self.assertEqual(d['requirement_ids']['missing'], ['R5'])
        self.assertEqual(d['requirement_ids']['unexpected_ids'], ['EXTRA'])
        self.assertEqual(sum(d['counts'][key] for key in ('supported', 'pending_review', 'needs_interpretation', 'missing', 'ambiguous')), 5)

    def test_duplicate_sources_are_explicitly_incomplete(self):
        run = local_run()
        run['requirements'].append(copy.deepcopy(run['requirements'][0]))
        d = dims(run)
        self.assertEqual(d['source_coverage']['counts']['requirements'], 1)
        self.assertEqual(d['source_coverage']['status'], 'incomplete')
        self.assertEqual(d['source_coverage']['requirement_ids']['duplicate_ids'], ['R1'])
        self.assertEqual(d['interpretation_coverage']['requirement_ids']['ambiguous'], ['R1'])

    def test_llm_supported_labels_never_become_verified_source_interpretations(self):
        run = local_run()
        run['engine'] = 'pipeline'
        d = dims(run)['interpretation_coverage']
        self.assertEqual(d['counts']['supported'], 0)
        self.assertEqual(d['counts']['pending_review'], 1)
        self.assertEqual(d['status'], 'pending_review')

    def test_actual_sat_is_separate_from_failed_or_skipped_semantic_probes(self):
        run = local_run()
        run['engine'] = 'pipeline'
        run['analysis']['pipeline_semantic_checks'] = {'passed': False, 'checks': {'vacuity': {'passed': False, 'solver_errors': [{'error': 'probe failed'}]}}}
        d = dims(run)
        self.assertEqual(d['consistency']['status'], 'sat')
        self.assertEqual(d['semantic_probes']['status'], 'failed')
        run['analysis']['pipeline_semantic_checks'] = {'passed': True, 'checks': {'vacuity': {'passed': True, 'skipped_administrative_guards': ['R1']}}}
        self.assertEqual(dims(run)['semantic_probes']['status'], 'partial')
        run['analysis']['pipeline_semantic_checks'] = {'passed': True, 'checks': {'symbol_drift': {'passed': False}}}
        self.assertEqual(dims(run)['semantic_probes']['status'], 'findings')

    def test_consistency_never_infers_sat_from_stage_or_diagnostic_unsat_artifact(self):
        run = local_run()
        run['analysis'] = {'status': 'sat'}
        run['artifacts'].append({'name': 'pipeline_model_unsat.smt2'})
        run['stages'] = [{'id': 'analysis', 'status': 'passed'}]
        d = dims(run)['consistency']
        self.assertEqual(d['status'], 'unknown')
        self.assertNotIn('pipeline_model_unsat.smt2', d['evidence'])
        run['analysis'] = {'status': 'partial', 'solver_status': 'sat', 'checked_ids': ['R1', 'R1', 'missing']}
        self.assertEqual(dims(run)['consistency']['counts']['associated_source_ids'], 1)
        run['analysis'] = {'status': 'unsat', 'solver_status': 'unsat', 'unsat_core': ['R1']}
        self.assertEqual(dims(run)['consistency']['status'], 'unsat')

    def test_recorded_typecheck_failure_is_not_masked_by_candidate_validation(self):
        run = add_behavior(local_run())
        run['native_typecheck'] = {'ok': False, 'errors': [{'code': 'BAD_UNIT'}]}
        self.assertEqual(dims(run)['types_units']['status'], 'failed')
        run['native_typecheck'] = {'ok': True, 'errors': []}
        self.assertEqual(dims(run)['types_units']['status'], 'validated_scope')

    def test_skipped_native_typecheck_is_not_reported_as_validated(self):
        run = local_run()
        run['native_typecheck'] = {'ok': True, 'skipped': True, 'errors': []}
        self.assertEqual(dims(run)['types_units']['status'], 'recorded_structure')
        self.assertFalse(dims(run)['types_units']['details']['native_typecheck_was_run'])
        add_behavior(run)
        self.assertEqual(dims(run)['types_units']['status'], 'validated_scope')
        self.assertTrue(dims(run)['types_units']['details']['candidate_inspection_matches'])
        self.assertFalse(dims(run)['types_units']['details']['native_typecheck_was_run'])

    def test_behavior_feasibility_is_required_for_bounded_pass(self):
        run = add_behavior(local_run())
        self.assertEqual(dims(run)['behavior']['status'], 'bounded_pass')
        run['behavioral_analysis']['model_feasibility']['verdict'] = 'unsat'
        self.assertEqual(dims(run)['behavior']['status'], 'infeasible')
        run['behavioral_analysis']['model_feasibility']['verdict'] = 'unknown'
        self.assertEqual(dims(run)['behavior']['status'], 'unknown')
        run['behavioral_analysis']['model_feasibility']['verdict'] = 'sat'
        run['behavioral_analysis']['checks'] = []
        self.assertEqual(dims(run)['behavior']['status'], 'partial')
        self.assertEqual(dims(run)['behavior']['details']['missing_property_results'], ['P1'])

    def test_proposals_liveness_and_counterexamples_remain_distinct(self):
        run = add_behavior(local_run())
        for verdict, expected in [('unproved', 'unproved'), ('counterexample', 'counterexample'), ('vacuous', 'partial'), ('unknown', 'partial')]:
            run['behavioral_analysis']['checks'][0]['verdict'] = verdict
            with self.subTest(verdict=verdict):
                self.assertEqual(dims(run)['behavior']['status'], expected)
        run['behavior_proposal'] = {'candidate': run.pop('behavior')}
        run['analysis_mode'] = 'propose_design'
        # Even malformed leftover solver labels cannot turn an unchecked proposal into a design pass.
        run['behavioral_analysis']['checks'][0]['verdict'] = 'bounded_pass'
        self.assertEqual(dims(run)['behavior']['status'], 'pending_review')

    def test_diagnostics_provenance_and_hash_mismatch_remain_visible(self):
        run = add_behavior(local_run())
        d = dims(run)
        self.assertEqual(d['provenance']['status'], 'findings')
        self.assertEqual(d['provenance']['counts']['diagnostics'], 2)
        self.assertEqual(d['provenance']['counts']['origins'], {'unspecified': 2})
        run['behavior']['horizon'] = 3
        d = dims(run)
        self.assertEqual(d['provenance']['status'], 'stale')
        self.assertEqual(d['types_units']['status'], 'failed')

    def test_quality_is_immutable_with_respect_to_live_review_decisions(self):
        run = add_behavior(local_run())
        before = assess_formalization(run)
        snapshot = copy.deepcopy(run)
        self.assertEqual(run, snapshot)
        run.update(assumption_reviews=[{'id': 'ar1', 'decision': 'accept'}], contract_reviews=[{'id': 'cr1', 'decision': 'accept'}],
                   architecture_binding_reviews=[{'id': 'br1', 'decision': 'accept'}], reviews=[{'decision': 'approve'}],
                   baseline={'status': 'approved'})
        self.assertEqual(assess_formalization(run), before)
        obligations = next(d for d in before['dimensions'] if d['id'] == 'review_obligations')
        self.assertEqual(obligations['counts']['assumptions'], 1)
        self.assertEqual(obligations['counts']['required_variable_bindings'], 1)
        self.assertEqual(obligations['status'], 'pending_at_generation')

    def test_evidence_links_only_name_existing_artifacts_and_summary_is_json_serializable(self):
        run = local_run()
        before = copy.deepcopy(run)
        result = assess_formalization(run)
        json.dumps(result)
        self.assertEqual(run, before)
        names = {a['name'] for a in run['artifacts']}
        self.assertTrue(all(set(d['evidence']) <= names for d in result['dimensions']))
        self.assertEqual(result['schema'], 'review_quality/1')
        run['compilation']['trace_compilation'] = {'status': 'failed'}
        self.assertEqual(dims(run)['compilation']['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
