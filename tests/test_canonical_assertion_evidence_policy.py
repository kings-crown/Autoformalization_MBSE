"""Offline regressions for source-frozen documentary versus model evidence."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from canonical_assertions import (build_suite, validate_assertion_response,
                                  validate_suite, validate_verdict)
from canonical_assertion_judging import evaluate_packets
from canonical_assertion_rescore import rescore_assessment


MODEL = """package Network {
    doc /* TTL denotes hop count. */
    doc /* Normal operation excludes the maintenance state. */
    doc /* Retention between evaluations is not represented. */
    // During normal operation, TTL shall be at most 64 hops.
    attribute ttl : ScalarValues::Integer;
    attribute normalOperation : ScalarValues::Boolean;
    require constraint ttlBound { not normalOperation or ttl <= 64 }
}"""


def sources():
    return [{"id": "R1", "text": "During normal operation, TTL shall be at most 64 hops.",
             "source": {"context": ["TTL denotes hop count.",
                 "Normal operation excludes the maintenance state.",
                 "Retention between evaluations is outside the represented scope."]}}]


def definition(aid, category, statement, quote, mode=None):
    value = {"id": aid, "category": category, "statement": statement,
             "source_basis": [{"source_id": "R1", "quote": quote}]}
    if mode is not None:
        value["evidence_requirement"] = mode
    return value


def authored():
    return [{"id": "R1", "assertions": [
        definition("context_meaning", "context",
            "The generated definition of normal operation excludes maintenance.",
            "Normal operation excludes the maintenance state.", "documentation"),
        definition("ttl_units", "unit", "The generated TTL meaning is hop count.",
            "TTL denotes hop count.", "documentation"),
        definition("scope_disclosure", "unsupported_semantics",
            "The generated model discloses that retention is not represented.",
            "Retention between evaluations is outside the represented scope.", "documentation"),
        definition("context_guard", "context",
            "The TTL bound is guarded by normal operation.",
            "During normal operation", "model"),
        definition("ttl_bound", "unit", "The model enforces the 64-hop upper bound.",
            "TTL shall be at most 64 hops.", "model"),
    ]}]


def suite(schema=None):
    rows = authored()
    if schema in {"sysml_assertions/1", "sysml_assertions/2"}:
        for assertion in rows[0]["assertions"]:
            assertion.pop("evidence_requirement")
    kwargs = {} if schema is None else {"schema": schema}
    return build_suite(sources(), rows, **kwargs)


def selected(*ids, schema=None):
    requirement = suite(schema)["requirements"][0]
    requirement["assertions"] = [a for a in requirement["assertions"] if a["id"] in ids]
    return requirement


def span(line, model=MODEL):
    return {"start_line": line, "end_line": line, "quote": model.splitlines()[line - 1]}


def response(requirement, evidence=None):
    return {"requirement_id": requirement["id"], "assertions": [
        {"id": assertion["id"], "status": "pass",
         "rationale": "Compared the cited generated content with the frozen source assertion.",
         "evidence": deepcopy(evidence if evidence is not None else [span(8)]),
         "counterexample": None} for assertion in requirement["assertions"]]}


class FrozenEvidenceRequirementTests(unittest.TestCase):
    def test_new_suite_freezes_explicit_modes_and_keeps_inputs_unchanged(self):
        packet, rows = sources(), authored()
        before_packet, before_rows = deepcopy(packet), deepcopy(rows)
        value = build_suite(packet, rows)
        self.assertEqual(value["schema"], "sysml_assertions/3")
        self.assertEqual(packet, before_packet)
        self.assertEqual(rows, before_rows)
        assertions = value["requirements"][0]["assertions"]
        self.assertEqual(assertions[:5], rows[0]["assertions"])
        self.assertEqual(len(assertions), 8)
        self.assertEqual([a["category"] for a in assertions[5:]],
                         ["coverage", "assumptions", "fidelity"])
        self.assertTrue(all(a["evidence_requirement"] == "model" for a in assertions[5:]))
        self.assertEqual(validate_suite(value), value)

    def test_builder_defaults_are_conservative_and_source_only(self):
        rows = authored()
        for assertion in rows[0]["assertions"]:
            assertion.pop("evidence_requirement")
        before = deepcopy(rows)
        value = build_suite(sources(), rows)
        self.assertEqual(rows, before)
        for assertion in value["requirements"][0]["assertions"]:
            expected = "documentation" if assertion["category"] == "unsupported_semantics" else "model"
            self.assertEqual(assertion["evidence_requirement"], expected)

    def test_frozen_v3_requires_mode_on_every_assertion(self):
        original = suite()
        for index in range(len(original["requirements"][0]["assertions"])):
            value = deepcopy(original)
            value["requirements"][0]["assertions"][index].pop("evidence_requirement")
            with self.subTest(index=index), self.assertRaises(ValueError):
                validate_suite(value)

    def test_unknown_or_malformed_modes_are_rejected(self):
        for mode in ("", "DOCUMENTATION", "documentation_or_model", "either", None,
                     True, 1, ["model"], {"model": True}):
            value = suite()
            value["requirements"][0]["assertions"][0]["evidence_requirement"] = mode
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_suite(value)

    def test_behavioral_categories_cannot_opt_into_documentary_evidence(self):
        for category in ("obligation", "condition", "boundary", "modality", "exception"):
            rows = authored()
            rows[0]["assertions"][0]["category"] = category
            with self.subTest(category=category), self.assertRaises(ValueError):
                build_suite(sources(), rows)

    def test_application_fixed_checks_cannot_opt_into_documentary_evidence(self):
        for category in ("coverage", "assumptions", "fidelity"):
            value = suite()
            assertion = next(a for a in value["requirements"][0]["assertions"]
                             if a["category"] == category)
            assertion["evidence_requirement"] = "documentation"
            with self.subTest(category=category), self.assertRaises(ValueError):
                validate_suite(value)

    def test_disclosure_requires_documentary_mode_and_known_category(self):
        value = suite()
        disclosure = next(a for a in value["requirements"][0]["assertions"]
                          if a["category"] == "unsupported_semantics")
        disclosure["evidence_requirement"] = "model"
        with self.assertRaises(ValueError):
            validate_suite(value)
        value = suite()
        value["requirements"][0]["assertions"][0]["category"] = "documentation"
        with self.assertRaises(ValueError):
            validate_suite(value)

    def test_v1_and_v2_load_without_upgrading_fields_or_denominator(self):
        fields = {"id", "statement", "category", "source_basis"}
        for schema, count in (("sysml_assertions/1", 7), ("sysml_assertions/2", 8)):
            value = suite(schema)
            before = deepcopy(value)
            checked = validate_suite(value)
            with self.subTest(schema=schema):
                self.assertEqual(checked, before)
                self.assertEqual(value, before)
                self.assertEqual(checked["schema"], schema)
                self.assertEqual(len(checked["requirements"][0]["assertions"]), count)
                self.assertTrue(all(set(a) == fields for a in checked["requirements"][0]["assertions"]))
                checked["requirements"][0]["assertions"][0]["evidence_requirement"] = "documentation"
                with self.assertRaises(ValueError):
                    validate_suite(checked)


class EvidenceModeVerdictTests(unittest.TestCase):
    def test_explicit_context_unit_and_disclosure_pass_on_precise_documentation(self):
        for aid, line in (("context_meaning", 3), ("ttl_units", 2), ("scope_disclosure", 4)):
            requirement = selected(aid)
            raw = response(requirement, [span(line)])
            before = deepcopy(raw), deepcopy(requirement)
            with self.subTest(assertion=aid):
                normalized = validate_verdict(raw, requirement, MODEL)["assertions"][0]
                self.assertEqual(normalized["status"], "pass")
                self.assertEqual(normalized["evidence_requirement"], "documentation")
                self.assertEqual(normalized["evidence"][0]["quote"], MODEL.splitlines(keepends=True)[line - 1])
                self.assertEqual((raw, requirement), before)

    def test_context_guards_and_unit_bounds_still_require_model_evidence(self):
        for aid in ("context_guard", "ttl_bound"):
            requirement = selected(aid)
            with self.subTest(assertion=aid):
                with self.assertRaises(ValueError):
                    validate_verdict(response(requirement, [span(5)]), requirement, MODEL)
                normalized = validate_verdict(response(requirement), requirement, MODEL)["assertions"][0]
                self.assertEqual(normalized["status"], "pass")
                self.assertEqual(normalized["evidence_requirement"], "model")

    def test_comments_claiming_implementation_do_not_pass_fixed_checks(self):
        requirement = suite()["requirements"][0]
        requirement["assertions"] = [a for a in requirement["assertions"]
                                      if a["category"] in {"coverage", "assumptions", "fidelity"}]
        for assertion in requirement["assertions"]:
            single = {"id": requirement["id"], "assertions": [assertion]}
            with self.subTest(category=assertion["category"]), self.assertRaises(ValueError):
                validate_verdict(response(single, [span(5)]), single, MODEL)

    def test_documentary_pass_still_requires_nonempty_matching_bounded_citation(self):
        requirement = selected("context_meaning")
        for evidence in ([],
                         [{**span(3), "quote": ""}],
                         [{**span(3), "quote": "Normal operation includes maintenance."}],
                         [{**span(3), "start_line": 2}],
                         [{**span(3), "end_line": 999}],
                         [{**span(3), "start_line": True}]):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                validate_verdict(response(requirement, evidence), requirement, MODEL)

    def test_judge_cannot_add_or_override_the_frozen_evidence_mode(self):
        requirement = selected("context_guard")
        raw = response(requirement, [span(5)])
        raw["assertions"][0]["evidence_requirement"] = "documentation"
        with self.assertRaises(ValueError):
            validate_verdict(raw, requirement, MODEL)

    def test_documentary_mixed_code_span_cannot_rewrite_or_omit_comment(self):
        requirement = selected("ttl_units")
        requirement["assertions"][0]["category"] = "context"
        model = "attribute ttl : Integer; // TTL is a hop count\n"
        for quote in ("attribute ttl : Integer; // TTL is in seconds",
                      "attribute ttl : Integer;"):
            evidence = [{"start_line": 1, "end_line": 1, "quote": quote}]
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                validate_verdict(response(requirement, evidence), requirement, model)
        for evidence in ([{"start_line": 1, "end_line": 1}], [span(1, model)]):
            with self.subTest(evidence=evidence):
                normalized = validate_verdict(response(requirement, evidence), requirement, model)
                verdict = normalized["assertions"][0]
                self.assertEqual(verdict["status"], "pass")
                self.assertEqual(verdict["evidence"][0]["quote"], model)
                self.assertEqual(verdict["evidence"][0]["normalization"],
                                 "exact" if "quote" in evidence[0] else "source_lines")

    def test_pure_documentary_indentation_normalization_preserves_meaning(self):
        requirement = selected("context_meaning")
        evidence = [{**span(3), "quote": span(3)["quote"].lstrip()}]
        verdict = validate_verdict(response(requirement, evidence), requirement, MODEL)["assertions"][0]
        self.assertEqual(verdict["status"], "pass")
        self.assertEqual(verdict["evidence"][0]["normalization"], "documentation_indentation")
        self.assertEqual(verdict["evidence"][0]["raw_quote"], evidence[0]["quote"])
        self.assertEqual(verdict["evidence"][0]["quote"], MODEL.splitlines(keepends=True)[2])

    def test_valid_documentary_and_model_peers_survive_invalid_neighbor(self):
        requirement = selected("context_meaning", "context_guard", "ttl_bound")
        raw = response(requirement)
        by_id = {a["id"]: a for a in raw["assertions"]}
        by_id["context_meaning"]["evidence"] = [span(3)]
        by_id["ttl_bound"]["evidence"] = [span(5)]
        result = validate_assertion_response(raw, requirement, MODEL)
        self.assertEqual(result["validation"]["status"], "partial")
        self.assertEqual(result["validation"]["planned"], 3)
        self.assertEqual(result["validation"]["accepted"], 2)
        self.assertEqual(result["validation"]["rejected"], 1)
        self.assertEqual([a["id"] for a in result["assertions"]], ["context_meaning", "context_guard"])
        self.assertEqual([a["evidence_requirement"] for a in result["assertions"]],
                         ["documentation", "model"])
        self.assertEqual(result["validation"]["assertion_errors"][0]["assertion_id"], "ttl_bound")

    def test_legacy_context_and_unit_defaults_are_model_but_disclosure_remains_eligible(self):
        for schema in ("sysml_assertions/1", "sysml_assertions/2"):
            for aid, line in (("context_meaning", 3), ("ttl_units", 2)):
                requirement = selected(aid, schema=schema)
                before = deepcopy(requirement)
                with self.subTest(schema=schema, assertion=aid):
                    with self.assertRaises(ValueError):
                        validate_verdict(response(requirement, [span(line)]), requirement, MODEL)
                    verdict = validate_verdict(response(requirement), requirement, MODEL)["assertions"][0]
                    self.assertEqual(verdict["evidence_requirement"], "model")
                    self.assertEqual(requirement, before)
            requirement = selected("scope_disclosure", schema=schema)
            verdict = validate_verdict(response(requirement, [span(4)]), requirement, MODEL)["assertions"][0]
            self.assertEqual(verdict["evidence_requirement"], "documentation")
            self.assertNotIn("evidence_requirement", requirement["assertions"][0])


class EvidencePolicyReportingTests(unittest.TestCase):
    def test_offline_judging_records_policy_and_frozen_modes(self):
        value = suite()
        expected_modes = {a["id"]: a["evidence_requirement"]
                          for a in value["requirements"][0]["assertions"]}
        calls = []

        def generator(system, prompt, model, directory, call_id):
            data = json.loads(prompt)
            calls.append(data)
            raw = response({"id": data["target_requirement_id"], "assertions": data["assertions"]})
            for verdict in raw["assertions"]:
                line = {"context_meaning": 3, "ttl_units": 2, "scope_disclosure": 4}.get(verdict["id"], 8)
                # Exercise the actual judge protocol: the evaluator supplies quote text.
                verdict["evidence"] = [{"start_line": line, "end_line": line}]
            return json.dumps(raw)

        packet = {"id": "candidate", "requirements": sources(), "sysml": MODEL, "fixed_context": None}
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "judged"
            report = evaluate_packets([packet], value, ["offline-a", "offline-b"], directory,
                                      [generator, generator])
            configuration = json.loads((directory / "configuration.json").read_text())
            saved = json.loads((directory / "assertions.json").read_text())
        self.assertEqual(saved, value)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["assertions"] == value["requirements"][0]["assertions"] for call in calls))
        self.assertTrue(report["evidence_policy"])
        self.assertEqual(configuration["evidence_policy"], report["evidence_policy"])
        self.assertEqual(configuration["assertion_evidence_requirements"], expected_modes)
        self.assertEqual(report["assertion_evidence_requirements"], expected_modes)
        self.assertEqual(report["joint"]["planned"], len(expected_modes))
        self.assertEqual(report["joint"]["pass"], len(expected_modes))
        self.assertEqual(report["metrics"]["unreviewed"], 0)
        self.assertEqual(report["by_evidence_requirement"]["documentation"]["planned"], 3)
        self.assertEqual(report["by_evidence_requirement"]["documentation"]["pass"], 3)
        self.assertEqual(report["by_evidence_requirement"]["model"]["planned"], 5)
        self.assertEqual(report["by_evidence_requirement"]["model"]["pass"], 5)

    def test_wholly_documentary_candidate_retains_preflight_skip(self):
        value = suite()
        callback = Mock(side_effect=AssertionError("Preflight must not call any judge"))
        packet = {"id": "comments", "requirements": sources(),
                  "sysml": "\n".join(MODEL.splitlines()[1:5]), "fixed_context": None}
        with tempfile.TemporaryDirectory() as tmp:
            report = evaluate_packets([packet], value, ["offline-a", "offline-b"], Path(tmp) / "judged",
                                      [callback, callback])
        callback.assert_not_called()
        self.assertEqual(report["results"][0]["content_screen"]["status"], "no_executable_requirement_content")
        self.assertEqual(report["joint"]["unreviewed"], len(value["requirements"][0]["assertions"]))
        self.assertEqual(report["joint"]["pass"], 0)


class EvidencePolicyRescoreTests(unittest.TestCase):
    def recorded_assessment(self, root, schema=None):
        value = suite(schema)

        def generator(system, prompt, model, directory, call_id):
            data = json.loads(prompt)
            raw = response({"id": data["target_requirement_id"], "assertions": data["assertions"]})
            for verdict in raw["assertions"]:
                line = {"context_meaning": 3, "ttl_units": 2, "scope_disclosure": 4}.get(verdict["id"], 8)
                verdict["evidence"] = [{"start_line": line, "end_line": line}]
            return json.dumps(raw)

        callback = Mock(side_effect=generator)
        packet = {"id": "candidate", "requirements": sources(), "sysml": MODEL, "fixed_context": None}
        directory = root / "original"
        report = evaluate_packets([packet], value, ["offline-a", "offline-b"], directory,
                                  [callback, callback])
        callback.reset_mock()
        return directory, report, callback

    @staticmethod
    def snapshot(directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def test_v3_documentary_passes_replay_without_calls_or_artifact_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original, old_report, callback = self.recorded_assessment(root)
            frozen = self.snapshot(original)
            destination = root / "rescored"
            with patch("bedrock_judging.BedrockTransport", side_effect=AssertionError("No inference allowed")):
                new_report = rescore_assessment(original, destination)
            callback.assert_not_called()
            self.assertEqual(self.snapshot(original), frozen)
            for name in ("configuration.json", "rubric.txt", "assertions.json"):
                self.assertEqual((destination / name).read_bytes(), frozen[name])
            for slot in (1, 2):
                for name in ("prompt.json", "response.txt", "response.json"):
                    relative = f"candidate/requirement-0001/judge-{slot}/{name}"
                    self.assertEqual((destination / relative).read_bytes(), frozen[relative])
            self.assertEqual(new_report["metrics"], old_report["metrics"])
            self.assertEqual(new_report["by_evidence_requirement"], old_report["by_evidence_requirement"])
            self.assertEqual(new_report["by_evidence_requirement"]["documentation"]["pass"], 3)
            self.assertEqual(new_report["joint"]["planned"], 8)
            metadata = json.loads((destination / "rescore.json").read_text())
            self.assertEqual(metadata, new_report["rescore"])
            self.assertEqual(metadata["original_evidence_policy"], old_report["evidence_policy"])
            self.assertEqual(metadata["evidence_policy"], new_report["evidence_policy"])
            self.assertEqual(metadata["assertion_evidence_requirements"], old_report["assertion_evidence_requirements"])
            self.assertEqual(metadata["new_calls"], 0)
            self.assertEqual(metadata["added_cost_usd"], 0)
            self.assertFalse(metadata["raw_replies_changed"])
            self.assertFalse(metadata["rubric_changed"])
            comparison = json.loads((destination / "comparison.json").read_text())
            self.assertEqual(comparison["changed_assertion_count"], 0)
            self.assertEqual(comparison["old"]["evidence_policy"], old_report["evidence_policy"])
            self.assertEqual(comparison["new"]["assertion_evidence_requirements"],
                             new_report["assertion_evidence_requirements"])

    def test_legacy_replay_without_policy_metadata_keeps_model_defaults_and_denominator(self):
        def remove_evidence_metadata(value):
            if isinstance(value, dict):
                return {key: remove_evidence_metadata(item) for key, item in value.items()
                        if key not in {"evidence_policy", "evidence_requirement",
                                       "assertion_evidence_requirements", "by_evidence_requirement"}}
            if isinstance(value, list):
                return [remove_evidence_metadata(item) for item in value]
            return value

        for schema, planned in (("sysml_assertions/1", 7), ("sysml_assertions/2", 8)):
            with self.subTest(schema=schema), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                original, old_report, callback = self.recorded_assessment(root, schema)
                for name in ("configuration.json", "judgments.json"):
                    path = original / name
                    path.write_text(json.dumps(remove_evidence_metadata(json.loads(path.read_text()))))
                frozen = self.snapshot(original)
                with patch("bedrock_judging.BedrockTransport", side_effect=AssertionError("No inference allowed")):
                    new_report = rescore_assessment(original, root / "rescored")
                callback.assert_not_called()
                self.assertEqual(self.snapshot(original), frozen)
                self.assertEqual(new_report["assertion_suite_schema"], schema)
                self.assertEqual(new_report["joint"], old_report["joint"])
                self.assertEqual(new_report["joint"]["planned"], planned)
                self.assertEqual(new_report["joint"]["unreviewed"], 2)
                self.assertEqual(new_report["assertion_evidence_requirements"]["context_meaning"], "model")
                self.assertEqual(new_report["assertion_evidence_requirements"]["ttl_units"], "model")
                self.assertEqual(new_report["assertion_evidence_requirements"]["scope_disclosure"], "documentation")
                assertions = {a["id"]: a for a in new_report["results"][0]["requirements"][0]["assertions"]}
                self.assertEqual(assertions["context_meaning"]["judge_statuses"], ["unreviewed", "unreviewed"])
                self.assertEqual(assertions["ttl_units"]["judge_statuses"], ["unreviewed", "unreviewed"])
                self.assertEqual(assertions["scope_disclosure"]["judge_statuses"], ["pass", "pass"])
                self.assertIn("legacy", new_report["rescore"]["original_evidence_policy"])
                self.assertEqual(new_report["rescore"]["assertion_evidence_requirements"],
                                 new_report["assertion_evidence_requirements"])
                self.assertEqual((root / "rescored" / "assertions.json").read_bytes(), frozen["assertions.json"])
                self.assertEqual((root / "rescored" / "configuration.json").read_bytes(), frozen["configuration.json"])

    def test_saved_report_evidence_mode_drift_is_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original, report, callback = self.recorded_assessment(root)
            report["assertion_evidence_requirements"]["context_meaning"] = "model"
            (original / "judgments.json").write_text(json.dumps(report))
            frozen = self.snapshot(original)
            destination = root / "rescored"
            with self.assertRaisesRegex(ValueError, "frozen assertion evidence requirements"):
                rescore_assessment(original, destination)
            callback.assert_not_called()
            self.assertFalse(destination.exists())
            self.assertEqual(self.snapshot(original), frozen)


if __name__ == "__main__":
    unittest.main()
