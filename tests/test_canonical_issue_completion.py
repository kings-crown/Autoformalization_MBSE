"""Offline posthoc issue-judgment completion; no generator or reference inputs."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from canonical_issue_judging import (DIMENSIONS, MODEL_DIMENSIONS, assess_issue_candidate,
    judgment_packet, validate_judgment, normalize_judgment, rescore_issue_assessment, repair_issue_assessment)
from test_canonical_issue_judging import sources, config, diagnostics, response, MODEL


def historical(directory, *, sysml=MODEL, raw=None, assessment=None):
    raw = response() if raw is None else raw
    callback = lambda *args: json.dumps(raw)
    report = assess_issue_candidate(sources(), sysml, diagnostics(), directory,
        bedrock_config=config(), judge_callbacks=[callback, callback])
    if assessment is not None:
        for judge in report['judges']:
            judge['assessment'] = deepcopy(assessment)
            judge['status'] = 'incomplete'
        (Path(directory) / 'report.json').write_text(json.dumps(report))
    return report


class IssueCompletionTests(unittest.TestCase):
    def test_array_wrapped_and_suffix_id_resolved_only_with_valid_known_source_quote(self):
        value = response()['dimensions']
        value[0]['source_basis'][0]['source_id'] = 'R1.text'
        packet = judgment_packet(sources(), MODEL, diagnostics())
        result = validate_judgment(value, packet)
        self.assertTrue(result['complete'])
        self.assertEqual(result['dimensions'][0]['source_basis'][0]['source_id'], 'R1')
        self.assertEqual(len(result['normalizations']), 2)
        value[0]['source_basis'][0]['quote'] = 'fabricated meaning'
        normalized, _ = normalize_judgment(value, packet)
        self.assertEqual(normalized['dimensions'][0]['source_basis'][0]['source_id'], 'R1.text')
        self.assertEqual(validate_judgment(value, packet)['dimensions'][0]['status'], 'unreviewed')

    def test_whitespace_normalization_uses_exact_authoritative_substring_but_not_ellipses_or_added_keywords(self):
        packet = judgment_packet(sources(), MODEL.replace('voltage <= 28', 'voltage\n  <=\t28'), diagnostics())
        value = response()
        result = validate_judgment(value, packet)
        self.assertEqual(result['dimensions'][0]['artifact_basis'][0]['quote'], 'voltage\n  <=\t28')
        self.assertTrue(result['complete'])
        for invalid in ('voltage ... 28', 'doc voltage <= 28', 'voltage < 28'):
            value['dimensions'][0]['artifact_basis'][0]['quote'] = invalid
            self.assertEqual(validate_judgment(value, packet)['dimensions'][0]['status'], 'unreviewed')

    def test_rescore_preserves_original_files_and_restores_unambiguous_historical_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            original, out = Path(tmp)/'original', Path(tmp)/'rescored'
            old = validate_judgment(None, judgment_packet(sources(), MODEL, diagnostics()))
            historical(original, raw=response()['dimensions'], assessment=old)
            before = (original/'report.json').read_bytes()
            result = rescore_issue_assessment(original, out)
            self.assertEqual(result['summary']['joint_pass'], 5)
            self.assertEqual(result['incremental_execution']['actual_calls'], 0)
            self.assertEqual((original/'report.json').read_bytes(), before)
            self.assertTrue(result['revision']['original_report_preserved'])

    def test_valid_judgments_are_immutable_even_when_repair_returns_them_changed(self):
        raw = response()
        raw['dimensions'][0]['artifact_basis'][0]['quote'] = 'fabricated'
        raw['dimensions'][1]['status'] = 'fail'
        raw['dimensions'][2]['status'] = 'unresolved'
        calls = []
        def callback(system, prompt, model, directory, call_id):
            payload = json.loads(prompt);calls.append(payload)
            corrected = response()
            corrected['dimensions'][0]['status'] = 'fail'
            return json.dumps(corrected)
        with tempfile.TemporaryDirectory() as tmp:
            original, out = Path(tmp)/'original', Path(tmp)/'completed'
            prior = historical(original, raw=raw)
            result = repair_issue_assessment(original, out, judge_callbacks=[callback, callback])
            for index, judge in enumerate(result['judges']):
                self.assertEqual(judge['assessment']['dimensions'][0]['status'], 'fail')
                self.assertEqual(judge['assessment']['dimensions'][1:], prior['judges'][index]['assessment']['dimensions'][1:])
            self.assertEqual(result['incremental_execution']['actual_calls'], 2)
        self.assertTrue(all(call['target_dimensions'] == ['source_preservation'] for call in calls))
        self.assertTrue(all('expected_status' not in json.dumps(call) for call in calls))

    def test_missing_model_dimensions_are_unavailable_and_never_called(self):
        raw = response()
        with tempfile.TemporaryDirectory() as tmp:
            original, out = Path(tmp)/'original', Path(tmp)/'completed'
            historical(original, sysml=None, raw=raw)
            callback = Mock(side_effect=AssertionError('Must not call for unavailable dimensions'))
            result = repair_issue_assessment(original, out, judge_callbacks=[callback, callback])
            callback.assert_not_called()
            self.assertEqual(result['summary']['unavailable_dimensions'], 3)
            self.assertEqual(result['summary']['repairable_unreviewed_judgments'], 0)
            self.assertEqual(result['incremental_execution']['actual_calls'], 0)
            self.assertEqual(result['summary']['unreviewed'], 3)

    def test_budget_is_bounded_and_chained_revision_cannot_reset_it(self):
        raw = response()
        raw['dimensions'][0]['artifact_basis'][0]['quote'] = 'fabricated'
        callback = Mock(return_value='not JSON')
        with tempfile.TemporaryDirectory() as tmp:
            original, out = Path(tmp)/'original', Path(tmp)/'completed'
            historical(original, raw=raw)
            result = repair_issue_assessment(original, out, max_attempts=2, judge_callbacks=[callback, callback])
            self.assertEqual(callback.call_count, 4)
            self.assertTrue(all(judge['stop_reason'] == 'budget_exhausted' for judge in result['judges']))
            with self.assertRaisesRegex(ValueError, 'chained'):
                repair_issue_assessment(out, Path(tmp)/'another', judge_callbacks=[callback, callback])
            with self.assertRaisesRegex(ValueError, '1 or 2'):
                repair_issue_assessment(original, Path(tmp)/'bad', max_attempts=3)

    def test_second_attempt_targets_only_remaining_invalid_dimension(self):
        raw = response()
        for row in raw['dimensions'][:2]: row['artifact_basis'][0]['quote'] = 'fabricated'
        seen = []
        def callback(system, prompt, model, directory, call_id):
            data = json.loads(prompt); seen.append((model,data['target_dimensions']))
            rows = [row for row in response()['dimensions'] if row['id'] in data['target_dimensions']]
            if len(rows) == 2: rows[1]['artifact_basis'][0]['quote'] = 'still invalid'
            return json.dumps({'dimensions':rows})
        with tempfile.TemporaryDirectory() as tmp:
            original, out = Path(tmp)/'original', Path(tmp)/'completed'
            historical(original, raw=raw)
            result = repair_issue_assessment(original,out,judge_callbacks=[callback,callback])
            self.assertEqual(result['summary']['joint_pass'],5)
        for model in ['judge-one','judge-two']:
            self.assertEqual([targets for m,targets in seen if m==model],
                             [['source_preservation','obligation_completeness'],['obligation_completeness']])

    def test_new_bedrock_calls_retain_usage_cost_and_cumulative_accounting(self):
        from bedrock_judging import BedrockTransport
        raw=response(); raw['dimensions'][0]['artifact_basis'][0]['quote']='fabricated'
        payload={'output':{'message':{'role':'assistant','content':[{'text':json.dumps(response())}]}},
                 'stopReason':'end_turn','usage':{'inputTokens':100,'outputTokens':50,'totalTokens':150}}
        client=Mock(converse=Mock(return_value=payload))
        transport=BedrockTransport(config(),client_factory=lambda _:client)
        with tempfile.TemporaryDirectory() as tmp:
            original,out=Path(tmp)/'original',Path(tmp)/'completed'
            historical(original,raw=raw)
            with patch('canonical_issue_judging.BedrockTransport',return_value=transport):
                result=repair_issue_assessment(original,out)
            self.assertEqual(result['incremental_execution']['usage']['total_tokens'],300)
            self.assertAlmostEqual(result['incremental_execution']['estimated_cost_usd'],0.0004)
            self.assertIsNone(result['execution']['estimated_cost_usd'])
            self.assertAlmostEqual(result['execution']['known_cost_usd'],0.0004)
            self.assertEqual(result['execution']['calls_with_unknown_cost'],2)
            self.assertEqual(client.converse.call_count,2)
            self.assertTrue((out/result['judges'][0]['correction_attempts'][0]['transport_artifact']).is_file())


if __name__ == '__main__': unittest.main()
