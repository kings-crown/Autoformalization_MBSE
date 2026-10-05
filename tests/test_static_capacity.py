"""Capacity can grow without merging distinct quantities or relaxing typing."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mutation_core as core
from canonical_tlr import validate_tlr, render_sysml
from review_behavior import validate_behavior


class StaticCapacityTests(unittest.TestCase):
    def test_larger_static_packet_preserves_independent_symbols(self):
        variables = [{"name": f"capacity_{i}", "type": "Int", "unit": "1"} for i in range(30)]
        with patch.object(core, "STATIC_MAX_VARIABLES", 64):
            tlr = validate_tlr({"schema": "mbse_tlr/1", "variables": variables,
                "requirements": [{"id": "R1", "status": "supported", "formula": {
                    "op": ">=", "args": [{"var": "capacity_29"}, {"value": "2", "unit": "1"}]}}]})
            self.assertEqual(len(tlr["variables"]), 30)
            self.assertIn("capacity_29", render_sysml(tlr))
            self.assertIn("v_capacity_29_0", core.emit_formula(tlr["requirements"][0]["formula"], core.validate_context(variables, [])))
            with self.assertRaisesRegex(ValueError, "1 to 24"):
                validate_behavior(core._wrapper(variables, []), ["Target"])

    def test_limits_and_type_checks_remain_enforced(self):
        with patch.object(core, "STATIC_MAX_VARIABLES", 32):
            with self.assertRaisesRegex(ValueError, "1 to 32"):
                core.validate_context([{"name": f"v{i}", "type": "Bool"} for i in range(33)], [])
            with self.assertRaises(ValueError):
                core.validate_context([{"name": "flag", "type": "Bool", "unit": "s"}], [])
        for invalid in ("0", "129", "unlimited", "1.5"):
            with self.subTest(invalid=invalid), patch.dict(core.os.environ, {"MBSE_STATIC_MAX_VARIABLES": invalid}):
                with self.assertRaises(ValueError):
                    core._static_capacity()


if __name__ == "__main__":
    unittest.main()
