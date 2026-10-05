"""Scope, binding, preflight and common-policy tests; no inference calls."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from canonical_abstractions import POLICY, POLICY_TEXT, POLICY_VERSION, representation_summary
from canonical_cli import SYSML_INSTRUCTIONS, TLR_INSTRUCTIONS, _fixed_context, read_json, run_candidate
from canonical_tlr import validate_tlr, render_sysml, tlr_context
from canonical_sysml_screen import requirement_content
from canonical_audits import audit_tlr
from canonical_assertion_judging import AUTHOR_PROMPT, JUDGE_PROMPT, evaluate_packets
from canonical_assertions import AUTHOR_SYSTEM, EVALUATOR_SYSTEM, build_suite
from canonical_judging import RUBRIC
from review_sysml import compiler_capability

EXAMPLE = ROOT / 'examples/canonical/message_abstractions'


def fixture():
    return read_json(EXAMPLE / 'requirements.json'), read_json(EXAMPLE / 'tlr.json')


from source_review_support import pass_source_review

@patch("canonical_cli._review_ask", new=pass_source_review)
class AbstractionTests(unittest.TestCase):
    def test_declared_capability_and_occurrence_roundtrip(self):
        sources, tlr = fixture()
        checked = validate_tlr(tlr, sources, require_abstractions=True)
        self.assertEqual(validate_tlr(checked, sources), checked)
        self.assertEqual(tlr_context(checked)['variables'][0]['name'], 'unicast_send_available')
        self.assertNotIn('description', tlr_context(checked)['variables'][0])
        text = render_sysml(checked)
        for value in ('Abstraction kind: capability', 'Abstraction operation:', 'Abstraction scope:',
                      'Abstraction kind: event_relation', 'v_receiving_address == observedSystem.v_specified_address'):
            self.assertIn(value, text)
        summary = representation_summary(checked)
        self.assertEqual(summary['by_kind']['capability'], 2)
        self.assertEqual(summary['by_kind']['event_relation'], 1)
        self.assertFalse(summary['implementation_verified'])

    def test_shared_policy_reaches_every_generation_and_judge_prompt(self):
        for prompt in (SYSML_INSTRUCTIONS, TLR_INSTRUCTIONS, AUTHOR_PROMPT, JUDGE_PROMPT,
                       AUTHOR_SYSTEM, EVALUATOR_SYSTEM, RUBRIC):
            self.assertIn(POLICY_TEXT, prompt)
            self.assertIn('support flag cannot stand for an actual recipient restriction', prompt)

    def test_new_generation_cannot_omit_policy_or_scope(self):
        _, tlr = fixture()
        for field in ('abstraction_policy',):
            changed = deepcopy(tlr); changed.pop(field)
            with self.assertRaises(ValueError): validate_tlr(changed, require_abstractions=True)
        for field in ('abstraction',):
            changed = deepcopy(tlr); changed['requirements'][0].pop(field)
            with self.assertRaises(ValueError): validate_tlr(changed)
        for field in ('meaning', 'scope', 'subject', 'operation', 'symbol', 'limitations'):
            changed = deepcopy(tlr); changed['requirements'][0]['abstraction'].pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError): validate_tlr(changed)

    def test_capability_requires_positive_bound_boolean_and_limits(self):
        _, tlr = fixture()
        for formula in ({'var':'receiving_address'}, {'op':'not','args':[{'var':'unicast_send_available'}]},
                        {'op':'implies','args':[{'var':'unicast_send_available'},{'var':'unicast_received'}]}):
            changed = deepcopy(tlr); changed['requirements'][0]['formula'] = formula
            with self.assertRaises(ValueError): validate_tlr(changed)
        changed = deepcopy(tlr); changed['requirements'][0]['abstraction']['limitations'] = []
        with self.assertRaises(ValueError): validate_tlr(changed)

    def test_descriptions_required_for_every_referenced_symbol(self):
        _, tlr = fixture()
        for variable in tlr['variables']:
            changed = deepcopy(tlr)
            next(v for v in changed['variables'] if v['name'] == variable['name']).pop('description')
            with self.subTest(name=variable['name']), self.assertRaises(ValueError): validate_tlr(changed)

    def test_event_relation_cannot_be_a_single_flag(self):
        _, tlr = fixture()
        for formula in ({'var':'unicast_received'}, {'op':'implies','args':[{'var':'unicast_received'},{'var':'unicast_received'}]}):
            changed = deepcopy(tlr); changed['requirements'][1]['formula'] = formula
            with self.assertRaises(ValueError): validate_tlr(changed)

    def test_limits_and_source_questions_are_distinct(self):
        for status, code in [('unsupported','profile_limit'), ('unsupported','resource_limit'),
                             ('unresolved','source_ambiguity'), ('unresolved','missing_context')]:
            tlr = {'schema':'mbse_tlr/1','abstraction_policy':POLICY_VERSION,'variables':[],
                   'requirements':[{'id':'R','status':status,'reason_code':code,'reason':'Explicit reason.'}]}
            result = validate_tlr(tlr)
            self.assertEqual(result['requirements'][0]['reason_code'],code)
            self.assertEqual(representation_summary(result)['formalization_status'],'no_executable_formalization')
            tlr['requirements'][0]['reason_code'] = 'missing_context' if status == 'unsupported' else 'profile_limit'
            with self.assertRaises(ValueError): validate_tlr(tlr)

    def test_legacy_input_is_readable_but_not_reclassified(self):
        _, tlr = fixture(); tlr.pop('abstraction_policy')
        for row in tlr['requirements']: row.pop('abstraction')
        result = validate_tlr(tlr)
        self.assertEqual(representation_summary(result)['by_kind']['unclassified'],3)
        self.assertIsNone(representation_summary(result)['policy'])
        self.assertIn('legacy/unclassified',render_sysml(result))

    def test_fixed_meanings_are_validated_and_cannot_change(self):
        sources, tlr = fixture()
        context = tlr_context(tlr)
        context['symbol_meanings'] = {v['name']:v['description'] for v in tlr['variables']}
        self.assertEqual(_fixed_context(context),context)
        bad = deepcopy(context); bad['symbol_meanings'].pop('specified_address')
        with self.assertRaises(ValueError): _fixed_context(bad)
        with tempfile.TemporaryDirectory() as tmp:
            changed = deepcopy(tlr)
            changed['variables'][0]['description'] = 'Every message has already arrived.'
            result = run_candidate(sources,Path(tmp)/'bad','B',model='test',context=context,tlr=changed,compile_model=False)
            self.assertEqual(result['status'],'failed')
            self.assertIn('fixed symbol_meanings',result['errors'][0])
            self.assertFalse((Path(tmp)/'bad/model.sysml').exists())
        with tempfile.TemporaryDirectory() as tmp:
            result = run_candidate(sources,Path(tmp)/'good','B',model='test',context=context,tlr=tlr,compile_model=False)
            self.assertEqual(result['status'],'completed',result['errors'])

    def test_generated_B_uses_policy_and_preserves_bindings_without_solver(self):
        sources, tlr = fixture(); generator=Mock(return_value=json.dumps(tlr))
        with tempfile.TemporaryDirectory() as tmp, patch('canonical_audits.audit_tlr',side_effect=AssertionError('B must not audit')):
            result=run_candidate(sources,Path(tmp)/'B','B',model='test',generator=generator,compile_model=False)
            self.assertEqual(result['status'],'completed',result['errors'])
            self.assertEqual(result['candidate_content']['require_constraint_sites'],3)
            self.assertEqual(result['representation']['constraints_generated'],3)
        self.assertEqual(json.loads(generator.call_args.args[1])['abstraction_policy'],POLICY)

    def test_old_provider_output_without_policy_is_retained_as_failure(self):
        sources, tlr = fixture(); tlr.pop('abstraction_policy')
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'bad'
            result=run_candidate(sources,output,'B',model='test',generator=Mock(return_value=json.dumps(tlr)),compile_model=False)
            self.assertEqual(result['status'],'failed')
            self.assertTrue((output/'candidate_tlr.json').exists())
            self.assertFalse((output/'model.sysml').exists())

    @unittest.skipUnless(shutil.which('z3') and compiler_capability()['available'], 'Real compiler and Z3 required')
    def test_real_compiler_and_solver_check_the_declared_abstractions(self):
        sources, tlr = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            result=run_candidate(sources,Path(tmp)/'BC','BC',model='fixture',tlr=tlr)
            self.assertEqual(result['compilation']['status'],'passed',result['compilation'])
            self.assertEqual(result['analysis']['consistency_status'],'sat')
            self.assertEqual(result['analysis']['representation']['by_kind']['capability'],2)
            recipient=next(r for r in result['analysis']['requirements'] if r['id']=='MSG-RECIPIENT')
            self.assertEqual(recipient['checks']['violatability']['status'],'sat')
            self.assertFalse(result['representation']['implementation_verified'])


@patch("canonical_cli._review_ask", new=pass_source_review)
class ContentPreflightTests(unittest.TestCase):
    def test_documentation_or_strings_do_not_count(self):
        for text in ('package M { doc /* require constraint { true } */ }',
                     'package M { attribute s = "require constraint { true }"; }',
                     "package M { attribute 'require constraint'; }",
                     'package M { // require constraint\n requirement R { doc /* unsupported */ } }'):
            self.assertEqual(requirement_content(text)['require_constraint_sites'],0)
        for text in ('require constraint c { x > 0 }','require { x }','require /* comment */ constraint { x }'):
            self.assertEqual(requirement_content(text)['require_constraint_sites'],1)

    def test_no_content_skips_paid_calls_and_keeps_denominators(self):
        rows=[{'id':'R','text':'The system shall send messages in order.'}]
        authored=[{'id':'R','assertions':[{'id':'ORDER','category':'obligation','statement':'Sending order is preserved.',
                    'source_basis':[{'source_id':'R','quote':rows[0]['text']}]}]}]
        suite=build_suite(rows,authored,schema="sysml_assertions/1")
        packet={'id':'candidate','requirements':rows,'sysml':'package M { requirement R { doc /* unsupported */ } }'}
        callbacks=[Mock(side_effect=AssertionError('must not call')) for _ in range(2)]
        with tempfile.TemporaryDirectory() as tmp:
            report=evaluate_packets([packet],suite,['j1','j2'],Path(tmp)/'judges',callbacks)
            self.assertEqual(report['summary']['planned_calls'],2)
            self.assertEqual(report['summary']['not_run_calls'],2)
            self.assertEqual(report['joint']['planned'],3)
            self.assertEqual(report['joint']['unreviewed'],3)
            self.assertEqual(report['by_category']['coverage']['unreviewed'],1)
            self.assertEqual(report['results'][0]['content_screen']['status'],'no_executable_requirement_content')
        for fn in callbacks: fn.assert_not_called()


if __name__ == '__main__': unittest.main()
