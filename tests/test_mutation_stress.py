"""Mutation evaluation regressions: semantic relations, evidence, and honest failure outcomes."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def var(name):
    return {"var": name}


def literal(value, unit="1"):
    return {"value": str(value), "unit": unit}


def expr(operator, *arguments):
    return {"op": operator, "args": list(arguments)}


def manifest():
    """A source-linked scalar fixture with an explicitly operational reference."""
    return {
        "schema": "mutation_campaign/1",
        "id": "threshold_campaign",
        "reference": {
            "status": "constructed_fixture",
            "author": "test fixture author",
            "description": "An operational comparison reference, not stakeholder ground truth.",
        },
        "context": {
            "variables": [{"name": "n", "type": "Int", "bounds": {"lower": "0"}}],
            "background": [],
        },
        "requirements": [{
            "id": "R1",
            "text": "The controller shall support at least ten languages.",
            "source": {"document": "fixture.csv", "location": "row 2"},
            "formula": expr(">=", var("n"), literal("10")),
        }],
        "variants": [{
            "id": "R1_limit_weakened",
            "requirement_id": "R1",
            "kind": "mutant",
            "category": "limit_violation",
            "eligibility": "The requirement contains an explicit integer lower bound.",
            "rationale": "Reduce the required lower bound by one language.",
            "expected_relation": "weakened",
            "mutation": {"operator": "shift_bound", "value": "-1"},
            "text": "The controller shall support at least nine languages.",
        }],
    }


class ManifestValidationTests(unittest.TestCase):
    def test_reference_metadata_is_optional_and_does_not_claim_approval(self):
        from mutation_stress import validate_manifest
        source = manifest()
        source.pop("reference")
        self.assertEqual(validate_manifest(source)["reference"], {"status": "unspecified"})
        source["reference"] = {"description": "Operational baseline"}
        self.assertEqual(validate_manifest(source)["reference"],
                         {"status": "unspecified", "description": "Operational baseline"})

    def test_original_input_is_not_mutated_during_validation(self):
        from mutation_stress import validate_manifest
        source = manifest()
        before = deepcopy(source)
        result = validate_manifest(source)
        self.assertEqual(source, before)
        self.assertEqual(result["schema"], "mutation_campaign/1")

    def test_rejects_duplicate_requirement_and_variant_identifiers(self):
        from mutation_stress import validate_manifest
        for collection in ("requirements", "variants"):
            source = manifest()
            source[collection].append(deepcopy(source[collection][0]))
            with self.subTest(collection=collection), self.assertRaises(ValueError):
                validate_manifest(source)

    def test_rejects_mutation_of_missing_requirement(self):
        from mutation_stress import validate_manifest
        source = manifest()
        source["variants"][0]["requirement_id"] = "missing"
        with self.assertRaises(ValueError):
            validate_manifest(source)

    def test_rejects_formula_with_undeclared_variable(self):
        from mutation_stress import validate_manifest
        source = manifest()
        source["requirements"][0]["formula"] = expr(">=", var("unregistered"), literal(10))
        with self.assertRaises(ValueError):
            validate_manifest(source)

    def test_rejects_same_requirement_twice_with_different_text(self):
        from mutation_stress import validate_manifest
        source = manifest()
        copy = deepcopy(source["requirements"][0])
        copy["text"] = "This different clause must not silently replace the original."
        source["requirements"].append(copy)
        with self.assertRaises(ValueError):
            validate_manifest(source)

    def test_requires_explicit_unsupported_reason(self):
        from mutation_stress import validate_manifest
        source = manifest()
        source["requirements"][0]["formula"] = None
        source["variants"] = []
        with self.assertRaises(ValueError):
            validate_manifest(source)

    def test_control_cannot_claim_to_be_a_semantic_defect(self):
        from mutation_stress import validate_manifest
        source = manifest()
        source["variants"][0].update(kind="control", category="equivalent_control", expected_relation="changed")
        with self.assertRaises(ValueError):
            validate_manifest(source)


    def test_rejects_operator_category_mismatch_and_reserved_identifier(self):
        from mutation_stress import validate_manifest
        for changes in ({"id": "baseline"}, {"category": "operating_guard_removal"}):
            source = manifest()
            source["variants"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_manifest(source)

    def test_rejects_duplicate_json_keys_and_nonfinite_values(self):
        from mutation_stress import read_json
        for content in ('{"id":"first", "id":"second"}', '{"value": NaN}'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "input.json"
                path.write_text(content)
                with self.assertRaises(ValueError):
                    read_json(path)

    def test_rejects_variant_identifiers_that_escape_the_evidence_directory(self):
        from mutation_stress import validate_manifest
        source = manifest()
        source["variants"][0]["id"] = "../../other_run"
        with self.assertRaises(ValueError):
            validate_manifest(source)

    def test_unsupported_requirement_cannot_acquire_an_invented_mutation(self):
        from mutation_stress import validate_manifest
        source = manifest()
        source["requirements"][0].update(formula=None, unsupported_reason="Unbounded eventual semantics.")
        with self.assertRaises(ValueError):
            validate_manifest(source)


class TypedFormulaTests(unittest.TestCase):
    def test_rejects_invalid_types_dimensions_nonlinearity_and_next_state(self):
        import mutation_core as core
        context = core.validate_context([
            {"name": "voltage", "type": "Real", "unit": "V"},
            {"name": "current", "type": "Real", "unit": "A"},
            {"name": "flag", "type": "Bool"},
        ], [])
        invalid = [
            expr("<=", var("flag"), literal(28)),
            expr("<=", var("voltage"), literal(28, "A")),
            expr("=", expr("*", var("voltage"), var("current")), literal(28, "V")),
            expr("<=", {"var": "voltage", "at": "next"}, literal(28, "V")),
            {"op": "forall", "args": [var("flag")]},
            var("voltage"),
            "(assert true)",
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                core.validate_formula(candidate, context)

    def test_normalizes_exact_unit_conversion_without_float_rounding(self):
        import mutation_core as core
        context = core.validate_context([{"name": "voltage", "type": "Real", "unit": "V"}], [])
        ast = core.validate_formula(expr("<=", var("voltage"), literal("28000", "mV")), context)
        self.assertEqual(ast["args"][1], {"value": "28", "unit": "V"})

    def test_domains_cannot_smuggle_implementation_equations(self):
        import mutation_core as core
        for addition in ({"value": "28"}, {"role": "parameter"}):
            with self.subTest(addition=addition), self.assertRaises(ValueError):
                core.validate_context([{"name": "voltage", "type": "Real", **addition}], [])


@unittest.skipUnless(shutil.which("z3"), "Local Z3 executable is unavailable")
class RealSolverMutationTests(unittest.TestCase):
    def compare(self, canonical, candidate, variables=None, background=None):
        import mutation_core as core
        context = core.validate_context(variables or [{"name": "n", "type": "Int", "bounds": {"lower": "0"}}], background or [])
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output = Path(directory.name)
        return core.compare_formulas(context, canonical, candidate, output), output

    def test_weakened_threshold_has_distinguishing_witness_and_exact_artifacts(self):
        result, output = self.compare(expr(">=", var("n"), literal(10)), expr(">=", var("n"), literal(9)))
        self.assertEqual(result["status"], "compared")
        self.assertEqual(result["classification"], "weakened")
        self.assertTrue(result["difference_detected"])
        self.assertEqual(result["newly_permitted"]["witness"]["n"], "9")
        self.assertEqual(result["newly_forbidden"]["status"], "unsat")
        for label in ("background", "newly_permitted", "newly_forbidden"):
            row = result[label]
            query = output / row["artifacts"]["query"]
            evidence = output / row["artifacts"]["result"]
            self.assertIn("(check-sat)", query.read_text())
            self.assertEqual(json.loads(evidence.read_text())["status"], row["status"])
            self.assertNotIn("query_sha256", row)
            self.assertNotIn("result_sha256", row)
        self.assertEqual(json.loads((output / "comparison.json").read_text()), result)

    def test_strict_boundary_strengthening_exposes_exact_boundary(self):
        result, _ = self.compare(expr("<=", var("voltage"), literal(28, "V")),
                                 expr("<", var("voltage"), literal(28, "V")),
                                 [{"name": "voltage", "type": "Real", "unit": "V"}])
        self.assertEqual(result["classification"], "strengthened")
        self.assertEqual(Decimal(result["newly_forbidden"]["witness"]["voltage"]), Decimal("28"))

    def test_guard_removal_strengthens_and_response_suppression_weakens(self):
        variables = [{"name": "command", "type": "Bool"}, {"name": "response", "type": "Bool"}]
        original = expr("implies", var("command"), var("response"))
        guard, _ = self.compare(original, var("response"), variables)
        self.assertEqual(guard["classification"], "strengthened")
        self.assertEqual(guard["newly_forbidden"]["witness"], {"command": "false", "response": "false"})
        suppression, _ = self.compare(original, True, variables)
        self.assertEqual(suppression["classification"], "weakened")
        self.assertEqual(suppression["newly_permitted"]["witness"], {"command": "true", "response": "false"})

    def test_value_replacement_changes_both_directions(self):
        result, _ = self.compare(expr("=", var("n"), literal(10)), expr("=", var("n"), literal(9)))
        self.assertEqual(result["classification"], "changed")
        self.assertEqual(result["newly_permitted"]["witness"]["n"], "9")
        self.assertEqual(result["newly_forbidden"]["witness"]["n"], "10")

    def test_double_negation_is_equivalent_and_not_detected(self):
        original = expr(">=", var("n"), literal(10))
        result, _ = self.compare(original, expr("not", expr("not", original)))
        self.assertEqual(result["classification"], "equivalent")
        self.assertIs(result["difference_detected"], False)

    def test_inconsistent_background_never_reports_equivalence(self):
        result, output = self.compare(expr(">=", var("n"), literal(10)), expr(">=", var("n"), literal(9)),
                                     background=[{"id": "impossible", "text": "Constructed impossible background.",
                                                  "predicate": expr("<", var("n"), literal(0))}])
        self.assertEqual(result["status"], "background_inconsistent")
        self.assertEqual(result["classification"], "inconclusive")
        self.assertIsNone(result["difference_detected"])
        self.assertNotIn("newly_permitted", result)
        self.assertFalse((output / "newly_permitted.smt2").exists())

    def test_invalid_encoding_is_retained_as_error_without_solver_queries(self):
        result, output = self.compare(expr(">=", var("n"), literal(10)), expr(">=", var("undeclared"), literal(9)))
        self.assertEqual(result["status"], "encoding_error")
        self.assertIsNone(result["difference_detected"])
        self.assertTrue((output / "comparison.json").exists())
        self.assertFalse(list(output.glob("*.smt2")))

    def test_consistency_success_does_not_mask_an_offline_semantic_difference(self):
        import mutation_core as core
        original = expr(">=", var("n"), literal(10))
        changed = expr(">=", var("n"), literal(9))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_consistency = core.check_consistency(manifest()["context"], original, root / "original")
            changed_consistency = core.check_consistency(manifest()["context"], changed, root / "changed")
            comparison = core.compare_formulas(manifest()["context"], original, changed, root / "comparison")
        self.assertEqual(original_consistency["status"], "sat")
        self.assertEqual(changed_consistency["status"], "sat")
        self.assertTrue(comparison["difference_detected"])
        self.assertEqual(comparison["classification"], "weakened")

    def test_evidence_is_never_overwritten(self):
        import mutation_core as core
        original = expr(">=", var("n"), literal(10))
        _, output = self.compare(original, original)
        before = {p.name: p.read_bytes() for p in output.iterdir()}
        with self.assertRaises(ValueError):
            core.compare_formulas(manifest()["context"], original, True, output)
        self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, before)


class InconclusiveSolverTests(unittest.TestCase):
    def test_one_sat_proves_difference_even_when_other_direction_unknown(self):
        import mutation_core as core
        with tempfile.TemporaryDirectory() as directory, patch.object(core, "_version", return_value={}), patch.object(
                core, "_query", side_effect=[{"status": "sat"}, {"status": "sat"}, {"status": "unknown"}]):
            result = core.compare_formulas(manifest()["context"], expr(">=", var("n"), literal(10)), True, directory)
        self.assertTrue(result["difference_detected"])
        self.assertEqual(result["classification"], "inconclusive")
        self.assertEqual(result["status"], "compared")

    def test_unknown_or_timeout_without_sat_is_not_equivalence(self):
        import mutation_core as core
        for status in ("unknown", "timeout", "solver_error"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory, patch.object(
                    core, "_version", return_value={}), patch.object(core, "_query", side_effect=[
                        {"status": "sat"}, {"status": "unsat"}, {"status": status}]):
                result = core.compare_formulas(manifest()["context"], expr(">=", var("n"), literal(10)), True, directory)
            self.assertIsNone(result["difference_detected"])
            self.assertEqual(result["classification"], "inconclusive")

    def test_background_timeout_blocks_directional_comparisons(self):
        import mutation_core as core
        with tempfile.TemporaryDirectory() as directory, patch.object(core, "_version", return_value={}), patch.object(
                core, "_query", return_value={"status": "timeout"}) as query:
            result = core.compare_formulas(manifest()["context"], True, False, directory)
        self.assertEqual(result["status"], "background_inconclusive")
        self.assertEqual(query.call_count, 1)
        self.assertIsNone(result["difference_detected"])

    def test_process_timeout_is_recorded_separately_from_execution_error(self):
        import mutation_core as core
        with patch.object(core.subprocess, "run", side_effect=subprocess.TimeoutExpired("z3", 1, output=b"partial")):
            timed = core._execute("(check-sat)", "z3", 1, {})
        self.assertEqual(timed["status"], "timeout")
        self.assertEqual(timed["stdout"], "partial")
        with patch.object(core.subprocess, "run", side_effect=FileNotFoundError("missing z3")):
            failed = core._execute("(check-sat)", "z3", 1, {})
        self.assertEqual(failed["status"], "solver_error")

    def test_solver_error_output_does_not_turn_into_a_sat_success(self):
        import mutation_core as core
        process = subprocess.CompletedProcess(["z3"], 0, 'sat\n(error "bad expression")\n', "")
        with patch.object(core.subprocess, "run", return_value=process):
            result = core._execute("(check-sat)", "z3", 1, {})
        self.assertEqual(result["status"], "solver_error")


@unittest.skipUnless(shutil.which("z3"), "Local Z3 executable is unavailable")
class FormalCampaignTests(unittest.TestCase):
    def campaign(self):
        source = manifest()
        source["context"]["variables"].extend([
            {"name": "command", "type": "Bool"}, {"name": "response", "type": "Bool"}])
        source["requirements"].extend([
            {"id": "R2", "text": "Respond whenever commanded.",
             "source": {"document": "fixture.csv", "location": "row 3"},
             "formula": expr("implies", var("command"), var("response"))},
            {"id": "R3", "text": "Exactly ten languages shall be supported.",
             "source": {"document": "fixture.csv", "location": "row 4"},
             "formula": expr("=", var("n"), literal(10))},
        ])
        template = source["variants"][0]
        for fields in (
            {"id": "R2_guard", "requirement_id": "R2", "category": "operating_guard_removal",
             "expected_relation": "strengthened", "mutation": {"operator": "remove_guard"}},
            {"id": "R2_suppression", "requirement_id": "R2", "category": "required_response_suppression",
             "expected_relation": "weakened", "mutation": {"operator": "suppress_response"}},
            {"id": "R3_value", "requirement_id": "R3", "category": "value_binding_mismatch",
             "expected_relation": "changed", "mutation": {"operator": "replace_value", "path": ["args", 1], "value": "9"}},
            {"id": "R1_control", "kind": "control", "category": "equivalent_control",
             "expected_relation": "equivalent", "mutation": {"operator": "double_negation"}},
        ):
            variant = {**deepcopy(template), **fields}
            variant.pop("text", None)
            source["variants"].append(variant)
        return source

    def test_all_four_fault_families_and_control_run_with_source_linked_evidence(self):
        import mutation_stress as stress
        source = self.campaign()
        before = deepcopy(source)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "campaign"
            report = stress.run_formal(source, output)
            self.assertEqual(source, before)
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["summary"]["mutant"]["planned"], 4)
            self.assertEqual(report["summary"]["mutant"]["comparable"], 4)
            self.assertEqual(report["summary"]["mutant"]["detected"], 4)
            self.assertEqual(report["summary"]["control"]["false_alarms"], 0)
            self.assertEqual(report["summary"]["control"]["planned"], 1)
            self.assertTrue(all(row["expected_relation_met"] for row in report["comparisons"]))
            # The sibling requirement n=10 must not mask the n=9 distinguishing valuation.
            self.assertEqual(report["comparisons"][0]["comparison"]["newly_permitted"]["witness"]["n"], "9")
            execution = json.loads((output / "execution.json").read_text())
            self.assertEqual(execution["configuration"]["provider_calls"], 0)
            self.assertNotIn("artifact_sha256", report)
            self.assertFalse((output / "implementation").exists())
            self.assertTrue((output / "report.md").is_file())
            frozen = json.loads((output / "manifest.json").read_text())
            self.assertEqual(frozen["requirements"][0]["source"], source["requirements"][0]["source"])

    def test_wrong_expected_relation_is_reported_without_relabeling_solver_result(self):
        import mutation_stress as stress
        source = manifest()
        source["variants"][0]["expected_relation"] = "strengthened"
        with tempfile.TemporaryDirectory() as directory:
            report = stress.run_formal(source, Path(directory) / "campaign")
        row = report["comparisons"][0]
        self.assertEqual(row["comparison"]["classification"], "weakened")
        self.assertIs(row["expected_relation_met"], False)
        self.assertEqual(report["summary"]["mutant"]["expected_relation_met"], 0)
        self.assertEqual(report["summary"]["mutant"]["detected"], 1)

    def test_campaign_does_not_overwrite_existing_directory(self):
        import mutation_stress as stress
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            protected = output / "existing.txt"
            protected.write_text("prior evidence")
            with self.assertRaises((ValueError, FileExistsError)):
                stress.run_formal(manifest(), output)
            self.assertEqual(list(output.iterdir()), [protected])
            self.assertEqual(protected.read_text(), "prior evidence")

    def test_cli_validate_and_formal_commands_have_actionable_exit_codes(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "manifest.json"
            source.write_text(json.dumps(manifest()))
            command = [sys.executable, str(root / "scripts" / "mutation_stress.py")]
            valid = subprocess.run(command + ["validate", "--manifest", str(source)], capture_output=True, text=True)
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(json.loads(valid.stdout)["status"], "valid")
            output = Path(directory) / "evidence"
            run = subprocess.run(command + ["formal", "--manifest", str(source), "--output", str(output)],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)["summary"]["mutant"]["detected"], 1)
            repeat = subprocess.run(command + ["formal", "--manifest", str(source), "--output", str(output)],
                                    capture_output=True, text=True)
            self.assertEqual(repeat.returncode, 2)
            self.assertIn("Mutation campaign error", repeat.stderr)


class SummaryOutcomeTests(unittest.TestCase):
    def test_unresolved_trials_remain_in_planned_denominator(self):
        from mutation_stress import summarize
        rows = []
        for index, (status, classification, detected) in enumerate([
            ("compared", "weakened", True),
            ("compared", "inconclusive", True),  # One SAT suffices, direction remains unresolved.
            ("background_inconclusive", "inconclusive", None),
            ("encoding_error", "inconclusive", None),
        ]):
            rows.append({"variant_id": str(index), "kind": "mutant", "category": "limit_violation",
                         "expected_relation_met": True if classification == "weakened" else None,
                         "comparison": {"status": status, "classification": classification, "difference_detected": detected}})
        result = summarize(rows)["mutant"]
        self.assertEqual(result["planned"], 4)
        self.assertEqual(result["comparable"], 1)
        self.assertEqual(result["inconclusive"], 3)
        self.assertEqual(result["detected"], 2)
        self.assertEqual(result["difference_rate_comparable"], 1)
        self.assertEqual(result["operational_detection_yield"], 0.5)

    def test_zero_comparable_controls_has_no_spurious_zero_false_alarm_rate(self):
        from mutation_stress import summarize
        result = summarize([])["control"]
        self.assertEqual(result["planned"], 0)
        self.assertIsNone(result["difference_rate_comparable"])
        self.assertIsNone(result["operational_detection_yield"])


def replay_fixture(source=None, baseline_value=10, candidate_value=9):
    from mutation_stress import validate_manifest
    from mutation_sources import source_packet
    source = validate_manifest(source or manifest())
    samples = []
    for variant, value in ((None, baseline_value), (source["variants"][0], candidate_value)):
        formulas = {r["id"]: deepcopy(r["formula"]) for r in source["requirements"]}
        formulas["R1"] = expr(">=", var("n"), literal(value))
        samples.append({
            "variant_id": variant["id"] if variant else "baseline", "repetition": 1,
            "source_requirements": source_packet(source, variant), "formulas": formulas,
            "provenance": {"method": "constructed test formula", "review_status": "unreviewed_fixture"},
        })
    return source, {"schema": "mutation_candidates/1", "samples": samples}


class ReplayTrustBoundaryTests(unittest.TestCase):
    def test_legacy_hashes_are_optional_informational_metadata(self):
        from mutation_sources import validate_candidates
        source, payload = replay_fixture()
        for sample in payload["samples"]:
            sample.pop("provenance")
        self.assertEqual(len(validate_candidates(source, payload)["samples"]), 2)
        payload.update(manifest_sha256="old hash", context_sha256="old context hash")
        self.assertEqual(len(validate_candidates(source, payload)["samples"]), 2)

    def test_actual_source_and_optional_structural_context_must_match(self):
        from mutation_sources import validate_candidates
        for field in ("source_text", "source_location", "context"):
            source, payload = replay_fixture()
            if field == "source_text":
                payload["samples"][1]["source_requirements"][0]["text"] += " extra meaning"
            elif field == "source_location":
                payload["samples"][1]["source_requirements"][0]["source"]["location"] = "row 900"
            else:
                payload["context"] = deepcopy(source["context"])
                payload["context"]["variables"][0]["bounds"]["lower"] = "1"
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_candidates(source, payload)

    def test_requires_a_baseline_per_repetition_and_complete_requirement_inventory(self):
        from mutation_sources import validate_candidates
        for change in ("missing_baseline", "missing_formula", "duplicate", "new_repetition"):
            source, payload = replay_fixture()
            if change == "missing_baseline":
                payload["samples"].pop(0)
            elif change == "missing_formula":
                payload["samples"][1]["formulas"].clear()
            elif change == "duplicate":
                payload["samples"].append(deepcopy(payload["samples"][1]))
            else:
                payload["samples"][1]["repetition"] = 2
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_candidates(source, payload)

    def test_invalid_formula_is_a_measured_output_not_an_import_rejection(self):
        from mutation_sources import validate_candidates
        source, payload = replay_fixture()
        payload["samples"][1]["formulas"]["R1"] = "invalid SMT/AST output"
        validated = validate_candidates(source, payload)
        self.assertEqual(validated["samples"][1]["formulas"]["R1"], "invalid SMT/AST output")

    def test_provenance_does_not_require_authorship_or_review_labels(self):
        from mutation_sources import validate_candidates
        for value in ({}, {"method": "AST extraction"}, {"review_status": "old label"}):
            source, payload = replay_fixture()
            payload["samples"][1]["provenance"] = value
            with self.subTest(value=value):
                self.assertEqual(validate_candidates(source, payload)["samples"][1]["provenance"], value)

    def test_null_formulas_require_explicit_unsupported_reasons(self):
        from mutation_sources import validate_candidates
        source, payload = replay_fixture()
        payload["samples"][1]["formulas"]["R1"] = None
        with self.assertRaises(ValueError):
            validate_candidates(source, payload)
        payload["samples"][1]["unsupported"] = {"R1": "No executable extraction adapter."}
        self.assertEqual(validate_candidates(source, payload)["samples"][1]["formulas"]["R1"], None)

    def test_source_packet_preserves_siblings_and_hides_reference_answers(self):
        from mutation_sources import source_packet
        from mutation_stress import validate_manifest
        source = manifest()
        source["requirements"].append({"id": "R2", "text": "Recovery eventually completes.",
            "source": {"document": "fixture.csv", "location": "row 3"},
            "formula": None, "unsupported_reason": "Unbounded eventual semantics."})
        source = validate_manifest(source)
        packet = source_packet(source, source["variants"][0])
        self.assertEqual(len(packet), 2)
        self.assertEqual(packet[0]["text"], source["variants"][0]["text"])
        self.assertEqual(packet[1]["text"], source["requirements"][1]["text"])
        self.assertTrue(all(set(r) == {"id", "text", "source"} for r in packet))
        self.assertEqual(source["requirements"][0]["text"], manifest()["requirements"][0]["text"])


@unittest.skipUnless(shutil.which("z3"), "Local Z3 executable is unavailable")
class ReplaySemanticTests(unittest.TestCase):
    def run_replay(self, source, payload):
        from mutation_stress import run_replay
        with tempfile.TemporaryDirectory() as directory:
            report = run_replay(source, payload, Path(directory) / "replay")
            return deepcopy(report)

    def test_expected_change_is_attributed_only_with_correct_baseline(self):
        source, payload = replay_fixture()
        report = self.run_replay(source, payload)
        row = report["comparisons"][0]
        self.assertTrue(row["baseline_matches_reference"])
        self.assertTrue(row["expected_reference_relation_verified"])
        self.assertTrue(row["expected_mutation_preserved"])
        self.assertTrue(row["attributable_preserved_change"])
        self.assertEqual(report["source_metrics"]["attributable_preserved_mutants"], 1)
        self.assertEqual(row["frozen_neighbor_consistency"]["status"], "sat")

    def test_an_already_wrong_baseline_cannot_receive_mutation_preservation_credit(self):
        source, payload = replay_fixture(baseline_value=9, candidate_value=9)
        report = self.run_replay(source, payload)
        row = report["comparisons"][0]
        self.assertTrue(row["comparison"]["difference_detected"])
        self.assertFalse(row["baseline_matches_reference"])
        self.assertTrue(row["expected_mutation_preserved"])
        self.assertFalse(row["attributable_preserved_change"])
        self.assertEqual(row["generated_baseline_comparison"]["classification"], "equivalent")
        self.assertEqual(report["source_metrics"]["attributable_preserved_mutants"], 0)
        self.assertEqual(report["source_metrics"]["baseline_reference_mismatches"], 1)

    def test_arbitrary_generation_error_does_not_count_as_preserving_intended_change(self):
        source, payload = replay_fixture(candidate_value=8)
        report = self.run_replay(source, payload)
        row = report["comparisons"][0]
        self.assertTrue(row["comparison"]["difference_detected"])
        self.assertTrue(row["expected_relation_met"])
        self.assertFalse(row["expected_mutation_preserved"])
        self.assertFalse(row["attributable_preserved_change"])
        self.assertEqual(report["source_metrics"]["expected_mutation_preserved"], 0)

    def test_invalid_or_missing_candidate_remains_an_inconclusive_planned_trial(self):
        for failure in ("invalid_formula", "missing_candidate"):
            source, payload = replay_fixture()
            if failure == "invalid_formula":
                payload["samples"][1]["formulas"]["R1"] = "not an AST"
            else:
                payload["samples"].pop(1)
            with self.subTest(failure=failure):
                report = self.run_replay(source, payload)
                row = report["comparisons"][0]
                expected = "encoding_error" if failure == "invalid_formula" else "missing_candidate"
                self.assertEqual(row["comparison"]["status"], expected)
                self.assertIsNone(row["comparison"]["difference_detected"])
                self.assertEqual(report["summary"]["mutant"]["planned"], 1)
                self.assertEqual(report["summary"]["mutant"]["inconclusive"], 1)
                self.assertEqual(report["summary"]["mutant"]["detected"], 0)

    def test_collateral_changes_and_runtime_evidence_are_reported_separately(self):
        original = manifest()
        original["context"]["variables"].append({"name": "q", "type": "Int"})
        original["requirements"].append({"id": "R2", "text": "The independent counter shall equal five.",
            "source": {"document": "fixture.csv", "location": "row 3"},
            "formula": expr("=", var("q"), literal(5))})
        source, payload = replay_fixture(original)
        payload["samples"][1]["formulas"]["R2"] = expr("=", var("q"), literal(6))
        payload["samples"][1]["runtime_evidence"] = {"status": "completed", "analysis": {"consistency": "sat"}}
        report = self.run_replay(source, payload)
        row = report["comparisons"][0]
        self.assertEqual(len(row["collateral_changes"]), 1)
        self.assertEqual(row["collateral_changes"][0]["requirement_id"], "R2")
        self.assertEqual(row["collateral_changes"][0]["comparison"]["classification"], "changed")
        self.assertEqual(row["runtime_evidence"], payload["samples"][1]["runtime_evidence"])
        self.assertEqual(report["summary"]["mutant"]["detected"], 1)


    def test_formal_only_variants_are_inventory_items_not_missing_source_trials(self):
        original = manifest()
        control = deepcopy(original["variants"][0])
        control.update(id="formal_only_control", kind="control", category="equivalent_control",
            expected_relation="equivalent", mutation={"operator": "double_negation"})
        control.pop("text")
        original["variants"].append(control)
        source, payload = replay_fixture(original)
        report = self.run_replay(source, payload)
        self.assertEqual(len(report["comparisons"]), 1)
        self.assertEqual(report["source_metrics"]["planned_variant_trials"], 1)
        self.assertEqual(report["summary"]["control"]["planned"], 0)
        eligibility = {row["variant_id"]: row for row in report["source_eligibility"]}
        self.assertFalse(eligibility["formal_only_control"]["eligible"])
        self.assertTrue(eligibility[source["variants"][0]["id"]]["eligible"])
        self.assertEqual(report["reference_checks"]["formal_only_control"]["classification"], "equivalent")

    def test_control_baseline_errors_are_not_reported_as_comparator_false_alarms(self):
        original = manifest()
        original["variants"][0].update(kind="control", category="equivalent_control", expected_relation="equivalent",
            mutation={"operator": "double_negation"}, text="The minimum supported language count shall be ten.")
        source, payload = replay_fixture(original, baseline_value=9, candidate_value=9)
        report = self.run_replay(source, payload)
        self.assertNotIn("false_alarms", report["summary"]["control"])
        self.assertEqual(report["source_metrics"]["control_reference_disagreements"], 1)
        self.assertEqual(report["source_metrics"]["control_generated_meaning_changes"], 0)
        self.assertEqual(report["source_metrics"]["control_changes_from_equivalent_baseline"], 0)

    def test_changed_generation_on_paraphrase_is_recorded_against_equivalent_baseline(self):
        original = manifest()
        original["variants"][0].update(kind="control", category="equivalent_control", expected_relation="equivalent",
            mutation={"operator": "double_negation"}, text="The minimum supported language count shall be ten.")
        source, payload = replay_fixture(original, baseline_value=10, candidate_value=9)
        report = self.run_replay(source, payload)
        self.assertEqual(report["source_metrics"]["control_reference_disagreements"], 1)
        self.assertEqual(report["source_metrics"]["control_generated_meaning_changes"], 1)
        self.assertEqual(report["source_metrics"]["control_changes_from_equivalent_baseline"], 1)
        self.assertFalse(report["comparisons"][0]["expected_mutation_preserved"])

    def test_inconsistent_canonical_bundle_is_reported_without_masking_target_difference(self):
        original = manifest()
        original["requirements"].append({"id": "R2", "text": "The controller supports at most five languages.",
            "source": {"document": "fixture.csv", "location": "row 3"},
            "formula": expr("<=", var("n"), literal(5))})
        source, payload = replay_fixture(original)
        report = self.run_replay(source, payload)
        self.assertEqual(report["canonical_bundle_consistency"]["status"], "unsat")
        row = report["comparisons"][0]
        self.assertEqual(row["frozen_neighbor_consistency"]["status"], "unsat")
        self.assertEqual(row["comparison"]["classification"], "weakened")
        self.assertTrue(row["comparison"]["difference_detected"])


class ScalarExtractionTests(unittest.TestCase):
    def fixture(self):
        from mutation_stress import validate_manifest
        from mutation_sources import source_packet
        source = manifest()
        source["requirements"][0]["binding"] = {"variable": "n", "subject": "controller", "quantity": "language count"}
        source = validate_manifest(source)
        packet = source_packet(source)
        run = {"tlr": {"schema": "review_tlr/1", "symbols": [{"name": "generated_n", "type": "Int", "unit": "1"}],
            "requirements": [{"id": "R1", "text": packet[0]["text"], "status": "supported", "kind": "quantity",
                "subject": "controller", "quantity": "language count", "symbol": "generated_n", "unit": "1",
                "relation": "ge", "value": "10", "context": "all operating contexts"}]}}
        return source, packet, run

    def test_supported_frozen_binding_extracts_the_actual_generated_literal(self):
        from mutation_sources import extract_scalar_formulas
        source, packet, run = self.fixture()
        run["tlr"]["requirements"][0]["value"] = "7"
        formulas, unsupported = extract_scalar_formulas(source, packet, run)
        self.assertFalse(unsupported)
        self.assertEqual(formulas["R1"]["args"][1]["value"], "7")

    def test_native_pipeline_tlr_is_not_reparsed_through_local_heuristics(self):
        from mutation_sources import extract_scalar_formulas
        source, packet, run = self.fixture()
        run["tlr"]["schema"] = "native_tlr/1"
        formulas, unsupported = extract_scalar_formulas(source, packet, run)
        self.assertIsNone(formulas["R1"])
        self.assertIn("no supported", unsupported["R1"])

    def test_type_unit_binding_source_or_added_domain_changes_remain_unsupported(self):
        from mutation_sources import extract_scalar_formulas
        for failure in ("type", "unit", "subject", "source", "minimum", "bounds", "context", "temporal", "binding"):
            source, packet, run = self.fixture()
            row = run["tlr"]["requirements"][0]
            if failure == "type":
                run["tlr"]["symbols"][0]["type"] = "Real"
            elif failure == "unit":
                run["tlr"]["symbols"][0]["unit"] = "V"
            elif failure == "subject":
                row["subject"] = "different controller"
            elif failure == "source":
                row["text"] += " stale"
            elif failure == "minimum":
                run["tlr"]["symbols"][0]["minimum"] = "0"
            elif failure == "bounds":
                run["tlr"]["symbols"][0]["bounds"] = {"lower": "0"}
            elif failure == "context":
                row["context"] = "during maintenance"
            elif failure == "temporal":
                row["kind"] = "bounded_response"
            else:
                source["requirements"][0].pop("binding")
            with self.subTest(failure=failure):
                formulas, unsupported = extract_scalar_formulas(source, packet, run)
                self.assertIsNone(formulas["R1"])
                self.assertIn("R1", unsupported)

    def test_undeclared_guard_or_response_cannot_disappear_in_scalar_extraction(self):
        from mutation_sources import extract_scalar_formulas
        for field in ("trigger", "response"):
            source, packet, run = self.fixture()
            run["tlr"]["requirements"][0][field] = "extra conditional semantics"
            with self.subTest(field=field):
                formulas, unsupported = extract_scalar_formulas(source, packet, run)
                self.assertIsNone(formulas["R1"])
                self.assertIn("R1", unsupported)


class CanonicalExtractionTests(unittest.TestCase):
    def fixture(self):
        from mutation_sources import source_packet
        from mutation_stress import validate_manifest
        source = validate_manifest(manifest())
        packet = source_packet(source)
        run = {"sources": deepcopy(packet), "tlr": {
            "schema": "mbse_tlr/1", "variables": deepcopy(source["context"]["variables"]),
            "assumptions": [], "requirements": [{"id": "R1", "status": "supported",
                "formula": expr(">=", var("n"), literal(7))}]}}
        return source, packet, run

    def test_direct_ast_extraction_preserves_actual_generated_threshold(self):
        from mutation_sources import extract_formulas
        source, packet, run = self.fixture()
        run["tlr"]["variables"][0]["description"] = "Number of supported languages"
        formulas, unsupported = extract_formulas(source, packet, run)
        self.assertFalse(unsupported)
        self.assertEqual(formulas["R1"], expr(">=", {"var": "n", "at": "current"}, literal(7)))

    def test_changed_domains_types_names_or_added_assumptions_are_not_comparable(self):
        from mutation_sources import extract_formulas, _compare_candidate
        for change in ("bounds", "type", "name", "assumption"):
            source, packet, run = self.fixture()
            if change == "bounds":
                run["tlr"]["variables"][0]["bounds"]["upper"] = "100"
            elif change == "type":
                run["tlr"]["variables"][0]["type"] = "Real"
            elif change == "name":
                run["tlr"]["variables"][0]["name"] = "other_n"
                run["tlr"]["requirements"][0]["formula"]["args"][0] = var("other_n")
            else:
                run["tlr"]["assumptions"].append({"id": "extra", "text": "Invented premise",
                    "predicate": expr("<=", var("n"), literal(100))})
            with self.subTest(change=change):
                formulas, unsupported = extract_formulas(source, packet, run)
                self.assertIsNone(formulas["R1"])
                self.assertIn("Comparison context differs", unsupported["R1"])
                outcome = _compare_candidate(source, source["requirements"][0]["formula"], None,
                                             None, 1, "z3", unsupported["R1"])
                self.assertEqual(outcome["status"], "not_comparable")

    def test_source_changes_and_missing_or_duplicate_requirements_cannot_pass(self):
        from mutation_sources import extract_formulas
        for change in ("text", "source", "missing", "duplicate", "new_id"):
            source, packet, run = self.fixture()
            if change == "text":
                run["tlr"]["requirements"][0]["text"] = "A different requirement"
            elif change == "source":
                run["sources"][0]["text"] = "A different requirement"
            elif change == "missing":
                run["tlr"]["requirements"] = []
            elif change == "duplicate":
                run["tlr"]["requirements"].append(deepcopy(run["tlr"]["requirements"][0]))
            else:
                run["tlr"]["requirements"][0]["id"] = "R99"
            with self.subTest(change=change):
                formulas, unsupported = extract_formulas(source, packet, run)
                self.assertIsNone(formulas["R1"])
                self.assertIn("R1", unsupported)

    def test_unsupported_semantics_stay_explicit(self):
        from mutation_sources import extract_formulas
        source, packet, run = self.fixture()
        run["tlr"]["requirements"][0] = {"id": "R1", "status": "unsupported",
                                         "reason": "Requires unbounded temporal semantics"}
        formulas, unsupported = extract_formulas(source, packet, run)
        self.assertIsNone(formulas["R1"])
        self.assertEqual(unsupported["R1"], "Requires unbounded temporal semantics")

    def test_real_json_import_and_supplied_candidate_remain_extractable(self):
        from canonical_cli import run_candidate, sources_from_file
        from mutation_sources import extract_formulas
        source, packet, run = self.fixture()
        packet[0]["text"] = "  " + packet[0]["text"] + "\n"
        packet[0]["source"].update(section="5.2", definitions=["Language means selectable interface language"],
                                    input_location="archived section 5.2")
        source["requirements"][0]["text"] = packet[0]["text"]
        source["requirements"][0]["source"] = deepcopy(packet[0]["source"])
        with tempfile.TemporaryDirectory() as directory, patch("canonical_cli._compile", return_value={"status": "passed"}), \
                patch("canonical_cli._ask", side_effect=AssertionError("Fixture must not call provider")):
            input_path = Path(directory) / "requirements.json"
            input_path.write_text(json.dumps({"requirements": packet}))
            imported = sources_from_file(input_path)
            self.assertEqual(imported, packet)
            output = Path(directory) / "candidate"
            result = run_candidate(imported, output, "B", model="test-model", context=source["context"], tlr=run["tlr"])
            self.assertEqual(result["status"], "completed", result["errors"])
            result["sources"] = json.loads((output / "sources.json").read_text())
            formulas, unsupported = extract_formulas(source, packet, result)
        self.assertFalse(unsupported)
        self.assertEqual(formulas["R1"]["args"][1]["value"], "7")

    def test_json_import_rejects_duplicate_keys_before_normalization(self):
        from canonical_cli import sources_from_file
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requirements.json"
            path.write_text('[{"id":"R1","text":"first", "text":"silently replaced"}]')
            with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
                sources_from_file(path)

    def test_source_generation_passes_only_fixed_context_and_reads_canonical_artifacts(self):
        import mutation_sources as sources
        from unittest.mock import Mock
        source, packet, run = self.fixture()
        observed = []
        def start(command, stdout, stderr, start_new_session):
            observed.append(command)
            output = Path(command[command.index("--output-dir") + 1])
            output.mkdir()
            (output / "tlr.json").write_text(json.dumps(run["tlr"]))
            (output / "sources.json").write_text(json.dumps(packet))
            stdout.write(json.dumps({"status": "completed", "tlr": str(output / "tlr.json"),
                                     "analysis": {"consistency": "sat"}}))
            stdout.flush()
            process = Mock()
            process.wait.return_value = 0
            return process
        with tempfile.TemporaryDirectory() as directory, patch.object(sources.subprocess, "Popen", side_effect=start):
            result = sources._generate_sample(source, packet, "baseline", 1, Path(directory),
                                               "pipeline", 5, model="test-model")
            command = observed[0]
            context = json.loads(Path(command[command.index("--context-file") + 1]).read_text())
            self.assertEqual(context, source["context"])
            self.assertNotIn("requirements", context)
        self.assertIn("canonical_cli.py", command[1])
        self.assertEqual(command[command.index("--condition") + 1], "C")
        self.assertEqual(command[command.index("--model") + 1], "test-model")
        self.assertEqual(command[command.index("--abstention-repairs") + 1], "0")
        self.assertEqual(result["formulas"]["R1"], expr(">=", {"var": "n", "at": "current"}, literal(7)))
        self.assertEqual(result["status"], "completed")


class SourceGenerationBudgetTests(unittest.TestCase):
    def test_workflow_budget_is_checked_before_output_or_generation(self):
        import mutation_sources as sources
        with tempfile.TemporaryDirectory() as directory, patch.object(sources, "_generate_sample") as generate:
            output = Path(directory) / "never_created"
            with self.assertRaises(ValueError):
                sources.run_source(manifest(), output, repetitions=2, max_generations=3)
            generate.assert_not_called()
            self.assertFalse(output.exists())

    def test_repetition_and_engine_bounds_precede_any_generation(self):
        import mutation_sources as sources
        for options in ({"repetitions": True}, {"repetitions": 0}, {"engine": "invented"}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory, patch.object(sources, "_generate_sample") as generate:
                with self.assertRaises(ValueError):
                    sources.run_source(manifest(), Path(directory) / "never_created", **options)
                generate.assert_not_called()

    @unittest.skipUnless(shutil.which("z3"), "Local Z3 executable is unavailable")
    def test_paired_source_invocations_keep_context_and_do_not_pass_reference_formulas(self):
        import mutation_sources as sources
        source, expected = replay_fixture()
        source["requirements"].append({"id": "R2", "text": "Eventually recover.",
            "source": {"document": "fixture.csv", "location": "row 3"}, "formula": None,
            "unsupported_reason": "Unbounded temporal obligation."})
        observed = []
        def generate(manifest_value, packet, vid, repetition, output, engine, timeout, model=None, abstention_repairs=0):
            observed.append((deepcopy(packet), vid, repetition, engine, timeout, abstention_repairs))
            value = 10 if vid == "baseline" else 9
            return {"variant_id": vid, "repetition": repetition, "source_requirements": packet,
                "formulas": {"R1": expr(">=", var("n"), literal(value)), "R2": None},
                "unsupported": {"R2": "Unbounded temporal obligation."},
                "provenance": {"method": "test generation", "review_status": "unreviewed_fixture"}}
        with tempfile.TemporaryDirectory() as directory, patch.object(sources, "_generate_sample", side_effect=generate):
            report = sources.run_source(source, Path(directory) / "source", max_generations=2)
        self.assertEqual(len(observed), 2)
        self.assertTrue(all(call[3] == "pipeline" for call in observed))
        self.assertTrue(all(call[5] == 0 for call in observed))
        self.assertEqual([call[1] for call in observed], ["baseline", source["variants"][0]["id"]])
        self.assertTrue(all(len(call[0]) == 2 for call in observed))
        self.assertEqual(observed[0][0][1], observed[1][0][1])
        self.assertTrue(all(set(row) == {"id", "text", "source"} for call in observed for row in call[0]))
        self.assertEqual(report["generation_samples"], 2)
        inventory = {row["requirement_id"]: row for row in report["source_inventory"]}
        self.assertEqual(inventory["R2"]["status"], "unsupported")
        self.assertEqual(report["canonical_bundle_unsupported_ids"], ["R2"])
        self.assertEqual(report["comparisons"][0]["frozen_neighbor_unsupported_ids"], ["R2"])
        self.assertEqual(report["source_metrics"]["attributable_preserved_mutants"], 1)

class SourceAbstentionRecoveryTests(unittest.TestCase):
    def test_invalid_budgets_and_fixture_recovery_fail_before_output_or_calls(self):
        import mutation_sources as sources
        for options in ({"abstention_repairs": True}, {"abstention_repairs": -1},
                        {"abstention_repairs": 6}, {"abstention_repairs": 1.5},
                        {"engine": "local", "abstention_repairs": 1}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(sources, "_generate_sample") as generate:
                output = Path(tmp) / "never_created"
                with self.assertRaises(ValueError):
                    sources.run_source(manifest(), output, **options)
                generate.assert_not_called()
                self.assertFalse(output.exists())

    def test_same_explicit_budget_and_fixed_context_for_baseline_mutant_control(self):
        import mutation_sources as sources
        source = manifest()
        source["variants"].append({"id": "R1_paraphrase", "requirement_id": "R1", "kind": "control",
            "category": "equivalent_control", "eligibility": "Constructed paraphrase",
            "rationale": "Preserve the lower bound", "expected_relation": "equivalent",
            "mutation": {"operator": "double_negation"},
            "text": "At least ten languages shall be supported by the controller."})
        observed = []
        def generate(manifest_value, packet, vid, repetition, output, engine, timeout,
                     model=None, abstention_repairs=0):
            observed.append({"packet": deepcopy(packet), "variant": vid,
                "context": deepcopy(manifest_value["context"]), "model": model,
                "abstention_repairs": abstention_repairs})
            return {"variant_id": vid, "repetition": repetition}
        with tempfile.TemporaryDirectory() as tmp, patch.object(sources, "_generate_sample", side_effect=generate), \
                patch.object(sources, "_evaluate_samples", side_effect=lambda m, c, o, meta, t, z: meta):
            metadata = sources.run_source(source, Path(tmp) / "source", max_generations=3,
                                          abstention_repairs=2, model="same-generation-model")
        config = metadata["configuration"]
        self.assertEqual(config["planned_workflow_invocations"], 3)
        self.assertEqual(config["max_additional_model_transport_invocations_per_workflow"], 4)
        self.assertEqual(config["max_model_transport_invocations_per_workflow"], 5)
        self.assertEqual(config["max_model_transport_invocations"], 15)
        self.assertEqual([x["variant"] for x in observed], ["baseline", "R1_limit_weakened", "R1_paraphrase"])
        self.assertTrue(all(x["abstention_repairs"] == 2 for x in observed))
        self.assertTrue(all(x["model"] == "same-generation-model" for x in observed))
        self.assertTrue(all(x["context"] == sources.validate_manifest(source)["context"] for x in observed))
        self.assertTrue(all(set(row) == {"id", "text", "source"} for x in observed for row in x["packet"]))
        self.assertIn("evaluation-only", config["feedback_boundary"])

    def test_subprocess_budget_forwarding_records_actual_recovery_and_guards_context(self):
        import mutation_sources as sources
        from unittest.mock import Mock
        source, packet, run = CanonicalExtractionTests().fixture()
        # A recovered output with an invented bound must remain incomparable.
        run["tlr"]["variables"][0]["bounds"]["upper"] = "100"
        ledger = {"diagnosis_calls": 1, "repair_calls": 1, "accepted_repairs": 1}
        config = {"model": "test-model", "abstention_repairs": 2}
        observed = []
        def start(command, stdout, stderr, start_new_session):
            observed.append(command)
            output = Path(command[command.index("--output-dir") + 1])
            output.mkdir()
            for name, value in (("tlr.json", run["tlr"]), ("sources.json", packet),
                                ("configuration.json", config), ("repair.json", ledger)):
                (output / name).write_text(json.dumps(value))
            stdout.write(json.dumps({"status": "completed", "tlr": str(output / "tlr.json")}))
            stdout.flush()
            process = Mock()
            process.wait.return_value = 0
            return process
        with tempfile.TemporaryDirectory() as tmp, patch.object(sources.subprocess, "Popen", side_effect=start):
            result = sources._generate_sample(source, packet, "baseline", 1, Path(tmp),
                "pipeline", 5, model="test-model", abstention_repairs=2)
            command = observed[0]
            self.assertEqual(command[command.index("--abstention-repairs") + 1], "2")
            supplied = json.loads(Path(command[command.index("--context-file") + 1]).read_text())
            self.assertEqual(supplied, source["context"])
        self.assertEqual(result["runtime_evidence"]["configuration"], config)
        self.assertEqual(result["runtime_evidence"]["repair"], ledger)
        self.assertIsNone(result["formulas"]["R1"])
        self.assertIn("Comparison context differs", result["unsupported"]["R1"])

    def test_cli_default_zero_and_explicit_budget_reach_source_runner(self):
        import mutation_stress
        from io import StringIO
        for value in (None, 3):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "manifest.json"
                path.write_text(json.dumps(manifest()))
                args = ["source", "--manifest", str(path), "--output", str(Path(tmp) / "output")]
                if value is not None:
                    args += ["--abstention-repairs", str(value)]
                with patch.object(mutation_stress, "run_source", return_value={"status": "completed", "summary": {}}) as generate, \
                        patch("sys.stdout", new_callable=StringIO):
                    self.assertEqual(mutation_stress.main(args), 0)
                self.assertEqual(generate.call_args.kwargs["abstention_repairs"], value or 0)


class SourceSolverFeedbackTests(unittest.TestCase):
    def test_invalid_feedback_and_combined_budgets_fail_before_output_or_calls(self):
        import mutation_sources as sources
        for options in ({"feedback_repairs": True}, {"feedback_repairs": -1},
                        {"feedback_repairs": 6}, {"feedback_repairs": 1.5},
                        {"engine": "local", "feedback_repairs": 1},
                        {"abstention_repairs": 1, "feedback_repairs": 1}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(sources, "_generate_sample") as generate:
                output = Path(tmp) / "never_created"
                with self.assertRaises(ValueError):
                    sources.run_source(manifest(), output, **options)
                generate.assert_not_called()
                self.assertFalse(output.exists())

    def test_same_feedback_budget_for_baseline_mutant_control_with_model_call_bound(self):
        import mutation_sources as sources
        source = manifest()
        source["variants"].append({"id": "R1_paraphrase", "requirement_id": "R1", "kind": "control",
            "category": "equivalent_control", "eligibility": "Constructed paraphrase",
            "rationale": "Preserve the lower bound", "expected_relation": "equivalent",
            "mutation": {"operator": "double_negation"},
            "text": "At least ten languages shall be supported by the controller."})
        observed = []
        def generate(manifest_value, packet, vid, repetition, output, engine, timeout,
                     model=None, abstention_repairs=0, feedback_repairs=0):
            observed.append({"packet": deepcopy(packet), "variant": vid,
                "context": deepcopy(manifest_value["context"]), "model": model,
                "abstention_repairs": abstention_repairs, "feedback_repairs": feedback_repairs})
            return {"variant_id": vid, "repetition": repetition}
        with tempfile.TemporaryDirectory() as tmp, patch.object(sources, "_generate_sample", side_effect=generate), \
                patch.object(sources, "_evaluate_samples", side_effect=lambda m, c, o, meta, t, z: meta):
            metadata = sources.run_source(source, Path(tmp) / "source", max_generations=3,
                                          feedback_repairs=2, model="same-generation-model")
        config = metadata["configuration"]
        self.assertEqual(config["planned_workflow_invocations"], 3)
        self.assertEqual(config["max_additional_model_transport_invocations_per_workflow"], 2)
        self.assertEqual(config["max_model_transport_invocations_per_workflow"], 3)
        self.assertEqual(config["max_model_transport_invocations"], 9)
        self.assertEqual(config["feedback_mode"], "solver")
        self.assertEqual([x["variant"] for x in observed], ["baseline", "R1_limit_weakened", "R1_paraphrase"])
        self.assertTrue(all(x["feedback_repairs"] == 2 and x["abstention_repairs"] == 0 for x in observed))
        self.assertTrue(all(x["model"] == "same-generation-model" for x in observed))
        self.assertTrue(all(x["context"] == sources.validate_manifest(source)["context"] for x in observed))
        self.assertTrue(all(set(row) == {"id", "text", "source"} for x in observed for row in x["packet"]))
        self.assertIn("evaluation-only", config["feedback_boundary"])
        self.assertIn("no held-out comparison findings", config["provider_policy"])

    def test_feedback_forwarded_and_actual_ledger_retained_without_context_relaxation(self):
        import mutation_sources as sources
        from unittest.mock import Mock
        source, packet, run = CanonicalExtractionTests().fixture()
        run["tlr"]["variables"][0]["bounds"]["upper"] = "100"
        ledger = {"schema": "semantic_feedback_repair/1", "feedback_calls": 2, "accepted_repairs": 1}
        config = {"model": "test-model", "feedback_repair_budget": 2, "feedback_mode": "solver"}
        observed = []
        def start(command, stdout, stderr, start_new_session):
            observed.append(command)
            output = Path(command[command.index("--output-dir") + 1])
            output.mkdir()
            for name, value in (("tlr.json", run["tlr"]), ("sources.json", packet),
                                ("configuration.json", config), ("feedback_repair.json", ledger)):
                (output / name).write_text(json.dumps(value))
            stdout.write(json.dumps({"status": "completed", "tlr": str(output / "tlr.json")}))
            stdout.flush()
            process = Mock()
            process.wait.return_value = 0
            return process
        with tempfile.TemporaryDirectory() as tmp, patch.object(sources.subprocess, "Popen", side_effect=start):
            result = sources._generate_sample(source, packet, "baseline", 1, Path(tmp),
                "pipeline", 5, model="test-model", feedback_repairs=2)
            command = observed[0]
            self.assertEqual(command[command.index("--abstention-repairs") + 1], "0")
            self.assertEqual(command[command.index("--feedback-repairs") + 1], "2")
            self.assertEqual(command[command.index("--condition") + 1], "C")
            self.assertEqual(command[command.index("--model") + 1], "test-model")
            supplied = json.loads(Path(command[command.index("--context-file") + 1]).read_text())
            self.assertEqual(supplied, source["context"])
            supplied_packet = json.loads(Path(command[command.index("--statement") + 1]).read_text())
            self.assertEqual(supplied_packet, packet)
            self.assertTrue(all(set(row) <= {"id", "text", "source"} for row in supplied_packet))
            invocation = json.loads((Path(tmp) / "generations" / "1" / "baseline" / "invocation.json").read_text())
        self.assertEqual(invocation["feedback_repairs"], 2)
        self.assertEqual(result["runtime_evidence"]["configuration"], config)
        self.assertEqual(result["runtime_evidence"]["feedback_repair"], ledger)
        self.assertIsNone(result["formulas"]["R1"])
        self.assertIn("Comparison context differs", result["unsupported"]["R1"])

    def test_cli_preserves_default_callback_signature_and_forwards_opt_in(self):
        import mutation_stress
        from io import StringIO
        for value in (None, 0, 3):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "manifest.json"
                path.write_text(json.dumps(manifest()))
                args = ["source", "--manifest", str(path), "--output", str(Path(tmp) / "output")]
                if value is not None:
                    args += ["--feedback-repairs", str(value)]
                with patch.object(mutation_stress, "run_source", return_value={"status": "completed", "summary": {}}) as generate, \
                        patch("sys.stdout", new_callable=StringIO):
                    self.assertEqual(mutation_stress.main(args), 0)
                self.assertEqual(generate.call_args.kwargs["abstention_repairs"], 0)
                if value:
                    self.assertEqual(generate.call_args.kwargs["feedback_repairs"], value)
                else:
                    self.assertNotIn("feedback_repairs", generate.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
