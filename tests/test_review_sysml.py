"""Semantic handoff and real-compiler regression checks for the review profile."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from review_sysml import compile_sysml, compiler_capability, generate_sysml


def model_fixture():
    requirements = [
        {"id": "R1", "text": "Respond within 5 seconds.", "source": {"document": "source.txt", "location": "line 1"}},
        {"id": "R2", "text": "Respond after 8 seconds.", "source": {}},
        {"id": "R3", "text": "Reliable. */ part injected : MissingType; /*", "source": {}},
    ]
    tlr = {
        "schema": "review_tlr/1",
        "requirements": [
            {"id": "R1", "status": "supported", "symbol": "delay", "unit": "s", "relation": "le", "value": "5.000"},
            {"id": "R2", "status": "supported", "symbol": "delay", "unit": "s", "relation": "gt", "value": "8"},
            {"id": "R3", "status": "unsupported", "reason": "Not quantified."},
        ],
        "symbols": [{"name": "delay", "type": "Real", "unit": "s", "minimum": "0"}],
    }
    return requirements, tlr


class ReviewSysmlTests(unittest.TestCase):
    def test_preserves_quantitative_semantics_and_uninterpreted_source(self):
        requirements, tlr = model_fixture()
        generated = generate_sysml("Demo", requirements, tlr)
        text = generated["text"]
        self.assertIn("attribute q_delay : ISQ::DurationValue", text)
        self.assertIn("modeledSystem.q_delay <= 5.000 [SI::s]", text)
        self.assertIn("modeledSystem.q_delay > 8 [SI::s]", text)
        self.assertEqual(generated["summary"]["formalized"], 2)
        self.assertEqual(generated["summary"]["documentation_only"], 1)
        self.assertIn("requirement <'R3'>", text)
        self.assertNotIn("satisfy Req_3_R3", text)
        self.assertNotIn("*/ part injected", text)

    def test_percent_scale_is_explicit(self):
        requirements, tlr = model_fixture()
        tlr["symbols"][0]["unit"] = "%"
        for clause in tlr["requirements"][:2]:
            clause["unit"] = "%"
        generated = generate_sysml("Percent", requirements, tlr)
        self.assertIn("80 means 80 percent, not 0.8", generated["text"])
        self.assertEqual(generated["summary"]["units_as_metadata"], ["%"])

    def test_rejects_dropped_source_or_unit_mismatch(self):
        requirements, tlr = model_fixture()
        with self.assertRaises(ValueError):
            generate_sysml("Demo", requirements[:-1], tlr)
        tlr["requirements"][0]["unit"] = "m"
        with self.assertRaises(ValueError):
            generate_sysml("Demo", requirements, tlr)

    def test_unavailable_compiler_is_not_a_success(self):
        with patch.dict("os.environ", {"SYSML_KERNEL_JAR": "/missing/review-sysml.jar"}):
            result = compile_sysml(Path("/missing/example.sysml"))
        self.assertEqual(result["status"], "not_run")

    @unittest.skipUnless(compiler_capability()["available"], "Local SysML pilot compiler is unavailable")
    def test_real_compiler_accepts_model_and_rejects_syntax_and_type_errors(self):
        requirements, tlr = model_fixture()
        cases = [
            (generate_sysml("Demo", requirements, tlr)["text"], "passed"),
            ("package Broken { part p : ; }", "failed"),
            ("package Broken { part p : MissingType; }", "failed"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.sysml"
            for text, expected in cases:
                with self.subTest(expected=expected, text=text[:45]):
                    path.write_text(text, encoding="utf-8")
                    result = compile_sysml(path)
                    self.assertEqual(result["status"], expected, result)
                    if expected == "failed":
                        self.assertTrue(any(item["severity"] == "error" for item in result["diagnostics"]))


if __name__ == "__main__":
    unittest.main()
