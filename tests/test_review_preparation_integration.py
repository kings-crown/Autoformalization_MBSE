"""PDF preparation feeds the existing canonical HTTP conversion, without inference."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
from review_server import create_app
from canonical_cli import sources_from_file
from test_review_preparation import pdf_bytes
from test_review_canonical import fixture


@unittest.skipUnless(all(shutil.which(tool) for tool in ('pdfinfo', 'pdftotext', 'z3')),
                     'PDF preparation integration requires Poppler and Z3')
class PreparationConversionTests(unittest.TestCase):
    def test_reviewed_pdf_packet_reaches_shared_renderer_and_solver(self):
        source, tlr, _ = fixture()
        definition = 'Voltage is the observed battery potential measured in volts.'
        excluded = 'The display shall use a green background.'
        proposal = {'schema': 'document_preparation/1', 'items': [
            {'id': 'R1', 'kind': 'requirement', 'text': source['text'],
             'citations': [{'page': 1, 'quote': source['text']}],
             'context_ids': ['VOLT'], 'rationale': 'Source voltage bound.'},
            {'id': 'VOLT', 'kind': 'definition', 'text': definition,
             'citations': [{'page': 2, 'quote': definition}],
             'context_ids': [], 'rationale': 'Defines the measured quantity.'},
            {'id': 'OUT', 'kind': 'requirement', 'text': excluded,
             'citations': [{'page': 2, 'quote': excluded}],
             'context_ids': [], 'rationale': 'A separate display obligation.'},
        ]}

        def wait_for(client, path, terminal):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                response = client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                record = response.json()
                if record['status'] in terminal:
                    return record
                time.sleep(.02)
            self.fail('Local fixture did not complete in time.')

        with tempfile.TemporaryDirectory() as directory, \
             patch('canonical_cli._ask', return_value=json.dumps(proposal)) as provider, \
             patch('canonical_cli._compile', return_value={'status': 'passed', 'diagnostics': []}):
            app = create_app(Path(directory))
            with TestClient(app) as client:
                self.assertEqual(client.get('/').status_code, 200)
                response = client.post('/api/workflow/preparations?filename=sample.pdf',
                                       content=pdf_bytes([source['text'], definition + '\n' + excluded]),
                                       headers={'Content-Type': 'application/pdf'})
                self.assertEqual(response.status_code, 201, response.text)
                preparation_path = '/api/workflow/preparations/' + response.json()['id']
                provider.assert_not_called()
                self.assertEqual(client.get('/api/workflow/runs').json(), [])
                installed_tool = shutil.which
                with patch('review_preparation.shutil.which', side_effect=lambda tool:
                           '/configured/codex' if tool == 'codex' else installed_tool(tool)):
                    response = client.post(preparation_path + '/propose', json={'model': 'fixture-only'})
                self.assertEqual(response.status_code, 202, response.text)
                record = wait_for(client, preparation_path, {'ready', 'failed'})
                self.assertEqual(record['status'], 'ready', record.get('errors'))
                self.assertTrue(all(not item['selected'] for item in record['items']))
                for item in record['items']:
                    item['selected'] = item['id'] in {'R1', 'VOLT'}
                response = client.put(preparation_path + '/review', json={
                    'revision': record['revision'], 'reviewer': 'Fixture reviewer',
                    'acknowledge': True, 'items': record['items'], 'note': 'Battery-only fixture scope.'})
                self.assertEqual(response.status_code, 200, response.text)
                exported = client.get(preparation_path + '/packet')
                self.assertEqual(exported.status_code, 200, exported.text)
                packet = exported.json()
                self.assertEqual(packet['schema'], 'prepared_source_packet/1')
                self.assertEqual(len(packet['requirements']), 1)
                self.assertEqual(packet['shared_context'][0]['text'], definition)
                self.assertNotIn('shared', packet['requirements'][0]['source']['context'])
                # The download is the same reusable input for CLI and GUI. Its
                # shared excerpts appear once on the wire, then the importer
                # resolves the full context for each source requirement.
                downloaded = Path(directory) / 'downloaded-source.json'
                downloaded.write_bytes(exported.content)
                imported = sources_from_file(downloaded, 'json')
                saved_packet = app.state.preparation_store.artifact(record['id'], 'packet.json')
                self.assertEqual(sources_from_file(saved_packet, 'json'), imported)
                self.assertEqual(imported[0]['source']['context']['shared'], packet['shared_context'])
                self.assertEqual(imported[0]['source']['context_ids'], ['VOLT'])
                self.assertEqual(imported[0]['source']['context']['shared'][0]['kind'], 'definition')
                self.assertEqual(imported[0]['source']['context']['shared'][0]['citations'],
                                 [{'page': 2, 'quote': definition}])
                self.assertNotIn(excluded, exported.text)
                self.assertEqual(client.get('/api/workflow/runs').json(), [])
                self.assertEqual(provider.call_count, 1)

                response = client.post('/api/workflow/runs', json={
                    'name': 'Reviewed PDF fixture', 'text': exported.text, 'format': 'json',
                    'condition': 'C', 'tlr': tlr, 'feedback_repairs': 0})
                self.assertEqual(response.status_code, 202, response.text)
                run = wait_for(client, '/api/workflow/runs/' + response.json()['id'], {'completed', 'failed'})
                self.assertEqual(run['status'], 'completed', run.get('errors'))
                self.assertEqual(run['sources'], imported)
                self.assertEqual(run['tlr']['requirements'][0]['source'], imported[0]['source'])
                self.assertEqual(run['analysis']['consistency_status'], 'sat')
                self.assertIn('require constraint', run['model_text'])
                self.assertEqual(provider.call_count, 1, 'Conversion of the supplied TLR must not call a provider.')


if __name__ == '__main__':
    unittest.main()
