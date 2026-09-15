"""LLM candidates must pass the same typed contract as engineer-supplied models."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import review_behavior_proposal as proposal


def voltage_behavior(nominal='27.8'):
    def var(name, at='current'):
        return {'var': name, 'at': at}
    def op(name, *args):
        return {'op': name, 'args': list(args)}
    return {'schema': 'review_behavior/1', 'horizon': 2, 'step': {'value': '1', 'unit': 's'},
            'variables': [{'name': 'voltage', 'type': 'Real', 'role': 'output', 'unit': 'V'},
                          {'name': 'noise', 'type': 'Real', 'role': 'disturbance', 'unit': 'V', 'bounds': {'lower': '-0.5', 'upper': '0.5'}},
                          {'name': 'nominal', 'type': 'Real', 'role': 'parameter', 'unit': 'V', 'value': nominal}],
            'initial': [op('=', var('voltage'), op('+', var('nominal'), var('noise')))],
            'transitions': [op('=', var('voltage', 'next'), op('+', var('nominal'), var('noise', 'next')))],
            'assumptions': [],
            'properties': [{'id': 'VoltageRobustness', 'requirement_ids': ['REQ-001'], 'kind': 'robustness',
                            'predicate': op('<=', var('voltage'), {'value': '28', 'unit': 'V'})}]}


class BehaviorProposalTests(unittest.TestCase):
    def run_proposal(self, output=None, error=None):
        mock = AsyncMock(return_value=output, side_effect=error)
        with tempfile.TemporaryDirectory() as directory, patch.object(proposal.legacy, '_codex_chat_json', mock):
            result = asyncio.run(proposal.propose_behavior([{'id': 'REQ-001', 'text': 'battery.voltage <= 28 V'}], Path(directory), 'fixture-model'))
            self.assertEqual(result, json.loads((Path(directory) / 'llm_behavior_proposal.json').read_text()))
        mock.assert_awaited_once()
        return result

    def test_valid_candidate_retains_model_origin_and_review_scope(self):
        result = self.run_proposal({'behavior': voltage_behavior(), 'reason': 'Candidate additive disturbance model; engineer must justify its bound.'})
        self.assertEqual(result['status'], 'proposed')
        self.assertEqual(result['origin'], 'llm')
        self.assertIn('unestablished', result['source_fidelity'])
        self.assertEqual(result['candidate']['properties'][0]['requirement_ids'], ['REQ-001'])

    def test_invented_requirement_id_is_rejected_and_raw_candidate_retained(self):
        candidate = voltage_behavior()
        candidate['properties'][0]['requirement_ids'] = ['MADE-UP']
        result = self.run_proposal({'behavior': candidate, 'reason': 'candidate'})
        self.assertEqual(result['status'], 'invalid')
        self.assertIsNone(result['candidate'])
        self.assertEqual(result['raw']['behavior'], candidate)

    def test_declining_unsupported_model_is_explicit(self):
        result = self.run_proposal({'behavior': None, 'reason': 'Requires continuous dynamics outside the supported profile.'})
        self.assertEqual(result['status'], 'not_proposed')

    def test_provider_failure_retains_a_diagnostic(self):
        result = self.run_proposal(error=RuntimeError('synthetic provider failure'))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('synthetic provider failure', result['summary'])


if __name__ == '__main__':
    unittest.main()
