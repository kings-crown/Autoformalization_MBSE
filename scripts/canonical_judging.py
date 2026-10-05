"""Independent, condition-blind LLM assessment of final SysML and optional judge controls."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

from canonical_cli import _ask, _fixed_context, _strip_fence, read_json, write_json
from canonical_abstractions import POLICY, POLICY_TEXT

LABELS = {"faithful", "partially_faithful", "incorrect", "unresolved"}
RUBRIC = '''Assess the supplied final SysML against the original source requirements and surrounding source context. You are an independent evaluator; do not infer correctness from syntax, comments claiming correctness, model agreement, or absence of solver errors. Assess under the explicitly supplied fixed context when present; its declared domains and assumptions are study inputs, not candidate inventions. Identify additional restrictions introduced by the candidate. Do not invent omitted context. Evaluate conditions, obligations, endpoints, units, modalities and exceptions. Distinguish executable constraints from documentation-only preservation. Record omissions, invented assumptions and unsupported semantics. A wholly undocumented/unrepresented obligation cannot be faithful. Use unresolved when the source interpretation or artifact cannot support a definite judgment. No condition labels, generator identities or solver results are available.
Return one JSON object with exactly a requirements array. Every supplied source ID must appear exactly once. Each entry has exactly:
id, label (faithful|partially_faithful|incorrect|unresolved), justification (nonempty text), omitted_obligations (list of strings), invented_assumptions (list of strings), unsupported_semantics (list of strings), distinguishing_scenario (text or null).
The result is LLM-assessed fidelity, not human-validated ground truth or engineering approval.'''


RUBRIC += "\n\n" + POLICY_TEXT


def _assessment(value, ids):
    if not isinstance(value, dict) or set(value) != {"requirements"} or not isinstance(value["requirements"], list):
        raise ValueError("Judge response needs exactly a requirements array")
    seen = set()
    keys = {"id", "label", "justification", "omitted_obligations", "invented_assumptions", "unsupported_semantics", "distinguishing_scenario"}
    for row in value["requirements"]:
        if (not isinstance(row, dict) or set(row) != keys or not isinstance(row["id"], str)
                or row["id"] not in ids or row["id"] in seen):
            raise ValueError("Judge response must cover every source exactly once with all rubric fields")
        seen.add(row["id"])
        if (not isinstance(row["label"], str) or row["label"] not in LABELS
                or not isinstance(row["justification"], str) or not row["justification"].strip()):
            raise ValueError("Judge labels and justifications must be explicit")
        for field in ("omitted_obligations", "invented_assumptions", "unsupported_semantics"):
            if not isinstance(row[field], list) or any(not isinstance(x, str) or not x.strip() for x in row[field]):
                raise ValueError("Judge evidence fields must be lists of nonempty strings")
        if row["distinguishing_scenario"] is not None and (not isinstance(row["distinguishing_scenario"], str)
                or not row["distinguishing_scenario"].strip()):
            raise ValueError("Distinguishing scenario must be nonempty text or null")
        if row["label"] == "faithful" and (row["omitted_obligations"] or row["invented_assumptions"]):
            raise ValueError("Faithful label contradicts identified omissions or invented assumptions")
    if seen != set(ids):
        raise ValueError("Judge response omitted source IDs")
    return value



def record_policy_alignment(study_dir, output_dir, report):
    """Record generation/assessment policy differences without unblinding judges."""
    path = Path(study_dir) / "study_configuration.json"
    generation = read_json(path).get("abstraction_policy") if path.is_file() else None
    if generation == POLICY:
        status = "matched"
        interpretation = "Generation and assessment used the same recorded abstraction policy."
    elif generation is None:
        status = "legacy_generation_unclassified"
        interpretation = ("Generation recorded no abstraction policy. This is an assessment under the current policy, "
                          "not evidence that the historical generation used that policy.")
    else:
        status = "different_policy"
        interpretation = ("Generation and assessment recorded different abstraction policies. Treat this as a "
                          "separately identified reassessment, not a comparison using one shared policy.")
    alignment = {"status": status, "generation_policy": deepcopy(generation),
                 "assessment_policy": deepcopy(POLICY), "interpretation": interpretation}
    report["policy_alignment"] = alignment
    configuration_path = Path(output_dir) / "configuration.json"
    configuration = read_json(configuration_path) if configuration_path.is_file() else {}
    configuration["abstraction_policy"] = deepcopy(POLICY)
    configuration["policy_alignment"] = deepcopy(alignment)
    write_json(configuration_path, configuration)


def packets_from_study(study_dir):
    """Load separate A/B/C or legacy shared B/C candidates without unblinding.

    Artifact identity is independent of study condition: only identical source,
    fixed context and actual SysML text reuse a packet. Initial candidates and
    generator/solver metadata never enter judge packets.
    """
    directory = Path(study_dir)
    study = read_json(directory / "study.json")
    sources = read_json(directory / "sources.json")
    configuration = directory / "study_configuration.json"
    config = read_json(configuration) if configuration.is_file() else {}
    context = _fixed_context(config.get("context"))
    packets, observations = [], []
    rows = study["rows"]
    if not rows and study.get("status") in {"failed", "interrupted"}:
        # Preparation can fail before arm directories exist. Those planned
        # candidates still belong in assessment denominators; do not silently
        # turn a failed full-document study into zero requirements to assess.
        repetitions = config.get("repetitions", study.get("summary", {}).get("planned_repetitions", 1))
        if type(repetitions) is not int or not 1 <= repetitions <= 50:
            raise ValueError("Failed study has no valid planned repetition count")
        rows = [{"repetition": rep, **{arm: {"status": "failed", "admission": "withheld"}
                 for arm in ("A", "B", "C")}} for rep in range(1, repetitions + 1)]
    for row in rows:
        rep = row["repetition"]
        arms = ("A", "B", "C") if "B" in row or "C" in row or "initial" in row else ("A", "BC")
        for arm in arms:
            model = directory / f"rep-{rep:03d}" / arm / "model.sysml"
            packet_id = None
            outcome = row.get(arm) or {}
            if arm in row and model.is_file():
                # Preserve line endings: normalization must not create false
                # artifact identity or change the text cited by an evaluator.
                with model.open(encoding="utf-8", newline="") as stream:
                    text = stream.read()
                if text.strip():
                    existing = next((p for p in packets if p["requirements"] == sources
                                     and p.get("fixed_context") == context and p["sysml"] == text), None)
                    if existing:
                        packet_id = existing["id"]
                    else:
                        packet_id = f"candidate-{len(packets) + 1:04d}"
                        packet = {"id": packet_id, "requirements": sources, "sysml": text}
                        if context is not None:
                            packet["fixed_context"] = context
                        packets.append(packet)
            for condition in (("B", "C") if arm == "BC" else (arm,)):
                observations.append({"repetition": rep, "condition": condition, "packet_id": packet_id,
                    "artifact_arm": arm,
                    "admission": outcome.get("admission") if condition == "C" else "not_assessed"})
    return packets, observations, sources


def judge_packets(packets, judge_models, output_dir, generator=None):
    """Two separate calls per unique packet; calibration answers never enter prompts."""
    if not isinstance(judge_models, (list, tuple)) or len(judge_models) != 2 or any(not isinstance(m, str) or not m.strip() for m in judge_models):
        raise ValueError("Supply two judge model IDs; calls remain independent even if IDs match")
    if not isinstance(packets, list) or not packets:
        raise ValueError("No candidate packets available to judge")
    packets = deepcopy(packets)
    seen = set()
    for packet in packets:
        if not isinstance(packet, dict) or set(packet) - {"id", "requirements", "sysml", "expected_labels", "fixed_context"}:
            raise ValueError("Invalid judging packet")
        pid = packet.get("id")
        if not isinstance(pid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", pid) or pid in seen:
            raise ValueError("Judging packets need unique safe IDs")
        seen.add(pid)
        if not isinstance(packet.get("sysml"), str) or not packet["sysml"].strip():
            raise ValueError("Judging packet needs the actual SysML text")
        rows = packet.get("requirements")
        if not isinstance(rows, list) or not rows or any(
                not isinstance(r, dict) or not isinstance(r.get("id"), str) or not r["id"].strip()
                or not isinstance(r.get("text"), str) or not r["text"].strip() for r in rows):
            raise ValueError("Judging packet needs original source requirements")
        ids = [r["id"] for r in rows]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate source IDs in judging packet")
        if "fixed_context" in packet:
            packet["fixed_context"] = _fixed_context(packet["fixed_context"])
        expected = packet.get("expected_labels", {})
        if (not isinstance(expected, dict) or set(expected) - set(ids)
                or any(not isinstance(label, str) or label not in LABELS for label in expected.values())):
            raise ValueError("Calibration labels must map known source IDs to rubric labels")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    write_json(directory / "packets.json", packets)
    write_json(directory / "configuration.json", {"judge_models": list(judge_models),
        "assessment": "independent source fidelity labels against actual SysML",
        "semantic_repairs": 0,
        "semantic_repairs_scope": "Evaluator actions only: judging never repairs the frozen candidate. Generator recovery policy and counts belong to generation execution records and are excluded from judge prompts.",
        "abstraction_policy": deepcopy(POLICY)})
    (directory / "rubric.txt").write_text(RUBRIC + "\n", encoding="utf-8")
    results, calibration = [], []
    for packet in packets:
        prompt_fields = {"requirements": [{k: r[k] for k in ("id", "text", "source") if k in r} for r in packet["requirements"]],
                         "sysml": packet["sysml"], "abstraction_policy": deepcopy(POLICY)}
        if packet.get("fixed_context") is not None:
            prompt_fields["fixed_context"] = packet["fixed_context"]
        prompt = json.dumps(prompt_fields, ensure_ascii=False, indent=2)
        row = {"packet_id": packet["id"], "judges": []}
        for index, model in enumerate(judge_models, 1):
            call = directory / packet["id"] / f"judge-{index}"
            call.mkdir(parents=True)
            write_json(call / "prompt.json", prompt_fields)
            judgment = {"index": index, "model": model, "status": "failed"}
            try:
                response = (generator or _ask)(RUBRIC, prompt, model, call, "judgment")
                (call / "response.txt").write_text(response, encoding="utf-8")
                (call / "response.json").write_text(_strip_fence(response), encoding="utf-8")
                assessment = _assessment(read_json(call / "response.json"), {r["id"] for r in packet["requirements"]})
                judgment.update(status="completed", assessment=assessment)
            except Exception as exc:
                judgment["error"] = f"{type(exc).__name__}: {exc}"
            observed = {r["id"]: r["label"] for r in judgment.get("assessment", {}).get("requirements", [])}
            for rid, expected in packet.get("expected_labels", {}).items():
                label = observed.get(rid)
                calibration.append({"judge": index, "packet_id": packet["id"], "requirement_id": rid,
                    "status": judgment["status"], "expected": expected, "observed": label,
                    "label_match": expected == label if label is not None else None})
            write_json(call / "assessment.json", judgment)
            row["judges"].append(judgment)
        results.append(row)
    calibration_summary = []
    for index in (1, 2):
        planned = [r for r in calibration if r["judge"] == index]
        labels = [r for r in planned if r["status"] == "completed"]
        positive_planned = [r for r in planned if r["expected"] in {"incorrect", "partially_faithful"}]
        negative_planned = [r for r in planned if r["expected"] == "faithful"]
        positive = [r for r in positive_planned if r["status"] == "completed"]
        negative = [r for r in negative_planned if r["status"] == "completed"]
        detected = sum(r["observed"] in {"incorrect", "partially_faithful"} for r in positive)
        false_alarms = sum(r["observed"] in {"incorrect", "partially_faithful"} for r in negative)
        accepted = sum(r["observed"] == "faithful" for r in negative)
        definitive_negative = sum(r["observed"] != "unresolved" for r in negative)
        calibration_summary.append({"judge": index,
            "planned_labels": len(planned), "completed_labels": len(labels),
            "failed_labels": len(planned) - len(labels),
            "defect_controls_planned": len(positive_planned),
            "defect_controls_judged": len(positive),
            "defect_controls_failed": len(positive_planned) - len(positive),
            "defects_detected": detected,
            "defect_controls_unresolved": sum(r["observed"] == "unresolved" for r in positive),
            "detection_yield_planned": detected / len(positive_planned) if positive_planned else None,
            "sensitivity_completed": detected / len(positive) if positive else None,
            "faithful_controls_planned": len(negative_planned),
            "faithful_controls_judged": len(negative),
            "faithful_controls_failed": len(negative_planned) - len(negative),
            "faithful_controls_accepted": accepted,
            "faithful_controls_unresolved": sum(r["observed"] == "unresolved" for r in negative),
            "faithful_acceptance_yield_planned": accepted / len(negative_planned) if negative_planned else None,
            "false_alarms": false_alarms,
            "false_alarm_rate_completed": false_alarms / len(negative) if negative else None,
            "false_alarm_rate_definitive": false_alarms / definitive_negative if definitive_negative else None,
            "unresolved": sum(r["observed"] == "unresolved" for r in labels)})
    report = {"schema": "canonical_judgments/1", "status": "completed", "judge_models": judge_models,
              "results": results, "calibration": calibration, "calibration_summary": calibration_summary,
              "abstraction_policy": deepcopy(POLICY),
              "summary": {"unique_packets": len(packets), "planned_calls": 2 * len(packets),
                          "failed_judgments": sum(j["status"] != "completed" for r in results for j in r["judges"]),
                          "planned_requirement_judgments": 2 * sum(len(p["requirements"]) for p in packets),
                          "completed_requirement_judgments": sum(len(j.get("assessment", {}).get("requirements", [])) for r in results for j in r["judges"]),
                          "unresolved_requirement_judgments": sum(a["label"] == "unresolved" for r in results for j in r["judges"] for a in j.get("assessment", {}).get("requirements", []))},
              "claim": "LLM-assessed semantic fidelity under the evaluated cases and judging protocol; no human-validated ground truth.",
              "limitations": ["Judges may share mistakes; agreement is not proof.",
                  "Calibration labels are supplied study references, not automatically established facts.",
                  "Completed-judgment rates exclude failed calls; planned-control yields and failed counts retain those outcomes.",
                  "Unresolved labels count separately and are not accepted controls or detected defects.",
                  "No adjudication or correction is applied; both original judgments are preserved."]}
    write_json(directory / "judgments.json", report)
    return report


def judge_study(study_dir, judge_models, output_dir, generator=None):
    packets, views, sources = packets_from_study(study_dir)
    if packets:
        report = judge_packets(packets, judge_models, output_dir, generator)
    else:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=False)
        report = {"schema": "canonical_judgments/1", "status": "no_candidates", "results": [],
                  "abstraction_policy": deepcopy(POLICY),
                  "summary": {"unique_packets": 0, "planned_calls": 0, "failed_judgments": 0}}
    record_policy_alignment(study_dir, output_dir, report)
    results = {r["packet_id"]: r for r in report["results"]}
    observations = []
    for view in views:
        result = results.get(view["packet_id"], {})
        for source in sources:
            labels = []
            for judge in result.get("judges", []):
                labels.append(next((r["label"] for r in judge.get("assessment", {}).get("requirements", []) if r["id"] == source["id"]), None))
            complete = len(labels) == 2 and all(label is not None for label in labels)
            faithful = complete and all(label == "faithful" for label in labels)
            defective = complete and all(label in {"incorrect", "partially_faithful"} for label in labels)
            unresolved = complete and "unresolved" in labels
            disputed = complete and not (faithful or defective or unresolved)
            state = ("unreviewed" if not complete else "jointly_faithful" if faithful
                     else "jointly_defective" if defective else "unresolved" if unresolved else "disputed")
            observations.append({**view, "requirement_id": source["id"], "judge_labels": labels,
                                 "jointly_faithful": faithful, "jointly_defective": defective,
                                 "complete_judgments": complete, "unresolved": unresolved, "disputed": disputed,
                                 "label_disagreement": complete and labels[0] != labels[1],
                                 "assessment_status": state})
    report["observations"] = observations
    report["by_condition"] = {}
    for condition in ("A", "B", "C"):
        rows = [r for r in observations if r["condition"] == condition]
        report["by_condition"][condition] = {"planned_source_observations": len(rows),
            "jointly_faithful": sum(r["jointly_faithful"] for r in rows),
            "jointly_defective": sum(r["jointly_defective"] for r in rows),
            "unreviewed": sum(not r["complete_judgments"] for r in rows),
            "unresolved": sum(r["unresolved"] for r in rows),
            "disputed": sum(r["disputed"] for r in rows),
            "label_disagreements": sum(r["label_disagreement"] for r in rows),
            "admitted_jointly_faithful": sum(r["jointly_faithful"] and r["admission"] == "admitted_consistent_encoding" for r in rows) if condition == "C" else None}
        if condition == "C":
            admitted = [r for r in rows if r["admission"] == "admitted_consistent_encoding"]
            complete = sum(r["complete_judgments"] for r in admitted)
            defects = sum(r["jointly_defective"] for r in admitted)
            report["by_condition"][condition].update(
                admitted_source_observations=len(admitted),
                admitted_complete_judgments=complete,
                admitted_jointly_defective=defects,
                admitted_disputed=sum(r["disputed"] for r in admitted),
                admitted_unresolved=sum(r["unresolved"] for r in admitted),
                admitted_unreviewed=sum(not r["complete_judgments"] for r in admitted),
                llm_assessed_false_assurance={
                    "numerator": defects, "denominator": complete,
                    "rate": defects / complete if complete else None,
                    "numerator_definition": "Admitted source observations labeled incorrect or partially_faithful by both judges.",
                    "denominator_definition": "Admitted source observations with two valid rubric judgments, including disputed and unresolved judgments.",
                    "interpretation": "Conservative LLM-assessed defect fraction, not ground-truth error rate. Disputed, unresolved, and unreviewed observations are reported separately and are not established defect-free."})
    write_json(Path(output_dir) / "judgments.json", report)
    return report
