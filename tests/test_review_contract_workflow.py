"""Independent shared-contract provenance, semantic evidence and review boundaries.

These tests use the in-process ASGI client, local Z3 and the local SysML compiler.
They do not start a listening server or invoke a model provider.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from review_server import create_app
from review_sysml import compiler_capability


def voltage_candidate(nominal="27", limit="28"):
    """Keep the independent implementation value out of its required bound."""
    variables = [{"name": "voltage", "type": "Real", "role": "output", "unit": "V"}]
    initial, transitions = [], []
    if nominal is not None:
        variables.append({"name": "nominal", "type": "Real", "role": "parameter", "unit": "V", "value": nominal})
        initial = [{"op": "=", "args": [{"var": "voltage"}, {"var": "nominal"}]}]
        transitions = [{"op": "=", "args": [{"var": "voltage", "at": "next"}, {"var": "nominal"}]}]
    return {
        "schema": "review_behavior/1", "horizon": 2, "step": {"value": "1", "unit": "s"},
        "variables": variables, "initial": initial, "transitions": transitions, "assumptions": [],
        "properties": [{"id": "VoltageLimit", "kind": "always", "requirement_ids": ["REQ-001"],
                        "predicate": {"op": "<=", "args": [{"var": "voltage"}, {"value": limit, "unit": "V"}]}}],
    }


class ContractInputBoundaryTests(unittest.TestCase):
    def test_partial_source_rule_or_property_mutation_is_rejected(self):
        from review_contracts import build_contract_bundle, behavior_from_contracts
        requirements = [{"id": "REQ-001", "text": "battery.voltage <= 28 V", "source": {"document": "fixture.txt", "location": "line 1"}}]
        bundle = build_contract_bundle(requirements, hashlib.sha256(b"fixture source").hexdigest(), voltage_candidate())
        original = deepcopy(bundle)
        mutations = [
            lambda b: b["contracts"][0]["source_refs"][0].update(text="battery.voltage <= 99 V"),
            lambda b: b["contracts"][0]["rule"].update(version="unreviewed-rule-version"),
            lambda b: b["contracts"][0]["property"]["predicate"]["args"][1].update(value="99"),
            lambda b: b["behavior"]["initial"].append(False),
            lambda b: b.update(context_sha256="stale"),
        ]
        for index, mutate in enumerate(mutations):
            changed = deepcopy(bundle)
            mutate(changed)
            with self.subTest(mutation=index), self.assertRaises(ValueError):
                behavior_from_contracts(changed)
        canonical = behavior_from_contracts(bundle)
        canonical["properties"][0]["predicate"]["args"][1]["value"] = "99"
        self.assertEqual(bundle, original)


@unittest.skipUnless(shutil.which("z3") and compiler_capability()["available"], "Local Z3/SysML tools unavailable")
class ContractWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="contract_workflow_")
        self.root = Path(self.directory.name)
        self.app = create_app(self.root)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def load(self, run_id):
        response = self.client.get(f"/api/runs/{run_id}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def completed(self, *, candidate=None, limit="28", **extra):
        payload = {"name": "Shared voltage contract", "engine": "local", "format": "text",
                   "text": f"battery.voltage <= {limit} V", "behavior": candidate or voltage_candidate(limit=limit),
                   "analysis_mode": "check_design",
                   "design_review": {"reviewer": "Contract workflow fixture", "acknowledge": True,
                                     "rationale": "Independent synthetic candidate and property interpretation inspected."}}
        payload.update(extra)
        if payload.get("parent_run_id"):
            parent = self.load(payload["parent_run_id"])
            payload["design_review"].update(parent_source_hash=parent["source_hash"],
                                            parent_evidence_hash=parent["evidence_hash"])
        response = self.client.post("/api/runs", json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["id"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            run = self.load(run_id)
            if run["status"] in {"completed", "failed"}:
                self.assertEqual(run["status"], "completed", run.get("errors"))
                return run
            time.sleep(.05)
        self.fail("Local contract run did not finish within sixty seconds.")

    def contract(self, run):
        return next(row for row in run["contracts"]["contracts"] if row.get("property") is not None)

    def decide_contract(self, run, decision="accept", **overrides):
        payload = {"source_hash": run["source_hash"], "evidence_hash": run["evidence_hash"],
                   "contract_review_hash": run["contract_review_hash"], "reviewer": "Contract workflow fixture",
                   "decision": decision, "rationale": "Synthetic source and exact proposed contract inspected."}
        payload.update(overrides)
        return self.client.post(f"/api/runs/{run['id']}/contracts/{self.contract(run)['id']}/reviews", json=payload)

    def review(self, run, **overrides):
        payload = {"source_hash": run["source_hash"], "evidence_hash": run["evidence_hash"],
                   "assumption_review_hash": run["assumption_review_hash"], "contract_review_hash": run["contract_review_hash"],
                   "architecture_binding_review_hash": run.get("architecture_binding_review_hash"),
                   "correction_summary_sha256": (run.get("correction_summary") or {}).get("sha256"),
                   "reviewer": "Contract workflow fixture", "decision": "approve", "acknowledge": True,
                   "scope": "This synthetic candidate and its inspected interpretations only.",
                   "rationale": "Inspect local contract, model, scope and solver evidence."}
        payload.update(overrides)
        return self.client.post(f"/api/runs/{run['id']}/reviews", json=payload)

    def accept_bindings(self, run):
        view = self.client.get(f"/api/runs/{run['id']}/bindings").json()
        attribute = next(e for e in view['choices'] if e['kind'] == 'attribute' and e.get('unit') == 'V')
        owner = next((e for e in view['choices'] if e['kind'] == 'part'), attribute)
        for target in view['status']['targets']:
            if not target['required']:
                continue
            element = attribute if target['target_kind'] == 'variable' else owner
            response = self.client.post(f"/api/runs/{run['id']}/bindings/reviews", json={
                'source_hash': run['source_hash'], 'evidence_hash': run['evidence_hash'],
                'model_sha256': view['status']['model_sha256'],
                'architecture_binding_review_hash': run['architecture_binding_review_hash'],
                'target_kind': target['target_kind'], 'target_id': target['target_id'], 'element_id': element['id'],
                'decision': 'accept', 'reviewer': 'Contract workflow fixture',
                'rationale': 'Explicit association to this synthetic domain declaration; no implementation equivalence claim.'})
            self.assertEqual(response.status_code, 201, response.text)
            run = self.load(run['id'])
        return run

    def accept_assumptions(self, run):
        for assumption in run["assumptions"]:
            payload = {"source_hash": run["source_hash"], "evidence_hash": run["evidence_hash"],
                       "assumption_review_hash": run["assumption_review_hash"], "reviewer": "Contract workflow fixture",
                       "decision": "accept", "rationale": "Synthetic premise reviewed for this exact fixture."}
            response = self.client.post(f"/api/runs/{run['id']}/assumptions/{assumption['id']}/reviews", json=payload)
            self.assertEqual(response.status_code, 201, response.text)
            run = self.load(run["id"])
        return run

    def test_shared_guarantee_has_source_rule_sysml_and_counterexample_evidence(self):
        run = self.completed(candidate=voltage_candidate("29"))
        contract = self.contract(run)
        self.assertEqual(run["contracts"]["schema"], "review_contracts/1")
        self.assertEqual(contract["requirement_ids"], ["REQ-001"])
        self.assertEqual(contract["property"], run["behavior"]["properties"][0])
        self.assertTrue(contract["rule"]["id"])
        self.assertTrue(contract["rule"]["version"])
        self.assertEqual(contract["rule"]["basis"], "deterministic_property_pattern")
        self.assertEqual(contract["owner"]["status"], "unallocated")
        self.assertEqual(contract["source_alignment"], "pending")
        self.assertIn(run["requirements"][0]["text"], json.dumps(contract["source_refs"]))
        recorded_input = run["behavioral_analysis"]["contract_input"]
        self.assertEqual(recorded_input["source_hash"], run["source_hash"])
        for field in ("behavior_sha256", "context_sha256"):
            self.assertEqual(recorded_input[field], run["contracts"][field])
        self.assertEqual(contract["checks"][0]["behavior_binding"], "exact_hash")
        links = contract["checks"][0]["artifacts"]
        self.assertEqual({link["role"] for link in links}, {"query", "result"})
        for link in links:
            exact_bytes = (self.root / run["id"] / link["artifact"]).read_bytes()
            self.assertEqual(hashlib.sha256(exact_bytes).hexdigest(), link["sha256"])
        self.assertEqual(run["compilation"]["status"], "passed")
        self.assertEqual(run["behavioral_analysis"]["model_feasibility"]["verdict"], "sat")
        check = run["behavioral_analysis"]["checks"][0]
        self.assertEqual(check["verdict"], "counterexample")
        self.assertTrue(all(row["values"]["voltage"] == "29" for row in check["trace"]))
        directory = self.root / run["id"]
        feasibility = run["behavioral_analysis"]["model_feasibility"]
        base_bytes = (directory / feasibility["artifacts"]["query"]).read_bytes()
        self.assertEqual(hashlib.sha256(base_bytes).hexdigest(), feasibility["query_sha256"])
        # The source limit occurs only in G. If injected into base, the independent
        # candidate voltage=29 would become infeasible instead of a counterexample.
        import requirements_pipeline as legacy
        def atoms(value):
            if isinstance(value, list):
                return [atom for item in value for atom in atoms(item)]
            return [value]
        self.assertNotIn("28", atoms(legacy._parse_sexpr(base_bytes.decode())))
        query_bytes = (directory / check["evidence"]["artifacts"]["query"]).read_bytes()
        self.assertEqual(hashlib.sha256(query_bytes).hexdigest(), check["evidence"]["query_sha256"])
        self.assertIn("28", query_bytes.decode())
        self.assertIn("28", run["model"]["text"])
        inspection = run["model"]["inspection"]
        self.assertEqual(inspection["text_sha256"], hashlib.sha256(run["model"]["text"].encode()).hexdigest())
        self.assertTrue(any("VoltageLimit" in e.get("property_ids", []) for e in inspection["elements"]))
        reference = contract["sysml"]
        self.assertEqual(reference["binding"], "exact_text_index")
        self.assertEqual(reference["text_sha256"], inspection["text_sha256"])
        self.assertEqual(reference["text_sha256"], run["compilation"]["model_sha256"])
        indexed = {e.get("qualified_name"): e for e in inspection["elements"] if e.get("qualified_name")}
        declared = indexed[reference["qualified_name"]]
        self.assertEqual(declared["start_line"], reference["start_line"])
        self.assertEqual(declared["source_requirement_ids"], ["REQ-001"])
        self.assertEqual(len(reference["constraint_names"]), 3)
        for step, name in enumerate(reference["constraint_names"]):
            constraint = indexed[name]
            self.assertIn(f"v_voltage_{step} <= 28", constraint["expression"])
            self.assertGreater(constraint["start_line"], reference["start_line"])
            self.assertLess(constraint["end_line"], reference["end_line"])
        run = self.accept_assumptions(run)
        self.assertEqual(self.decide_contract(run).status_code, 201)
        run = self.load(run["id"])
        self.assertEqual(self.review(run).status_code, 409)

    def test_pending_rejected_stale_and_changed_contract_decisions_gate_acceptance(self):
        run = self.accept_bindings(self.accept_assumptions(self.completed()))
        contract_bytes = (self.root / run["id"] / "contracts.json").read_bytes()
        initial_hash = run["contract_review_hash"]
        self.assertEqual(self.review(run).status_code, 409)
        for field in ("source_hash", "evidence_hash", "contract_review_hash"):
            with self.subTest(field=field):
                self.assertEqual(self.decide_contract(run, **{field: "stale"}).status_code, 409)
        self.assertEqual(self.decide_contract(run, contract_review_hash=None).status_code, 422)
        self.assertEqual(self.decide_contract(run, "reject").status_code, 201)
        rejected = self.load(run["id"])
        self.assertNotEqual(rejected["contract_review_hash"], initial_hash)
        self.assertEqual(self.review(rejected).status_code, 409)
        self.assertEqual(self.decide_contract(run).status_code, 409)
        self.assertEqual(self.decide_contract(rejected).status_code, 201)
        accepted = self.load(run["id"])
        self.assertEqual(self.review(accepted, contract_review_hash=initial_hash).status_code, 409)
        response = self.review(accepted)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.load(run["id"])["baseline"]["status"], "approved")
        self.assertEqual(self.decide_contract(accepted, "defer").status_code, 201)
        changed = self.load(run["id"])
        self.assertEqual(changed["baseline"]["status"], "pending")
        self.assertEqual(self.review(changed).status_code, 409)
        self.assertEqual(changed["evidence_hash"], run["evidence_hash"])
        self.assertEqual((self.root / run["id"] / "contracts.json").read_bytes(), contract_bytes)
        self.assertEqual(changed["contracts"], run["contracts"])

    def test_contract_and_query_tampering_blocks_contract_review_and_export(self):
        run = self.completed()
        query_name = run["behavioral_analysis"]["model_feasibility"]["artifacts"]["query"]
        for name in ("contracts.json", query_name):
            with self.subTest(artifact=name):
                path = self.root / run["id"] / name
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b"\n ")
                    self.assertEqual(self.decide_contract(run).status_code, 409)
                    self.assertEqual(self.review(run).status_code, 409)
                    self.assertEqual(self.client.get(f"/api/runs/{run['id']}/artifacts/{name}").status_code, 409)
                    self.assertEqual(self.client.get(f"/api/runs/{run['id']}/packet").status_code, 409)
                finally:
                    path.write_bytes(original)
        self.assertEqual(self.decide_contract(run).status_code, 201)

    def test_changed_limit_has_directional_semantic_evidence_independent_of_candidate(self):
        parent = self.completed(candidate=voltage_candidate("27", "28"))
        child = self.completed(candidate=voltage_candidate("27", "29"), limit="29", parent_run_id=parent["id"],
                               revision_rationale="Synthetic limit revision for an explicit semantic comparison.")
        self.assertEqual(parent["contracts"]["context_sha256"], child["contracts"]["context_sha256"])
        self.assertNotEqual(parent["source_hash"], child["source_hash"])
        change = next(c for c in child["contract_changes"]["changes"] if c["contract_id"] == self.contract(child)["id"])
        self.assertTrue(change["source_changed"])
        # Candidate voltage is fixed at 27 on both sides. Its dynamics must not
        # hide that relaxing 28 to 29 permits more contract valuations.
        self.assertEqual(change["status"], "weakened", change)
        self.assertEqual(change["newly_permitted"]["verdict"], "sat")
        self.assertEqual(change["newly_forbidden"]["verdict"], "unsat")
        self.assertTrue(change["newly_permitted"]["witness"])
        for kind in ("newly_permitted", "newly_forbidden"):
            evidence = change[kind]
            query = self.root / child["id"] / evidence["artifacts"]["query"]
            self.assertEqual(hashlib.sha256(query.read_bytes()).hexdigest(), evidence["query_sha256"])
        self.assertEqual(child["contract_reviews"], [])
        self.assertEqual(self.review(child).status_code, 409)
        child = self.accept_bindings(self.accept_assumptions(child))
        self.assertEqual(self.decide_contract(child).status_code, 201)
        child = self.load(child["id"])
        missing_ack = self.review(child)
        self.assertEqual(missing_ack.status_code, 409)
        self.assertIn("acknowledge", missing_ack.text.lower())
        approved = self.review(child, acknowledge_contract_changes=True)
        self.assertEqual(approved.status_code, 201, approved.text)
        self.assertEqual(approved.json()["contract_changes_snapshot"], child["contract_changes"])

    def test_context_revision_is_not_equivalence_and_preserves_parent_evidence(self):
        parent = self.completed(candidate=voltage_candidate(None))
        parent_dir = self.root / parent["id"]
        original_files = {a["name"]: (parent_dir / a["name"]).read_bytes() for a in parent["artifacts"]}
        original_contract = deepcopy(parent["contracts"])
        candidate = voltage_candidate(None)
        candidate["assumptions"] = [{"id": "SupplyScope", "text": "Synthetic supply domain", "scope": "always",
                                      "predicate": {"op": ">=", "args": [{"var": "voltage"}, {"value": "0", "unit": "V"}]}}]
        child = self.completed(candidate=candidate, parent_run_id=parent["id"],
                               revision_rationale="Introduce a source-visible environmental domain for comparison.")
        self.assertEqual(parent["source_hash"], child["source_hash"])
        self.assertNotEqual(parent["contracts"]["context_sha256"], child["contracts"]["context_sha256"])
        self.assertNotEqual(parent["evidence_hash"], child["evidence_hash"])
        self.assertEqual(child["contract_reviews"], [])
        comparison = child["contract_changes"]
        self.assertNotEqual(comparison["status"], "equivalent")
        self.assertTrue(comparison["changes"])
        self.assertFalse(any(row.get("status") == "equivalent" for row in comparison["changes"]))
        parent_after = self.load(parent["id"])
        self.assertEqual(parent_after["contracts"], original_contract)
        self.assertEqual(parent_after["evidence_hash"], parent["evidence_hash"])
        self.assertEqual(parent_after["baseline"]["status"], "superseded")
        for name, content in original_files.items():
            self.assertEqual((parent_dir / name).read_bytes(), content, name)


if __name__ == "__main__":
    unittest.main()
