"""Revalidate frozen judge replies without inference or changes to original results.

This is a new scoring-policy analysis of existing evidence, not a new trial. The
source packet, assertions, model text and raw judge responses remain fixed.
"""
from __future__ import annotations

from copy import deepcopy
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re

from canonical_cli import _strip_fence, read_json, write_json
from canonical_assertions import (EVIDENCE_POLICY, assertion_evidence_requirements,
                                  assertion_metrics, validate_suite)
import canonical_assertion_judging as judging


def _views(report, packet_ids):
    """Keep original condition/admission metadata, not stale assessment metrics."""
    views, seen = [], set()
    for observation in report.get("observations", []):
        # Observations also contain requirement-specific scores. Recover only
        # the frozen study attribution so each packet is expanded once per
        # condition/repetition, independent of present or future metric fields.
        view = {key: deepcopy(observation[key]) for key in
                ("repetition", "condition", "packet_id", "artifact_arm", "admission")
                if key in observation}
        if view.get("packet_id") not in packet_ids:
            raise ValueError("Saved observation references an unknown packet")
        key = json.dumps(view, sort_keys=True)
        if key not in seen:
            seen.add(key)
            views.append(view)
    return views


def _original_rows(report, packets, suite, models):
    """Validate identity and indexing before creating any new output."""
    if report.get("judge_models") != models:
        raise ValueError("Saved report and configuration disagree on judge models")
    rows = {}
    packet_ids = {packet["id"] for packet in packets}
    requirement_ids = {row["id"] for row in suite["requirements"]}
    results = report.get("results")
    if not isinstance(results, list):
        raise ValueError("Saved report has no results array")
    for packet in results:
        pid = packet.get("packet_id")
        if pid not in packet_ids or pid in rows:
            raise ValueError("Saved report has duplicate or unknown packets")
        rows[pid] = {}
        for row in packet.get("requirements", []):
            rid = row.get("requirement_id")
            if rid not in requirement_ids or rid in rows[pid]:
                raise ValueError("Saved report has duplicate or unknown requirements")
            calls = row.get("judges")
            if (not isinstance(calls, list) or len(calls) != 2
                    or any(call.get("judge") != slot or call.get("model") != models[slot - 1]
                           for slot, call in enumerate(calls, 1))):
                raise ValueError("Saved report has inconsistent judge slots")
            assertions = row.get("assertions")
            expected_ids = {a["id"] for r in suite["requirements"] if r["id"] == rid for a in r["assertions"]}
            if (not isinstance(assertions, list) or any(not isinstance(a, dict) for a in assertions)
                    or len(assertions) != len(expected_ids)
                    or {a.get("id") for a in assertions} != expected_ids
                    or any(not isinstance(a.get("judge_statuses"), list)
                           or len(a["judge_statuses"]) != 2
                           or any(s not in {"pass", "fail", "unresolved", "unreviewed"} for s in a["judge_statuses"])
                           or not isinstance(a.get("agreement"), dict) for a in assertions)):
                raise ValueError("Saved report must retain every planned assertion exactly once with two judge slots")
            rows[pid][rid] = row
        if set(rows[pid]) != requirement_ids:
            raise ValueError("Saved report omitted planned requirements")
    if set(rows) != packet_ids:
        raise ValueError("Saved report omitted planned packets")
    return rows


def _bound_prompt(call, requirement, packet, suite, configuration):
    prompt = read_json(call / "prompt.json")
    expected = {
        "target_requirement_id": requirement["id"],
        "source_packet": suite["sources"],
        "fixed_context": suite.get("fixed_context"),
        "assertions": requirement["assertions"],
        "sysml_with_line_numbers": "".join(
            f"{index + 1}: {line}" for index, line in enumerate(packet["sysml"].splitlines(keepends=True))),
    }
    if not isinstance(prompt, dict):
        raise ValueError("Saved judge prompt is not an object")
    for key, value in expected.items():
        if key not in prompt or prompt[key] != value:
            raise ValueError(f"Saved judge prompt differs from frozen inputs: {key}")
    policy = configuration.get("abstraction_policy")
    if policy is not None and prompt.get("abstraction_policy") != policy:
        raise ValueError("Saved judge prompt differs from frozen abstraction policy")
    return prompt


LEADING_BRACE_POLICY = "single_leading_brace_recovery/1"


def _strict_json(text):
    """Use the live parser's duplicate-key and finite-number restrictions."""
    def unique(pairs):
        value = {}
        for key, child in pairs:
            if key in value:
                raise ValueError(f"Duplicate JSON key: {key}")
            value[key] = child
        return value
    def finite_float(token):
        value = float(token)
        if not math.isfinite(value):
            raise ValueError("Non-finite JSON number")
        return value
    return json.loads(text, object_pairs_hook=unique, parse_float=finite_float,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))


def _exact_text(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return stream.read()


def _completed_provider_text(call, original_text):
    """Recovery requires exactly one original, completed Bedrock reply."""
    paths = sorted(call.glob("bedrock_calls/*/judgment/result.json"))
    if len(paths) != 1:
        raise ValueError("Leading-brace recovery requires exactly one original provider result")
    result = read_json(paths[0])
    if not isinstance(result, dict) or result.get("status") != "ok" or result.get("stop_reason") != "end_turn":
        raise ValueError("Leading-brace recovery requires a completed provider response (ok/end_turn)")
    if result.get("text") != original_text:
        raise ValueError("Original provider text and saved raw response disagree")
    return str(paths[0].resolve())


def _reply(call, *, recover_leading_brace=False, requirement_id=None, audit=None):
    """Strict-first replay; an opt-in correction removes one character only.

    Original files stay untouched. The caller records the proposed parse and
    still applies the normal target and per-assertion evidence validation.
    """
    audit = {} if audit is None else audit
    audit.update(status="rejected", original_parse_status="not_attempted", recovery_attempted=False)
    text_path, json_path = call / "response.txt", call / "response.json"
    if text_path.is_file():
        original_text = _exact_text(text_path)
        normalized = _strip_fence(original_text)
        audit["original_parse_status"] = "failed"
        try:
            raw = _strict_json(normalized)
        except json.JSONDecodeError as exc:
            audit["original_parse_error"] = str(exc)
            prefix = re.match(r"^\{[ \t\r\n]*\{", normalized)
            # Never extract a substring, balance braces, choose among objects,
            # or recover duplicate/nonfinite JSON errors by changing content.
            if (not recover_leading_brace or prefix is None or exc.pos != prefix.end() - 1
                    or normalized != original_text.strip(" \t\r\n")):
                raise
            audit["recovery_attempted"] = True
            audit["provider_result"] = _completed_provider_text(call, original_text)
            if json_path.is_file() and _exact_text(json_path) != normalized:
                raise ValueError("Saved raw and response.json text disagree before parsing recovery")
            offset = len(original_text) - len(original_text.lstrip(" \t\r\n"))
            recovered_text = original_text[:offset] + original_text[offset + 1:]
            raw = _strict_json(recovered_text)
            if (not isinstance(raw, dict) or set(raw) != {"requirement_id", "assertions"}
                    or raw["requirement_id"] != requirement_id or not isinstance(raw["assertions"], list)):
                raise ValueError("Recovered object must match the frozen judgment root and target requirement")
            audit.update(status="recovered", removed_character="{", removed_offset_in_original_text=offset,
                         normalization="None: exactly one original character removed; surrounding whitespace preserved",
                         recovered_text=recovered_text)
            return raw
        audit["original_parse_status"] = "passed"
        if json_path.is_file() and _strict_json(_exact_text(json_path)) != raw:
            raise ValueError("Saved raw and decoded judge responses disagree")
        audit["status"] = "strict"
        return raw
    if json_path.is_file():
        audit["original_parse_status"] = "failed"
        raw = _strict_json(_exact_text(json_path))
        audit.update(status="strict", original_parse_status="passed")
        return raw
    raise FileNotFoundError("No saved judge response is available; no new call was made")


def _failed(requirement, slot, model, error, code):
    return {"judge": slot, "model": model, "status": "failed", "error": error,
            "reason_code": code,
            "metrics": assertion_metrics(["unreviewed"] * len(requirement["assertions"]))}


def _score_snapshot(report):
    return deepcopy({key: report[key] for key in (
        "status", "summary", "metrics", "joint", "per_judge", "by_category",
        "by_condition", "calibration_summary", "assertion_suite_schema", "evaluation_policy", "fidelity",
        "evidence_policy", "assertion_evidence_requirements", "by_evidence_requirement") if key in report})


def _joint_status(row):
    return next((key for key in ("pass", "fail", "disputed", "unresolved", "unreviewed")
                 if row["agreement"].get(key)), "unreviewed")


def _write_comparison(directory, report, original, original_rows):
    changes = []
    csv_rows = []
    conditions = {}
    for observation in report.get("observations", []):
        conditions.setdefault(observation["packet_id"], set()).add(observation["condition"])
    for packet in report["results"]:
        pid = packet["packet_id"]
        for row in packet["requirements"]:
            rid = row["requirement_id"]
            old = {a["id"]: a for a in original_rows[pid][rid]["assertions"]}
            for assertion in row["assertions"]:
                aid = assertion["id"]
                before = old[aid]
                after_statuses = assertion["judge_statuses"]
                record = {"packet_id": pid, "conditions": ",".join(sorted(conditions.get(pid, []))),
                          "requirement_id": rid, "assertion_id": aid,
                          "category": report["assertion_categories"][aid],
                          "old_judge_1": before["judge_statuses"][0],
                          "old_judge_2": before["judge_statuses"][1],
                          "new_judge_1": after_statuses[0], "new_judge_2": after_statuses[1],
                          "judge_1_error": assertion.get("judge_errors", [None, None])[0],
                          "judge_2_error": assertion.get("judge_errors", [None, None])[1],
                          "old_joint": _joint_status(before), "new_joint": _joint_status(assertion)}
                csv_rows.append(record)
                if before["judge_statuses"] != after_statuses:
                    changes.append(record)
    fields = ["packet_id", "conditions", "requirement_id", "assertion_id", "category", "old_judge_1",
              "old_judge_2", "new_judge_1", "new_judge_2", "judge_1_error", "judge_2_error", "old_joint", "new_joint"]
    with (directory / "assertions.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(csv_rows)
    parsing_correction = report["rescore"]["parse_recovery"]["enabled"]
    comparison = {"schema": "assertion_rescore_comparison/1", "old": _score_snapshot(original),
                  "new": _score_snapshot(report), "changed_assertions": changes,
                  "changed_assertion_count": len(changes),
                  "interpretation": ("Secondary offline parsing correction on identical frozen replies; original assessment remains primary. No verdict edits, generation or judge rerun."
                                     if parsing_correction else
                                     "Scoring-policy change on identical frozen replies; no generation or judge rerun.")}
    write_json(directory / "comparison.json", comparison)
    lines = ["# Offline assertion rescoring", "",
             "Valid individual verdicts are retained. Invalid individual verdicts remain unreviewed; "
             "they are not converted to passes or semantic failures. All planned assertions remain in the denominator.", "",
             "This changes the scoring policy for existing frozen replies. It is not a new experiment or additional independent evidence.", "",
             "| Condition | Old joint passes | New joint passes | Planned | Old unreviewed | New unreviewed |",
             "|---|---:|---:|---:|---:|---:|"]
    old_groups = original.get("by_condition", {"All packets": {"joint": original["joint"]}})
    new_groups = report.get("by_condition", {"All packets": {"joint": report["joint"]}})
    for group, value in new_groups.items():
        before, after = old_groups[group]["joint"], value["joint"]
        lines.append(f"| {group} | {before['pass']} | {after['pass']} | {after['planned']} | "
                     f"{before['unreviewed']} | {after['unreviewed']} |")
    lines += ["", "No new provider calls or charges. Original token/cost records remain in the original assessment tree and are linked in rescore.json.",
              "", "See judgments.json for per-assertion diagnostics, assertions.csv for every planned assertion, "
              "comparison.json for old/new aggregates, and original_call.json beside each rescored assessment for its frozen source.",
              "", "configuration.json is the unchanged original inference configuration. The new validation and evidence policies are recorded in rescore.json and judgments.json. Frozen assertion evidence requirements are preserved; legacy assertions retain their original model-evidence default except unsupported-semantics disclosure."]
    if parsing_correction:
        lines += ["", "The original assessment remains primary evidence. This optional secondary analysis removes exactly one surplus leading opening brace only from an otherwise complete strict JSON judgment with matching completed provider text. It never changes verdict content or the existing assertion validator. The same rule is applied to every saved response, without selecting by score. See parse_recovery.json beside each response and aggregate parsing counts in rescore.json. Original response files remain verbatim; a recovered_response.json file records the separately parsed text only when recovery occurred."]
    (directory / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def rescore_assessment(assessment_dir, output_dir, *, recover_leading_brace=False):
    """Retain valid assertion verdicts from already saved independent judge calls.

    Never invokes a model, edits the original assessment, copies provider billing
    records as new calls, or retries an original skipped/failed provider request.
    """
    if type(recover_leading_brace) is not bool:
        raise ValueError("recover_leading_brace must be Boolean")
    source, directory = Path(assessment_dir).resolve(), Path(output_dir).resolve()
    if source == directory or source in directory.parents or directory in source.parents:
        raise ValueError("Original and rescored assessment directories must not overlap")
    if directory.exists():
        raise FileExistsError(f"Rescore output already exists: {directory}")
    suite = validate_suite(read_json(source / "assertions.json"))
    packets = judging._validate_packets(read_json(source / "packets.json"), suite)
    configuration = read_json(source / "configuration.json")
    models = configuration.get("judge_models")
    if not isinstance(models, list) or len(models) != 2 or any(not isinstance(m, str) or not m for m in models):
        raise ValueError("Saved configuration must identify two judge models")
    original = read_json(source / "judgments.json")
    original_rows = _original_rows(original, packets, suite, models)
    views = _views(original, {p["id"] for p in packets})
    report = deepcopy(original)
    report["assertion_suite_schema"] = suite["schema"]
    report["results"], report["calibration"] = [], []
    report["validation_policy"] = "per_assertion/1"
    report["evidence_policy"] = EVIDENCE_POLICY
    report["assertion_evidence_requirements"] = assertion_evidence_requirements(suite)
    for key in ("observations", "by_condition"):
        report.pop(key, None)
    categories = {a["id"]: a["category"] for r in suite["requirements"] for a in r["assertions"]}
    if report.get("assertion_categories") != categories:
        raise ValueError("Saved report differs from frozen assertion categories")
    if ("assertion_evidence_requirements" in original
            and original["assertion_evidence_requirements"] != report["assertion_evidence_requirements"]):
        raise ValueError("Saved report differs from frozen assertion evidence requirements")
    directory.mkdir(parents=True, exist_ok=False)
    for name in ("assertions.json", "packets.json", "configuration.json", "rubric.txt"):
        if (source / name).is_file():
            (directory / name).write_bytes((source / name).read_bytes())
    metadata = {"schema": "assertion_rescore/1", "created_at": datetime.now(timezone.utc).isoformat(),
                "original_assessment_dir": str(source), "original_report": str(source / "judgments.json"),
                "original_configuration": str(source / "configuration.json"),
                "configuration_snapshot_role": "configuration.json records the original inference configuration, not the new validation or evidence policies.",
                "original_validation_policy": original.get("validation_policy", "legacy_all_or_nothing/1"),
                "original_evidence_policy": original.get("evidence_policy", "legacy_code_except_disclosure/1"),
                "original_parse_recovery": deepcopy(original.get("parse_recovery")),
                "evidence_policy": EVIDENCE_POLICY,
                "assertion_evidence_requirements": assertion_evidence_requirements(suite),
                "policy": "per_assertion/1", "new_calls": 0, "added_cost_usd": 0,
                "raw_replies_changed": False, "candidates_changed": False,
                "assertion_suite_schema": suite["schema"], "assertions_added": 0,
                "rubric_changed": False,
                "parse_recovery": {"enabled": recover_leading_brace,
                    "policy": LEADING_BRACE_POLICY if recover_leading_brace else "strict_json/1",
                    "scope": "Secondary offline parsing correction; original assessment remains primary. No verdict edits or score-based selection.",
                    "counts": {"planned_responses": 0, "original_parse_passed": 0, "original_parse_failed": 0,
                               "recovered_responses": 0, "strict_responses": 0, "rejected_responses": 0,
                               "not_attempted_responses": 0}},
                "cost_accounting": "Reuse original provider records; copied evidence is not a new provider call.",
                "original_provider_records": [str(p) for p in sorted(source.glob("*/requirement-*/judge-*/bedrock_calls"))]}
    report["rescore"] = metadata
    for packet in packets:
        pid = packet["id"]
        old_packet = next(p for p in original["results"] if p["packet_id"] == pid)
        result = {key: deepcopy(value) for key, value in old_packet.items() if key != "requirements"}
        result["requirements"] = []
        for index, requirement in enumerate(suite["requirements"], 1):
            old_row = original_rows[pid][requirement["id"]]
            calls = []
            for slot, model in enumerate(models, 1):
                relative = Path(pid) / f"requirement-{index:04d}" / f"judge-{slot}"
                old_call, call = source / relative, directory / relative
                call.mkdir(parents=True)
                old_judgment = old_row["judges"][slot - 1]
                parsing = {"schema": "assertion_parse_recovery/1", "policy": metadata["parse_recovery"]["policy"],
                           "enabled": recover_leading_brace, "original_call": str(old_call),
                           "status": "not_attempted", "original_parse_status": "not_attempted",
                           "recovery_attempted": False}
                if old_judgment["status"] == "not_run":
                    judgment = deepcopy(old_judgment)
                else:
                    try:
                        _bound_prompt(old_call, requirement, packet, suite, configuration)
                        raw = _reply(old_call, recover_leading_brace=recover_leading_brace,
                                     requirement_id=requirement["id"], audit=parsing)
                        judgment = judging.assess_response(raw, requirement, packet["sysml"], slot, model)
                    except (ValueError, TypeError, KeyError, OSError) as exc:
                        parsing["error"] = f"{type(exc).__name__}: {exc}"
                        judgment = _failed(requirement, slot, model, f"{type(exc).__name__}: {exc}",
                                           "saved_evidence_invalid_or_unavailable")
                recovered_text = parsing.pop("recovered_text", None)
                if recovered_text is not None:
                    (call / "recovered_response.json").write_text(recovered_text, encoding="utf-8")
                    parsing["recovered_response"] = "recovered_response.json"
                parsing["assessment_status"] = judgment["status"]
                write_json(call / "parse_recovery.json", parsing)
                counts = metadata["parse_recovery"]["counts"]
                counts["planned_responses"] += 1
                if parsing["original_parse_status"] in {"passed", "failed"}:
                    counts["original_parse_" + parsing["original_parse_status"]] += 1
                status_key = {"strict": "strict_responses", "recovered": "recovered_responses",
                              "rejected": "rejected_responses", "not_attempted": "not_attempted_responses"}
                counts[status_key[parsing["status"]]] += 1
                # Preserve raw evidence verbatim. Do not copy bedrock_calls and
                # accidentally create a second usage/cost record for this call.
                for name in ("prompt.json", "response.txt", "response.json"):
                    if (old_call / name).is_file():
                        (call / name).write_bytes((old_call / name).read_bytes())
                write_json(call / "original_call.json", {"directory": str(old_call),
                           "original_status": old_judgment["status"], "new_calls": 0, "added_cost_usd": 0})
                write_json(call / "assessment.json", judgment)
                calls.append(judgment)
            result["requirements"].append(judging._requirement_result(requirement, calls))
        report["results"].append(result)
    # The copied report describes the original inference run. Its public parse
    # summary must describe this replay instead, while retaining the original
    # policy and counts above for comparison. Keep the same summary shape used
    # by live judging; the rescore record retains the more detailed audit counts.
    parsing = metadata["parse_recovery"]
    report["parse_recovery"] = {
        "enabled": parsing["enabled"], "policy": parsing["policy"],
        "additional_calls": 0, "scope": parsing["scope"],
        "counts": {status: parsing["counts"][status + "_responses"]
                   for status in ("strict", "recovered", "rejected", "not_attempted")},
    }
    # Preserve all original labeled controls and metadata; only observed verdicts
    # change. Packet expectations are already validated by _validate_packets.
    lookup = {(p["packet_id"], r["requirement_id"], j["judge"], a["id"]): a["status"]
              for p in report["results"] for r in p["requirements"] for j in r["judges"]
              for a in j.get("assessment", {}).get("assertions", [])}
    for control in original.get("calibration", []):
        updated = deepcopy(control)
        updated["observed"] = lookup.get((control["packet_id"], control["requirement_id"],
                                         control["judge"], control["assertion_id"]), "unreviewed")
        report["calibration"].append(updated)
    judging._summaries(report)
    if "observations" in original:
        judging._study_summaries(report, views)
    judging._write_report(directory, report)
    write_json(directory / "rescore.json", metadata)
    _write_comparison(directory, report, original, original_rows)
    return report
