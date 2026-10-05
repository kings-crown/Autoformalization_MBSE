"""Contract source integrity and finite-projection boundary regressions."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from review_contracts import build_contract_bundle, behavior_from_contracts, bind_contract_evidence
from review_contract_sysml import render_contract_sysml
from review_profile import interpret
from review_behavior import analyze_behavior
from review_sysml import compile_sysml, compiler_capability


def v(name, nxt=False):
    return {'var': name, **({'at': 'next'} if nxt else {})}


def c(value, unit='1'):
    return {'value': str(value), 'unit': unit}


def op(name, *args):
    return {'op': name, 'args': list(args)}


def sources():
    return [{'id': 'R1', 'text': 'The bus shall have a voltage of at most 28 V.',
             'source': {'document': 'fixture.csv', 'location': 'row 2'}, 'authority': 'system specification'},
            {'id': 'R2', 'text': 'Response follows the command.', 'source': {'document': 'fixture.csv', 'location': 'row 3'}}]


def proposal(value='29'):
    return {'schema': 'review_behavior/1', 'horizon': 3, 'step': {'value': '1', 'unit': 's'},
            'variables': [{'name': 'voltage', 'type': 'Real', 'role': 'output', 'unit': 'V'}],
            'initial': [op('=', v('voltage'), c(value, 'V'))],
            'transitions': [op('=', v('voltage', True), v('voltage'))],
            'assumptions': [], 'properties': [{'id': 'VoltageLimit', 'kind': 'always',
                'requirement_ids': ['R1'], 'predicate': op('<=', v('voltage'), c('28', 'V'))}]}


class ContractBundleTests(unittest.TestCase):
    def test_preserves_sources_and_records_rule_mapping_without_claiming_llm_provenance(self):
        reqs = sources()
        bundle = build_contract_bundle(reqs, 'sourcehash', proposal())
        row = bundle['contracts'][0]
        self.assertEqual(row['source_refs'][0]['text'], reqs[0]['text'])
        self.assertEqual(row['source_refs'][0]['source'], reqs[0]['source'])
        self.assertEqual(row['rule']['basis'], 'deterministic_property_pattern')
        self.assertEqual(row['owner']['status'], 'unallocated')
        self.assertEqual(bundle['contracts'][1]['status'], 'needs_interpretation')
        self.assertIsNone(bundle['contracts'][1]['property'])
        self.assertEqual(behavior_from_contracts(bundle), bundle['behavior'])
        reqs[0]['text'] = 'mutated caller data'
        self.assertNotEqual(bundle['requirements'][0]['text'], reqs[0]['text'])

    def test_source_rule_property_and_context_tampering_are_rejected(self):
        base = build_contract_bundle(sources(), 'sourcehash', proposal())
        for change in (
            lambda b: b['contracts'][0]['source_refs'][0].update(text='new source'),
            lambda b: b['contracts'][0]['property']['predicate']['args'][1].update(value='29'),
            lambda b: b['contracts'][0]['rule'].update(version='99'),
            lambda b: b['behavior']['initial'].clear(),
            lambda b: b.update(context_sha256='wrong'),
            lambda b: b['contracts'].pop(),
        ):
            item = deepcopy(base)
            change(item)
            with self.assertRaises(ValueError):
                behavior_from_contracts(item)

    def test_saved_registry_metadata_preserves_rule_validation(self):
        bundle = build_contract_bundle(sources(), 'sourcehash', proposal())
        bundle['rule_registry'].update(provenance='Historical descriptive metadata',
                                       references=['Archived design notes'])
        original = deepcopy(bundle)
        self.assertEqual(behavior_from_contracts(bundle), bundle['behavior'])
        self.assertEqual(bundle, original)
        for label, change in (
            ('schema', lambda b: b['rule_registry'].update(schema='unsupported/1')),
            ('version', lambda b: b['rule_registry'].update(version='99')),
            ('rules', lambda b: b['rule_registry']['rules'][0].update(version='99')),
            ('unknown field', lambda b: b['rule_registry'].update(unknown='value')),
            ('rule mirror', lambda b: b['rules'][0].update(version='99')),
        ):
            with self.subTest(change=label):
                changed = deepcopy(bundle)
                change(changed)
                with self.assertRaisesRegex(ValueError, 'Contract rule registry differs'):
                    behavior_from_contracts(changed)

    def test_context_hash_excludes_guarantees_but_includes_dynamics_and_scope(self):
        b = proposal()
        original = build_contract_bundle(sources(), 'sourcehash', b)
        b['properties'][0]['predicate']['args'][1]['value'] = '30'
        guarantee = build_contract_bundle(sources(), 'sourcehash', b)
        self.assertEqual(original['context_sha256'], guarantee['context_sha256'])
        self.assertNotEqual(original['behavior_sha256'], guarantee['behavior_sha256'])
        b['horizon'] = 4
        self.assertNotEqual(guarantee['context_sha256'], build_contract_bundle(sources(), 'sourcehash', b)['context_sha256'])

    def test_local_scalar_contract_remains_formalized_without_behavior(self):
        reqs = sources()
        tlr = interpret(reqs)
        bundle = build_contract_bundle(reqs, 'sourcehash', None, tlr=tlr)
        row = bundle['contracts'][0]
        self.assertEqual(row['rule']['id'], 'scalar_bound')
        self.assertEqual(row['scalar']['value'], '28')
        self.assertEqual(row['scalar']['unit'], 'V')
        self.assertEqual(row['status'], 'pending_review')
        self.assertEqual(bundle['contracts'][1]['status'], 'needs_interpretation')
        self.assertIsNone(behavior_from_contracts(bundle))
        row['scalar']['value'] = '29'
        with self.assertRaises(ValueError):
            behavior_from_contracts(bundle)

    def test_scalar_source_text_mismatch_is_rejected(self):
        reqs = sources()
        tlr = interpret(reqs)
        tlr['requirements'][0]['text'] = 'Changed after extraction'
        with self.assertRaises(ValueError):
            build_contract_bundle(reqs, 'sourcehash', None, tlr=tlr)

    def test_scalar_symbol_unit_mismatch_is_rejected(self):
        reqs = sources()
        tlr = interpret(reqs)
        tlr['symbols'][0]['unit'] = 's'
        with self.assertRaises(ValueError):
            build_contract_bundle(reqs, 'sourcehash', None, tlr=tlr)

    def test_owner_hints_do_not_allocate_or_promote_trace_metadata(self):
        model = {'inspection': {'elements': [
            {'kind': 'part', 'qualified_name': 'Rail::Controller', 'id': 'p1', 'source_requirement_ids': ['R1'], 'mapping_basis': 'generator_metadata'},
            {'kind': 'part', 'qualified_name': 'Trace::Row', 'id': 'p2', 'source_requirement_ids': ['R1'], 'mapping_basis': 'documentation_reference'}]}}
        row = build_contract_bundle(sources(), 'sourcehash', proposal(), model=model)['contracts'][0]
        self.assertIsNone(row['owner']['qualified_name'])
        self.assertEqual([c['qualified_name'] for c in row['owner']['candidates']], ['Rail::Controller'])

    def test_evidence_hash_mismatch_cannot_attach_stale_pass(self):
        bundle = build_contract_bundle(sources(), 'sourcehash', proposal())
        analysis = {'contract_input': {'behavior_sha256': 'stale'}, 'checks': [{
            'id': 'VoltageLimit', 'kind': 'always', 'requirement_ids': ['R1'], 'verdict': 'bounded_pass'}]}
        linked = bind_contract_evidence(bundle, [], analysis, None)
        self.assertEqual(linked['contracts'][0]['checks'], [])
        self.assertEqual(linked['contracts'][0]['evidence_binding'], 'rejected_behavior_hash_mismatch')

    def test_query_hash_mismatch_never_relabels_result_as_current_evidence(self):
        bundle = build_contract_bundle(sources(), 'sourcehash', proposal())
        analysis = {'artifact_hashes': {'query.smt2': 'new'}, 'checks': [{
            'id': 'VoltageLimit', 'kind': 'always', 'requirement_ids': ['R1'], 'verdict': 'bounded_pass',
            'evidence': {'verdict': 'unsat', 'query_sha256': 'old', 'artifacts': {'query': 'query.smt2'}}}]}
        linked = bind_contract_evidence(bundle, [], analysis, None)
        self.assertEqual(linked['contracts'][0]['checks'][0]['verdict'], 'unknown')
        self.assertEqual(linked['contracts'][0]['evidence_binding'], 'rejected_query_hash_mismatch')

    def test_projection_references_exact_text_and_leaves_eventual_obligation_open(self):
        b = proposal()
        b['properties'].append({'id': 'Progress', 'requirement_ids': ['R2'], 'kind': 'eventual_response', 'trigger': True, 'response': True})
        bundle = build_contract_bundle(sources(), 'sourcehash', b)
        model = render_contract_sysml(bundle)
        ref = model['contract_map']['CONTRACT-Progress']
        body = model['text'][ref['start_offset']:ref['end_offset']]
        self.assertIn('Unbounded eventual response remains unproved', body)
        self.assertNotIn('require constraint', body)
        self.assertEqual(ref['text_sha256'], hashlib.sha256(model['text'].encode()).hexdigest())
        self.assertEqual(ref['formalization'], 'metadata_only')

    def test_first_response_requires_no_early_response_and_preserves_pending_windows(self):
        b = proposal()
        b['variables'].append({'name': 'event', 'type': 'Bool', 'role': 'input'})
        b['properties'] = [{'id': 'Response', 'requirement_ids': ['R2'], 'kind': 'bounded_response',
                            'trigger': True, 'response': v('event'), 'window': {'min': 1, 'max': 2},
                            'response_semantics': 'first_after_trigger'}]
        model = render_contract_sysml(build_contract_bundle(sources(), 'sourcehash', b))
        self.assertIn('(not observedTrace.v_event_0)', model['text'])
        self.assertIn('(observedTrace.v_event_1 or observedTrace.v_event_2)', model['text'])
        self.assertIn('pending trigger steps: [2, 3]', model['text'])
        self.assertNotIn('guarantee_trigger_2', model['text'])

    @unittest.skipUnless(shutil.which('z3'), 'Z3 not available')
    def test_same_contract_yields_sat_violating_candidate_and_exact_query_links(self):
        bundle = build_contract_bundle(sources(), 'sourcehash', proposal())
        with tempfile.TemporaryDirectory() as directory:
            analysis = analyze_behavior(behavior_from_contracts(bundle), Path(directory))
            self.assertEqual(analysis['model_feasibility']['verdict'], 'sat')
            self.assertEqual(analysis['checks'][0]['verdict'], 'counterexample')
            base = (Path(directory) / 'behavior_model.smt2').read_text()
            self.assertNotIn('28', base)
            linked = bind_contract_evidence(bundle, [], analysis, None, Path(directory))
            for evidence in linked['contracts'][0]['checks'][0]['artifacts']:
                self.assertEqual(evidence['sha256'], hashlib.sha256((Path(directory) / evidence['artifact']).read_bytes()).hexdigest())
            self.assertIn('observedTrace.v_voltage_0 <= 28', render_contract_sysml(bundle)['text'])
            self.assertEqual(linked['contracts'][0]['source_alignment'], 'pending')

    def test_real_compiler_checks_independent_initial_transition_and_guarantee_blocks(self):
        if not compiler_capability()['available']:
            self.skipTest('Installed SysML pilot compiler unavailable')
        b = proposal()
        b['transitions'] = [op('=', v('voltage', True), op('ite', True, c('29', 'V'), c('27', 'V')))]
        b['properties'].append({'id': 'SecondLimit', 'requirement_ids': ['R2'], 'kind': 'always', 'predicate': True})
        reqs = sources()
        reqs[1]['text'] = 'Uninterpreted source with a */ closing marker.'
        model = render_contract_sysml(build_contract_bundle(reqs, 'sourcehash', b))
        self.assertEqual(model['text'].count('subject observedTrace : FiniteTrace = candidateTrace;'), 2)
        self.assertEqual(model['text'].count('part candidateTrace : FiniteTrace;'), 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'contract_projection.sysml'
            path.write_text(model['text'])
            result = compile_sysml(path)
        self.assertEqual(result['status'], 'passed', result['diagnostics'])


if __name__ == '__main__':
    unittest.main()
