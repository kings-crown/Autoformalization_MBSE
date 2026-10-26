"""Offline integration checks for bounded, lossless solver-feedback delivery."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import canonical_cli as cli
import canonical_audits
import canonical_request
import requirements_pipeline as transport
from canonical_tlr import validate_tlr
from test_canonical_feedback_execution import fixture, corrected, proposal


def redundant_fixture():
    """Two genuine, independent redundancy concerns; source remains fixed."""
    sources, tlr = fixture()
    tlr = corrected(tlr)
    for rid, op, value, phrase in (("R3", "<=", "30", "at most"),
                                  ("R4", ">=", "18", "at least")):
        sources.append({"id": rid, "text": f"Battery voltage shall be {phrase} {value} V."})
        tlr["requirements"].append({
            "id": rid, "status": "supported",
            "formula": {"op": op, "args": [{"var": "voltage"}, {"value": value, "unit": "V"}]},
            "abstraction": {"kind": "state_constraint", "meaning": f"Battery voltage {op} {value} V.",
                            "scope": "Battery voltage in an observation.", "limitations": []},
        })
    return sources, tlr


class DeliveryEnvironment(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)


class FeedbackDeliveryTests(DeliveryEnvironment):
    def test_exact_framing_system_and_previous_failure_are_counted(self):
        sources, tlr = fixture()
        calls = Mock(side_effect=[RuntimeError("offline diagnostic: voltage ≤ 28 V"),
                                  json.dumps(proposal(sources, tlr, tlr))])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "B"
            result = cli.run_candidate(sources, path, "B", model="offline", tlr=tlr,
                                       compile_model=False, feedback_repairs=2, generator=calls)
            self.assertEqual(result["feedback_repair"]["repair_attempts"], 2)
            for index, call in enumerate(calls.call_args_list, 1):
                system, prompt = call.args[:2]
                expected = canonical_request.compose_request(system, prompt)
                attempt = path / "feedback_attempts" / f"{index:03d}"
                self.assertEqual((attempt / "request.txt").read_text(), expected)
                delivery = cli.read_json(attempt / "evidence_delivery.json")
                self.assertEqual(delivery["request_preflights"][-1]["chars"], len(expected))
                self.assertGreater(len(expected), len(system) + len(prompt))
                self.assertEqual(delivery["request_preflights"][-1]["unit"], "characters")
            second = json.loads(calls.call_args_list[1].args[1])
            self.assertEqual(second["previous_failure"], "RuntimeError: offline diagnostic: voltage ≤ 28 V")
            without_failure = deepcopy(second)
            del without_failure["previous_failure"]
            self.assertGreater(len(calls.call_args_list[1].args[1]),
                               len(json.dumps(without_failure, ensure_ascii=False, indent=2)))

    def test_ask_preflight_blocks_even_when_low_level_transport_is_mocked(self):
        system, prompt = "Complete system instructions", "Private source: 電圧 ≤ 28 V"
        composed = canonical_request.compose_request(system, prompt)
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"CODEX_MAX_INPUT_CHARS": str(len(composed) - 1)}), \
                patch.object(transport, "_run_codex_exec") as run:
            with self.assertRaises(canonical_request.RequestSizeError):
                cli._ask(system, prompt, "offline", Path(temporary), "proposal")
            run.assert_not_called()
            receipt = cli.read_json(Path(temporary) / "proposal.json")
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(receipt["request_preflight"]["chars"], len(composed))
            self.assertEqual(receipt["request_preflight"]["status"], "oversize")

    def test_cli_input_too_large_stops_after_one_invocation(self):
        sources, tlr = fixture()
        calls = Mock(side_effect=RuntimeError("Codex rejected input_too_large before inference"))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "B"
            result = cli.run_candidate(sources, path, "B", model="offline", tlr=tlr,
                                       compile_model=False, feedback_repairs=2, generator=calls)
            ledger = result["feedback_repair"]
            self.assertEqual(calls.call_count, 1)
            self.assertEqual(ledger["repair_attempts"], 1)
            self.assertEqual(ledger["accepted_repairs"], 0)
            self.assertEqual(ledger["stop_reason"], "feedback_input_rejected")
            self.assertEqual(len(ledger["steps"]), 1)
            self.assertFalse((path / "feedback_attempts/002").exists())
            self.assertEqual(result["tlr"], validate_tlr(tlr, sources))
            delivery = cli.read_json(path / "feedback_attempts/001/evidence_delivery.json")
            self.assertTrue(delivery["transport_invoked"])
            self.assertFalse(delivery["response_received"])
            self.assertEqual(delivery["status"], "transport_failed")


@unittest.skipUnless(shutil.which("z3"), "Real offline Z3 is required")
class SolverFeedbackDeliveryTests(DeliveryEnvironment):
    def test_pretty_oversize_compacts_losslessly_with_exact_query_newlines(self):
        sources, tlr = fixture()
        calls = Mock(return_value=json.dumps(proposal(sources, tlr, tlr)))
        preflight = canonical_request.request_preflight
        initialized = []

        def choose_exact_compact_budget(system, prompt, max_chars=None):
            if not initialized and prompt:
                compact = json.dumps(json.loads(prompt), ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False)
                limit = len(canonical_request.compose_request(system, compact))
                os.environ["CODEX_MAX_INPUT_CHARS"] = str(limit)
                initialized.append(limit)
            return preflight(system, prompt, max_chars)

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(canonical_request, "request_preflight", side_effect=choose_exact_compact_budget):
            path = Path(temporary) / "C"
            result = cli.run_candidate(sources, path, "C", model="offline", tlr=tlr,
                                       compile_model=False, feedback_repairs=1, generator=calls)
            self.assertEqual(result["feedback_repair"]["repair_attempts"], 1)
            self.assertEqual(calls.call_count, 1)
            attempt = path / "feedback_attempts/001"
            saved = cli.read_json(attempt / "prompt.json")
            system, compact = calls.call_args.args[:2]
            self.assertEqual(json.loads(compact), saved)
            self.assertEqual(saved["source_packet"], sources)
            self.assertEqual((attempt / "request.txt").read_text(),
                             canonical_request.compose_request(system, compact))
            delivery = cli.read_json(attempt / "evidence_delivery.json")
            self.assertEqual(delivery["serialization"], "compact_json")
            pretty, compressed = delivery["request_preflights"]
            self.assertEqual(pretty["status"], "oversize")
            self.assertEqual(compressed["status"], "passed")
            self.assertEqual(compressed["chars"], initialized[0])
            evidence = json.loads(compact)["solver_feedback"]
            detailed = [check for check in evidence["checks"] if check["evidence_included"]]
            self.assertTrue(detailed)
            for check in detailed:
                query = check.get("query_smt2")
                if query is not None:
                    expected = (path / "feedback_attempts/000/audit" / check["query_file"]).read_text()
                    self.assertEqual(query, expected)
                    self.assertTrue(query.endswith("\n"))
            self.assertEqual(delivery["supplied_to_transport_check_ids"],
                             delivery["prepared_detail_check_ids"])
            self.assertTrue(delivery["transport_invoked"])
            self.assertTrue(delivery["response_received"])

    def test_compact_oversize_retains_evidence_without_spending_repair_call(self):
        sources, tlr = fixture()
        calls = Mock(side_effect=AssertionError("Oversize input must not reach generator"))
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"CODEX_MAX_INPUT_CHARS": "128"}):
            path = Path(temporary) / "C"
            result = cli.run_candidate(sources, path, "C", model="offline", tlr=tlr,
                                       compile_model=False, feedback_repairs=2, generator=calls)
            calls.assert_not_called()
            ledger = result["feedback_repair"]
            self.assertEqual(ledger["repair_attempts"], 0)
            self.assertEqual(ledger["accepted_repairs"], 0)
            self.assertEqual(ledger["stop_reason"], "feedback_input_too_large")
            attempt = path / "feedback_attempts/001"
            self.assertTrue((attempt / "request.txt").is_file())
            self.assertTrue((attempt / "solver_feedback.json").is_file())
            self.assertEqual(ledger["steps"][0]["request_preflight"]["status"], "oversize")
            delivery = cli.read_json(attempt / "evidence_delivery.json")
            self.assertEqual(delivery["status"], "blocked_size")
            self.assertFalse(delivery["transport_invoked"])
            self.assertFalse(delivery["response_received"])
            self.assertTrue(delivery["prepared_detail_check_ids"])
            self.assertEqual(delivery["supplied_to_transport_check_ids"], [])
            self.assertEqual([p["status"] for p in delivery["request_preflights"]],
                             ["oversize", "oversize"])
            self.assertFalse((path / "feedback_attempts/002").exists())
            self.assertEqual(result["tlr"], validate_tlr(tlr, sources))

    def test_unchanged_candidate_advances_independent_concerns_within_two_calls(self):
        sources, tlr = redundant_fixture()
        calls = Mock(return_value=json.dumps(proposal(sources, tlr, tlr)))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "C"
            result = cli.run_candidate(sources, path, "C", model="offline", tlr=tlr,
                                       compile_model=False, feedback_repairs=2, generator=calls)
            self.assertEqual(calls.call_count, 2)
            self.assertEqual(result["feedback_repair"]["repair_attempts"], 2)
            self.assertEqual(result["feedback_repair"]["accepted_repairs"], 0)
            first, second = [json.loads(call.args[1])["solver_feedback"] for call in calls.call_args_list]
            selected1 = first["selection"]["selected_concern_id"]
            selected2 = second["selection"]["selected_concern_id"]
            self.assertIsNotNone(selected1)
            self.assertIsNotNone(selected2)
            self.assertNotEqual(selected1, selected2)
            self.assertIn(selected2, first["selection"]["remaining_concern_ids"])
            for index, evidence in enumerate((first, second), 1):
                selection = evidence["selection"]
                delivery = cli.read_json(path / f"feedback_attempts/{index:03d}/evidence_delivery.json")
                self.assertEqual(delivery["supplied_to_transport_check_ids"], selection["detailed_check_ids"])
                self.assertEqual(delivery["summary_only_check_ids"], selection["summary_only_check_ids"])
                self.assertFalse(set(delivery["summary_only_check_ids"]) &
                                 set(delivery["supplied_to_transport_check_ids"]))
                expected_history = [] if index == 1 else [selected1]
                self.assertEqual(delivery["previously_supplied_concern_ids"], expected_history)
                ordinary = [row for row in evidence["checks"]
                            if row["name"] == "violatability" and row["status"] == "sat"]
                self.assertTrue(ordinary)
                for check in ordinary:
                    self.assertFalse(check["evidence_included"])
                    self.assertIsNone(check["query_smt2"])
                    self.assertIsNone(check["result"])
                    self.assertTrue(check["query_file"])

    def test_accepted_change_resets_history_and_regenerates_full_audit(self):
        sources, tlr = redundant_fixture()
        tlr["requirements"][1]["formula"]["args"][1]["value"] = "21"
        tlr["requirements"][1]["abstraction"]["meaning"] = "Battery voltage is at least 21 V."
        calls = []

        def generate(system, prompt, model, directory, call_id):
            data = json.loads(prompt)
            calls.append(data)
            now = data["current_tlr"]
            after = corrected(now) if len(calls) == 2 else now
            return json.dumps(proposal(sources, now, after))

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(canonical_audits, "audit_tlr", wraps=canonical_audits.audit_tlr) as audit:
            path = Path(temporary) / "C"
            result = cli.run_candidate(sources, path, "C", model="offline", tlr=tlr,
                                       compile_model=False, feedback_repairs=3, generator=generate)
            self.assertEqual(len(calls), 3)
            self.assertEqual(audit.call_count, 2)
            self.assertEqual(result["feedback_repair"]["accepted_repairs"], 1)
            self.assertEqual(result["feedback_repair"]["selected_attempt"], "feedback_attempts/002")
            first, second, third = [cli.read_json(path / f"feedback_attempts/{n:03d}/evidence_delivery.json")
                                    for n in (1, 2, 3)]
            self.assertEqual(second["previously_supplied_concern_ids"], [first["selected_concern_id"]])
            self.assertEqual(third["previously_supplied_concern_ids"], [])
            self.assertEqual(third["based_on"], "feedback_attempts/002")
            self.assertEqual(calls[2]["current_tlr"]["requirements"][1]["formula"]["args"][1]["value"], "20")
            old = cli.read_json(path / "feedback_attempts/000/analysis.json")
            new = cli.read_json(path / "feedback_attempts/002/analysis.json")
            self.assertEqual({r["id"] for r in new["requirements"]}, {s["id"] for s in sources})
            for report, subdir, lower in ((old, "000", "21"), (new, "002", "20")):
                query = report["consistency"]["artifacts"]["query"]
                exact = (path / "feedback_attempts" / subdir / "audit" / query).read_text()
                self.assertIn(f"(>= v_voltage_0 {lower})", exact)
                for row in report["requirements"]:
                    for check in row["checks"].values():
                        if "query" in check.get("artifacts", {}):
                            self.assertTrue((path / "feedback_attempts" / subdir / "audit" /
                                             check["artifacts"]["query"]).is_file())


if __name__ == "__main__":
    unittest.main()
