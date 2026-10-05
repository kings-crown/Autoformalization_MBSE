"""Missing prepared candidates remain in every planned study denominator."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from canonical_judging import packets_from_study


class FailedStudyAssessmentTests(unittest.TestCase):
    def test_preparation_failure_preserves_all_arms_and_repetitions(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "sources.json").write_text(json.dumps([{"id": "R1", "text": "An obligation."}]))
            (p / "study.json").write_text(json.dumps({"status": "failed", "rows": []}))
            (p / "study_configuration.json").write_text(json.dumps({"repetitions": 2, "context": None}))
            packets, observations, sources = packets_from_study(p)
            self.assertEqual(packets, [])
            self.assertEqual(len(observations), 6)
            self.assertEqual({(x["repetition"], x["condition"]) for x in observations},
                             {(r, a) for r in (1, 2) for a in ("A", "B", "C")})
            self.assertTrue(all(x["packet_id"] is None for x in observations))
            self.assertEqual(sources[0]["id"], "R1")


if __name__ == "__main__":
    unittest.main()
