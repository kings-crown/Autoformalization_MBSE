import json
from pathlib import Path
import tempfile
import threading
import unittest

from test_canonical_assertion_judging import MODELS, packet, suite, verdict
from canonical_assertion_judging import evaluate_packets


class JudgeConcurrencyTests(unittest.TestCase):
    def test_two_judges_overlap_and_keep_slot_order(self):
        barrier = threading.Barrier(2)
        calls = []
        lock = threading.Lock()
        def assess(system, prompt, model, directory, call_id):
            with lock:
                calls.append((model, str(directory), prompt))
            barrier.wait(timeout=3)
            return json.dumps(verdict(prompt))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'assessment'
            result = evaluate_packets([packet()], suite(), MODELS, output, [assess, assess], workers=2)
            self.assertEqual(result['status'], 'completed')
            judges = result['results'][0]['requirements'][0]['judges']
            self.assertEqual([j['model'] for j in judges], MODELS)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][2], calls[1][2])
            self.assertNotEqual(calls[0][1], calls[1][1])
            self.assertEqual(json.loads((output / 'configuration.json').read_text())['judge_workers'], 2)

    def test_concurrent_failure_does_not_erase_other_judgment(self):
        def failed(*args):
            raise RuntimeError('provider failed')
        def passed(system, prompt, *args):
            return json.dumps(verdict(prompt))
        with tempfile.TemporaryDirectory() as temporary:
            result = evaluate_packets([packet()], suite(), MODELS, Path(temporary) / 'assessment', [failed, passed], workers=2)
            row = result['results'][0]['requirements'][0]
            self.assertEqual(row['judges'][0]['metrics']['unreviewed'], 3)
            self.assertEqual(row['judges'][1]['metrics']['pass'], 3)
            self.assertEqual(row['joint']['planned'], 3)
            self.assertEqual(row['joint']['unreviewed'], 3)

    def test_invalid_workers_fail_before_output(self):
        for workers in (0, 3, True):
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / 'assessment'
                with self.assertRaises(ValueError):
                    evaluate_packets([packet()], suite(), MODELS, output, [lambda *a: None] * 2, workers=workers)
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
