"""Offline source-grounding, exact-evidence and denominator checks for LLM judges."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from canonical_assertions import (ASSUMPTIONS_STATEMENT, COVERAGE_STATEMENT,
    MAX_EVIDENCE_LINES, MAX_EVIDENCE_SPANS, assertion_metrics, build_suite,
    validate_assertion_response, validate_suite, validate_verdict)


def legacy_build_suite(*args, **kwargs):
    """Freeze existing assertion fixtures at v1; new v2 behavior has separate tests."""
    return build_suite(*args, **kwargs, schema="sysml_assertions/1")


def sources():
    return [{"id": "R1", "text": "The battery shall have a voltage of at most 28 V.",
             "source": {"document": "Battery specification", "context": {
                 "definition": "Voltage refers to the terminal voltage.",
                 "scope": ["During normal operation."]}}}]


def authored(packet=None):
    packet = packet or sources()
    return [{"id": source["id"], "assertions": [{"id": f"{source['id']}_BOUND",
             "statement": "The upper voltage endpoint is inclusive and equals 28 V.",
             "category": "boundary", "source_basis": [
                 {"source_id": source["id"], "quote": "at most 28 V"}]}]} for source in packet]


def suite():
    return legacy_build_suite(sources(), authored())


SYSML = '''package BatteryModel {
    part def Battery {
        attribute voltage : ScalarValues::Real;
    }
    requirement R1 {
        subject observedSystem : Battery;
        doc /* The battery shall have a voltage of at most 28 V. */
        require constraint limit { observedSystem.voltage <= 28 }
    }
}'''


def response(requirement=None, status="pass", evidence=None):
    requirement = requirement or suite()["requirements"][0]
    if evidence is None:
        evidence = [{"start_line": 8, "end_line": 8, "quote": SYSML.splitlines()[7]}]
    return {"requirement_id": requirement["id"], "assertions": [
        {"id": assertion["id"], "status": status,
         "rationale": "Compared the inclusive voltage limit with the source and context.",
         "evidence": deepcopy(evidence), "counterexample": None}
        for assertion in requirement["assertions"]]}


class AssertionSuiteTests(unittest.TestCase):
    def test_build_adds_required_checks_without_mutating_author_or_sources(self):
        packet, rows = sources(), authored()
        old_packet, old_rows = deepcopy(packet), deepcopy(rows)
        result = legacy_build_suite(packet, rows)
        self.assertEqual(packet, old_packet)
        self.assertEqual(rows, old_rows)
        checks = result["requirements"][0]["assertions"]
        self.assertEqual(len(checks), 3)
        self.assertEqual([a["category"] for a in checks], ["boundary", "coverage", "assumptions"])
        self.assertEqual(checks[1]["statement"], COVERAGE_STATEMENT)
        self.assertEqual(checks[2]["statement"], ASSUMPTIONS_STATEMENT)
        self.assertEqual(result["author"], "LLM-authored; unreviewed")
        self.assertIsNone(result["fixed_context"])

    def test_literal_quotes_can_reference_nested_context_and_other_sources(self):
        packet = sources()
        packet.append({"id": "R2", "text": "The backup battery shall supply at most 28 V.",
                       "source": {"context": ["R1 and R2 share the terminal-voltage definition."]}})
        rows = authored(packet)
        rows[1]["assertions"][0]["source_basis"] = [
            {"source_id": "R1", "quote": "terminal voltage"},
            {"source_id": "R2", "quote": "share the terminal-voltage definition"}]
        result = legacy_build_suite(packet, rows)
        self.assertEqual(len(result["requirements"]), 2)
        self.assertEqual(result["requirements"][1]["assertions"][1]["id"], "ASSERT_0002_COVERAGE")

    def test_source_and_formal_context_drift_fail_before_judging(self):
        context = {"variables": [{"name": "voltage", "type": "Real", "unit": "V"}], "background": []}
        value = legacy_build_suite(sources(), authored(), context=context)
        self.assertEqual(validate_suite(value, sources(), context)["fixed_context"], context)
        changed_source = sources()
        changed_source[0]["source"]["context"]["scope"] = ["In every operating mode."]
        with self.assertRaisesRegex(ValueError, "source packet differs"):
            validate_suite(value, changed_source, context)
        changed_context = deepcopy(context)
        changed_context["variables"][0]["bounds"] = {"upper": "28"}
        with self.assertRaisesRegex(ValueError, "fixed context differs"):
            validate_suite(value, sources(), changed_context)
        with self.assertRaisesRegex(ValueError, "fixed context differs"):
            validate_suite(value, sources(), None)
        self.assertEqual(validate_suite(value)["fixed_context"], context)

    def test_missing_requirement_or_category_cannot_shrink_denominator(self):
        value = suite()
        missing = deepcopy(value)
        missing["requirements"] = []
        with self.assertRaises(ValueError):
            validate_suite(missing)
        wrong_category = deepcopy(value)
        wrong_category["requirements"][0]["assertions"][2]["category"] = "boundary"
        with self.assertRaisesRegex(ValueError, "coverage and assumptions"):
            validate_suite(wrong_category)
        no_substantive = deepcopy(value)
        no_substantive["requirements"][0]["assertions"][0]["category"] = "coverage"
        with self.assertRaises(ValueError):
            validate_suite(no_substantive)

    def test_mandatory_checks_cannot_be_replaced_or_duplicated_in_manual_suite(self):
        original = suite()
        for category in ("coverage", "assumptions"):
            replaced = deepcopy(original)
            assertion = next(a for a in replaced["requirements"][0]["assertions"] if a["category"] == category)
            assertion["statement"] = "The model contains a package."
            with self.subTest(category=category), self.assertRaisesRegex(ValueError, "canonical statements"):
                validate_suite(replaced)
            duplicate = deepcopy(original)
            repeated = deepcopy(next(a for a in duplicate["requirements"][0]["assertions"] if a["category"] == category))
            repeated["id"] = "duplicate_check"
            duplicate["requirements"][0]["assertions"].append(repeated)
            with self.assertRaisesRegex(ValueError, "exactly one"):
                validate_suite(duplicate)
        renamed = deepcopy(original)
        renamed["requirements"][0]["assertions"][1]["id"] = "reviewed_coverage"
        validate_suite(renamed)
        self.assertIn("for this requirement", ASSUMPTIONS_STATEMENT)
        self.assertIn("applicable shared definitions", ASSUMPTIONS_STATEMENT)

    def test_invented_quote_unknown_source_and_expected_answers_are_rejected(self):
        for mutation in (
            lambda a: a.update(source_basis=[{"source_id": "R1", "quote": "at most 30 V"}]),
            lambda a: a.update(source_basis=[{"source_id": "R999", "quote": "at most 28 V"}]),
            lambda a: a.update(source_basis=[{"source_id": "R1", "quote": ""}]),
            lambda a: a.update(expected="pass"),
            lambda a: a.update(category="N/A"),
        ):
            with self.subTest(mutation=mutation):
                value = suite()
                mutation(value["requirements"][0]["assertions"][0])
                with self.assertRaises(ValueError):
                    validate_suite(value)
        value = suite()
        value["expected_labels"] = {"R1": "faithful"}
        with self.assertRaises(ValueError):
            validate_suite(value)

    def test_safe_global_ids_and_bounded_assertion_count(self):
        for bad_id in ("../R1", "has space", "x" * 121, "ASSERT_0001_COVERAGE"):
            rows = authored()
            rows[0]["assertions"][0]["id"] = bad_id
            with self.subTest(id=bad_id), self.assertRaises(ValueError):
                legacy_build_suite(sources(), rows)
        rows = authored()
        rows[0]["assertions"] = [{**deepcopy(rows[0]["assertions"][0]), "id": f"assert_{i}"} for i in range(24)]
        with self.assertRaisesRegex(ValueError, "1 to 23"):
            legacy_build_suite(sources(), rows)
        rows[0]["assertions"].pop()
        self.assertEqual(len(legacy_build_suite(sources(), rows)["requirements"][0]["assertions"]), 25)

    def test_author_cannot_supply_builtins_or_skip_substantive_checks(self):
        rows = authored()
        rows[0]["assertions"][0]["category"] = "coverage"
        with self.assertRaisesRegex(ValueError, "appended"):
            legacy_build_suite(sources(), rows)
        rows[0]["assertions"] = []
        with self.assertRaisesRegex(ValueError, "1 to 23"):
            legacy_build_suite(sources(), rows)


class AssertionEvidenceTests(unittest.TestCase):
    def test_pass_citations_preserve_frozen_definitions_and_input(self):
        requirement = suite()["requirements"][0]
        raw = response(requirement)
        before = deepcopy(raw)
        result = validate_verdict(raw, requirement, SYSML)
        self.assertEqual(raw, before)
        for actual, definition in zip(result["assertions"], requirement["assertions"]):
            self.assertEqual(actual["status"], "pass")
            self.assertEqual(actual["statement"], definition["statement"])
            self.assertEqual(actual["source_basis"], definition["source_basis"])

    def test_wrong_quote_range_or_semantic_token_changes_are_rejected(self):
        requirement = suite()["requirements"][0]
        original = response(requirement)
        for span in (
            {"start_line": 7, "end_line": 7, "quote": SYSML.splitlines()[7]},
            {"start_line": 8, "end_line": 8, "quote": SYSML.splitlines()[7].replace("28", "30")},
            {"start_line": True, "end_line": 8, "quote": SYSML.splitlines()[7]},
            {"start_line": 8, "end_line": 999, "quote": SYSML.splitlines()[7]},
        ):
            raw = deepcopy(original)
            raw["assertions"][0]["evidence"] = [span]
            with self.subTest(span=span), self.assertRaises(ValueError):
                validate_verdict(raw, requirement, SYSML)

    def test_multiline_evidence_preserves_original_internal_line_breaks(self):
        requirement = suite()["requirements"][0]
        text = "require constraint bound {\r\n    voltage <= 28\r\n}"
        raw = response(requirement, evidence=[{"start_line": 1, "end_line": 3, "quote": text}])
        validate_verdict(raw, requirement, text)
        raw["assertions"][0]["evidence"][0]["quote"] = text.replace("\r\n", "\n")
        result = validate_verdict(raw, requirement, text)
        evidence = result["assertions"][0]["evidence"][0]
        self.assertEqual(evidence["normalization"], "code_tokens")
        self.assertEqual(evidence["quote"], text)
        self.assertEqual(evidence["raw_quote"], text.replace("\r\n", "\n"))

    def test_first_line_indentation_is_normalized_with_both_proof_records(self):
        requirement = suite()["requirements"][0]
        quote = SYSML.splitlines()[7].lstrip()
        raw = response(requirement, evidence=[{"start_line": 8, "end_line": 8, "quote": quote}])
        before = deepcopy(raw)
        result = validate_verdict(raw, requirement, SYSML)
        span = result["assertions"][0]["evidence"][0]
        self.assertEqual(raw, before)
        self.assertEqual(span["raw_quote"], quote)
        self.assertEqual(span["quote"], SYSML.splitlines(keepends=True)[7])
        self.assertEqual(span["normalization"], "code_tokens")

    def test_omitted_source_comment_and_spacing_preserve_code_tokens(self):
        requirement = suite()["requirements"][0]
        text = '''requirement R1 {
    doc /* Source: {"text": "The voltage limit is 28 V", "nested": "long context"} */
    require constraint bound { voltage <= 28 }
}'''
        quote = "requirement R1 {\nrequire constraint bound {voltage <= 28}\n}"
        raw = response(requirement, evidence=[{"start_line": 1, "end_line": 4, "quote": quote}])
        span = validate_verdict(raw, requirement, text)["assertions"][0]["evidence"][0]
        self.assertEqual(span["quote"], text)
        self.assertEqual(span["raw_quote"], quote)
        self.assertEqual(span["normalization"], "code_tokens")

    def test_tokens_literals_and_quoted_whitespace_cannot_change(self):
        requirement = suite()["requirements"][0]
        cases = [
            ("require constraint bound { voltage <= 28 }", "require constraint bound { current <= 28 }"),
            ("require constraint bound { voltage <= 28 }", "require constraint bound { voltage <= 30 }"),
            ("require constraint bound { voltage <= 28 }", "require constraint bound { voltage < = 28 }"),
            ("require constraint bound { voltage <= 28 }", "requireconstraint bound { voltage <= 28 }"),
            ("attribute/*boundary*/voltage : Real;", "attributevoltage : Real;"),
            ("attribute voltage : Real = 28;", "attribute voltage : Real = 2 8;"),
            ('attribute label : String = "two  spaces";', 'attribute label : String = "two spaces";'),
            ("attribute 'two  spaces' : Real;", "attribute 'two spaces' : Real;"),
            ('attribute label : String = "https://a/*b*/";', 'attribute label : String = "https://a";'),
            ('attribute label : String = "a\\\\b";', 'attribute label : String = "ab";'),
        ]
        for text, quote in cases:
            raw = response(requirement, evidence=[{"start_line": 1, "end_line": 1, "quote": quote}])
            with self.subTest(text=text, quote=quote), self.assertRaisesRegex(ValueError, "exactly match"):
                validate_verdict(raw, requirement, text)
        text = '    attribute label : String = "two  spaces";'
        quote = 'attribute   label : String = "two  spaces";'
        raw = response(requirement, evidence=[{"start_line": 1, "end_line": 1, "quote": quote}])
        self.assertEqual(validate_verdict(raw, requirement, text)["assertions"][0]["status"], "pass")

    def test_partial_multiline_literals_and_blank_disclosures_are_not_normalized(self):
        requirement = suite()["requirements"][0]
        text = 'attribute label : String = "first\n  second line\nlast";'
        raw = response(requirement, evidence=[{"start_line": 2, "end_line": 2, "quote": "second line"}])
        with self.assertRaisesRegex(ValueError, "exactly match"):
            validate_verdict(raw, requirement, text)
        requirement = deepcopy(requirement)
        requirement["assertions"][0]["category"] = "unsupported_semantics"
        raw = response(requirement, status="unresolved", evidence=[])
        raw["assertions"][0].update(status="pass", evidence=[
            {"start_line": 2, "end_line": 2, "quote": "   "}])
        with self.assertRaisesRegex(ValueError, "meaningful SysML"):
            validate_verdict(raw, requirement, "doc /* Unsupported. */\n   \n")

    def test_documentation_indentation_preserves_disclosure_text(self):
        requirement = deepcopy(suite()["requirements"][0])
        requirement["assertions"][0]["category"] = "unsupported_semantics"
        text = "    doc /* Unsupported: the temporal obligation is not modeled. */"
        raw = response(requirement, status="unresolved", evidence=[])
        raw["assertions"][0].update(status="pass", evidence=[
            {"start_line": 1, "end_line": 1, "quote": text.lstrip()}])
        span = validate_verdict(raw, requirement, text)["assertions"][0]["evidence"][0]
        self.assertEqual(span["normalization"], "documentation_indentation")
        self.assertEqual(span["quote"], text)
        for quote in ("", "doc /* */", text.replace("not modeled", "modeled")):
            raw["assertions"][0]["evidence"][0]["quote"] = quote
            with self.subTest(quote=quote), self.assertRaisesRegex(ValueError, "exactly match"):
                validate_verdict(raw, requirement, text)

    def test_comment_and_documentation_only_evidence_cannot_pass(self):
        requirement = suite()["requirements"][0]
        for text, line in (
            ("doc /* voltage <= 28 */", 1),
            ("// require constraint { voltage <= 28 }", 1),
            ("/* opened before evidence\nrequire constraint { voltage <= 28 }\n*/", 2),
            ("doc requirement_text about R1 /* source text\nvoltage <= 28\n*/", 2),
            ("comment /* faithful */ }", 1),
        ):
            quoted = text.splitlines()[line - 1]
            raw = response(requirement, evidence=[{"start_line": line, "end_line": line, "quote": quoted}])
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "actual code evidence"):
                validate_verdict(raw, requirement, text)

    def test_comment_delimiters_inside_strings_do_not_hide_following_code(self):
        requirement = suite()["requirements"][0]
        text = '''attribute endpoint : String = "https://device/*name*/";
require constraint bound { 'voltage//value' <= 28 }'''
        raw = response(requirement, evidence=[{"start_line": 2, "end_line": 2, "quote": text.splitlines()[1]}])
        self.assertEqual(validate_verdict(raw, requirement, text)["assertions"][0]["status"], "pass")
        mixed = "doc /* requirement */ require constraint { voltage <= 28 }"
        raw = response(requirement, evidence=[{"start_line": 1, "end_line": 1, "quote": mixed}])
        validate_verdict(raw, requirement, mixed)

    def test_unsupported_disclosure_can_pass_with_docs_but_coverage_cannot(self):
        packet = [{"id": "R1", "text": "The alarm shall eventually activate."}]
        rows = [{"id": "R1", "assertions": [{"id": "R1_DISCLOSURE", "category": "unsupported_semantics",
                 "statement": "The unsupported unbounded temporal obligation is explicitly disclosed.",
                 "source_basis": [{"source_id": "R1", "quote": "eventually activate"}]}]}]
        requirement = legacy_build_suite(packet, rows)["requirements"][0]
        text = "doc /* Unsupported: unbounded eventual activation is not represented. */"
        evidence = [{"start_line": 1, "end_line": 1, "quote": text}]
        raw = response(requirement, status="unresolved", evidence=[])
        raw["assertions"][0].update(status="pass", evidence=evidence)
        raw["assertions"][1].update(status="fail", rationale="The temporal obligation is documented but not modeled.")
        result = validate_verdict(raw, requirement, text)
        self.assertEqual([a["status"] for a in result["assertions"]], ["pass", "fail", "unresolved"])
        self.assertEqual(assertion_metrics(a["status"] for a in result["assertions"])["pass_rate"], 1 / 3)
        for index in (1, 2):
            wrong = deepcopy(raw)
            wrong["assertions"][index].update(status="pass", evidence=evidence)
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "actual code evidence"):
                validate_verdict(wrong, requirement, text)
        wrong = deepcopy(raw)
        wrong["assertions"][0]["evidence"] = []
        with self.assertRaisesRegex(ValueError, "disclosure assertions need exact evidence"):
            validate_verdict(wrong, requirement, text)
        wrong = deepcopy(raw)
        wrong["assertions"][0]["evidence"][0]["quote"] = "Invented disclosure."
        with self.assertRaisesRegex(ValueError, "exactly match"):
            validate_verdict(wrong, requirement, text)

    def test_fail_and_unresolved_can_explain_missing_content_without_citations(self):
        requirement = suite()["requirements"][0]
        for status in ("fail", "unresolved"):
            raw = response(requirement, status=status, evidence=[])
            raw["assertions"][0]["counterexample"] = "The source prohibits 29 V but no voltage constraint exists."
            self.assertEqual(validate_verdict(raw, requirement, "package Empty {}")["assertions"][0]["status"], status)

    def test_incomplete_duplicate_added_or_self_scored_verdict_is_rejected(self):
        requirement = suite()["requirements"][0]
        for change in (
            lambda r: r["assertions"].pop(),
            lambda r: r["assertions"].append(deepcopy(r["assertions"][0])),
            lambda r: r["assertions"][0].update(id="invented"),
            lambda r: r.update(pass_rate=1),
            lambda r: r["assertions"][0].update(status="N/A"),
            lambda r: r["assertions"][0].update(counterexample="The limit is wrong."),
            lambda r: r["assertions"][0].update(evidence=[]),
            lambda r: r["assertions"][0].update(rationale=""),
        ):
            raw = response(requirement)
            change(raw)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_verdict(raw, requirement, SYSML)

    def test_large_evidence_span_is_rejected(self):
        requirement = suite()["requirements"][0]
        text = "\n".join("attribute voltage : Real;" for _ in range(MAX_EVIDENCE_LINES + 1))
        raw = response(requirement, evidence=[{"start_line": 1, "end_line": MAX_EVIDENCE_LINES + 1, "quote": text}])
        with self.assertRaisesRegex(ValueError, "bounded"):
            validate_verdict(raw, requirement, text)


class PartialAssertionResponseTests(unittest.TestCase):
    def setUp(self):
        self.requirement = suite()["requirements"][0]
        self.ids = [a["id"] for a in self.requirement["assertions"]]

    def test_all_valid_retains_strict_verdict_fields_without_mutating_inputs(self):
        raw = response(self.requirement)
        strict = validate_verdict(raw, self.requirement, SYSML)
        raw["assertions"].reverse()
        original_raw, original_requirement = deepcopy(raw), deepcopy(self.requirement)
        actual = validate_assertion_response(raw, self.requirement, SYSML)
        self.assertEqual(actual["assertions"], strict["assertions"])
        self.assertEqual(raw, original_raw)
        self.assertEqual(self.requirement, original_requirement)
        self.assertEqual(actual["validation"], {
            "policy": "per_assertion/1", "status": "completed", "planned": 3,
            "evidence_policy": "assertion_evidence/1",
            "accepted": 3, "rejected": 0, "assertion_errors": [], "response_errors": []})

    def test_comment_only_citation_rejects_only_its_assertion(self):
        raw = response(self.requirement)
        raw["assertions"][1]["evidence"] = [{"start_line": 7, "end_line": 7}]
        result = validate_assertion_response(raw, self.requirement, SYSML)
        self.assertEqual([a["id"] for a in result["assertions"]], [self.ids[0], self.ids[2]])
        self.assertEqual([a["status"] for a in result["assertions"]], ["pass", "pass"])
        validation = result["validation"]
        self.assertEqual((validation["status"], validation["planned"],
                          validation["accepted"], validation["rejected"]), ("partial", 3, 2, 1))
        self.assertEqual(validation["response_errors"], [])
        self.assertEqual(validation["assertion_errors"], [{
            "assertion_id": self.ids[1],
            "error": "Passed assertions need actual code evidence, not only documentation/comments"}])
        # The original strict policy remains strict for existing callers.
        with self.assertRaisesRegex(ValueError, "actual code evidence"):
            validate_verdict(raw, self.requirement, SYSML)

    def test_fail_and_unresolved_siblings_survive_an_invalid_pass(self):
        raw = response(self.requirement)
        raw["assertions"][0].update(status="fail", evidence=[], counterexample="29 V is permitted.")
        raw["assertions"][1].update(status="unresolved", evidence=[])
        raw["assertions"][2]["evidence"] = []
        result = validate_assertion_response(raw, self.requirement, SYSML)
        self.assertEqual([a["status"] for a in result["assertions"]], ["fail", "unresolved"])
        self.assertEqual(result["assertions"][0]["counterexample"], "29 V is permitted.")
        self.assertEqual(result["validation"]["accepted"], 2)
        self.assertEqual(result["validation"]["rejected"], 1)

    def test_missing_verdict_does_not_discard_other_assertions_or_shrink_plan(self):
        raw = response(self.requirement)
        raw["assertions"].pop(1)
        result = validate_assertion_response(raw, self.requirement, SYSML)
        self.assertEqual([a["id"] for a in result["assertions"]], [self.ids[0], self.ids[2]])
        self.assertEqual(result["validation"]["planned"], 3)
        self.assertEqual(result["validation"]["assertion_errors"], [{
            "assertion_id": self.ids[1], "error": "Judge response omitted frozen assertion"}])

    def test_duplicate_known_id_invalidates_all_versions_independent_of_order(self):
        raw = response(self.requirement)
        duplicate = deepcopy(raw["assertions"][0])
        duplicate.update(status="fail", evidence=[], rationale="A contradictory judgment.")
        raw["assertions"].append(duplicate)
        for rows in (raw["assertions"], list(reversed(raw["assertions"]))):
            with self.subTest(first=rows[0]["status"]):
                result = validate_assertion_response({**raw, "assertions": rows}, self.requirement, SYSML)
                self.assertEqual([a["id"] for a in result["assertions"]], self.ids[1:])
                self.assertEqual(result["validation"]["rejected"], 1)
                self.assertIn("duplicated", result["validation"]["assertion_errors"][0]["error"])

    def test_unknown_and_unidentifiable_rows_are_recorded_without_poisoning_known_ids(self):
        raw = response(self.requirement)
        raw["assertions"].extend([
            {**deepcopy(raw["assertions"][0]), "id": "UNPLANNED"},
            None, "not an assertion", {"status": "pass"}, {"id": ["not", "string"]},
        ])
        result = validate_assertion_response(raw, self.requirement, SYSML)
        self.assertEqual([a["id"] for a in result["assertions"]], self.ids)
        validation = result["validation"]
        self.assertEqual((validation["planned"], validation["accepted"], validation["rejected"]), (3, 3, 0))
        self.assertEqual(validation["status"], "partial")
        self.assertEqual(validation["assertion_errors"], [])
        self.assertEqual([e["row_index"] for e in validation["response_errors"]], list(range(3, 8)))
        self.assertEqual([e["assertion_id"] for e in validation["response_errors"]],
                         ["UNPLANNED", None, None, None, None])

    def test_malformed_row_with_known_id_is_rejected_individually(self):
        raw = response(self.requirement)
        raw["assertions"][1] = {"id": self.ids[1]}
        result = validate_assertion_response(raw, self.requirement, SYSML)
        self.assertEqual([a["id"] for a in result["assertions"]], [self.ids[0], self.ids[2]])
        self.assertEqual(result["validation"]["response_errors"], [])
        self.assertEqual(result["validation"]["assertion_errors"][0]["assertion_id"], self.ids[1])

    def test_invalid_evidence_and_verdict_fields_do_not_become_pass_or_fail(self):
        mutations = [
            lambda a: a.update(status="unsupported"),
            lambda a: a.update(rationale=""),
            lambda a: a.update(counterexample="Contradiction despite pass."),
            lambda a: a.update(extra_field=True),
            lambda a: a.update(evidence=[{"start_line": 8, "end_line": 8,
                                         "quote": SYSML.splitlines()[7].replace("28", "30")}]),
            lambda a: a.update(evidence=[{"start_line": True, "end_line": 8}]),
            lambda a: a.update(evidence=[{"start_line": 8, "end_line": 999}]),
            lambda a: a.update(evidence=[{"start_line": 8, "end_line": 7}]),
            lambda a: a.update(evidence=[{"start_line": 0, "end_line": 8}]),
            lambda a: a.update(evidence=[{"start_line": 8, "end_line": 8, "quote": None}]),
            lambda a: a.update(evidence=[{"start_line": 8, "end_line": 8}] * (MAX_EVIDENCE_SPANS + 1)),
        ]
        for mutation in mutations:
            raw = response(self.requirement)
            mutation(raw["assertions"][0])
            with self.subTest(verdict=raw["assertions"][0]):
                result = validate_assertion_response(raw, self.requirement, SYSML)
                self.assertEqual([a["id"] for a in result["assertions"]], self.ids[1:])
                self.assertEqual(result["validation"]["assertion_errors"][0]["assertion_id"], self.ids[0])
                self.assertEqual(result["validation"]["rejected"], 1)

    def test_no_accepted_verdicts_reports_failed_with_original_denominator(self):
        for raw in (response(self.requirement, evidence=[]),
                    {"requirement_id": "R1", "assertions": []}):
            with self.subTest(raw=raw):
                result = validate_assertion_response(raw, self.requirement, SYSML)
                self.assertEqual(result["assertions"], [])
                self.assertEqual(result["validation"]["status"], "failed")
                self.assertEqual((result["validation"]["planned"], result["validation"]["accepted"],
                                  result["validation"]["rejected"]), (3, 0, 3))

    def test_wrong_target_or_bad_envelope_remains_fatal(self):
        good = response(self.requirement)
        for raw in (None, [], "not JSON", {**good, "requirement_id": "R2"},
                    {**good, "assertions": {}}, {**good, "pass_rate": 1},
                    {"assertions": good["assertions"]}):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "target requirement"):
                validate_assertion_response(raw, self.requirement, SYSML)

    def test_disclosure_exception_and_source_line_hydration_stay_unchanged(self):
        requirement = deepcopy(self.requirement)
        requirement["assertions"][0]["category"] = "unsupported_semantics"
        raw = response(requirement)
        raw["assertions"][0]["evidence"] = [{"start_line": 7, "end_line": 7}]
        result = validate_assertion_response(raw, requirement, SYSML)
        self.assertEqual(result["validation"]["status"], "completed")
        evidence = result["assertions"][0]["evidence"][0]
        self.assertEqual(evidence["normalization"], "source_lines")
        self.assertEqual(evidence["quote"], SYSML.splitlines(keepends=True)[6])


class AssertionMetricTests(unittest.TestCase):
    def test_failed_and_absent_judgments_stay_in_planned_denominator(self):
        result = assertion_metrics(["pass", "fail", "unresolved", "unreviewed"])
        self.assertEqual(result, {"planned": 4, "pass": 1, "fail": 1, "unresolved": 1,
            "unreviewed": 1, "pass_rate": 0.25, "resolved_pass_rate": 0.5, "coverage": 0.5})
        missing = assertion_metrics(["unreviewed"] * 3)
        self.assertEqual(missing["pass_rate"], 0)
        self.assertIsNone(missing["resolved_pass_rate"])
        self.assertEqual(missing["coverage"], 0)

    def test_empty_denominators_are_null_and_unknown_status_not_dropped(self):
        empty = assertion_metrics([])
        self.assertEqual(empty["planned"], 0)
        self.assertTrue(all(empty[key] is None for key in ("pass_rate", "resolved_pass_rate", "coverage")))
        for status in ("N/A", "error", None, {}, "faithful"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                assertion_metrics([status])


if __name__ == "__main__":
    unittest.main()
