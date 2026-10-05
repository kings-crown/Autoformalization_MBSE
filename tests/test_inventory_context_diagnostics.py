"""Missing context citations are local, fully identified and never auto-repaired."""
from copy import deepcopy
import unittest

from test_canonical_obligations import fixture
from canonical_obligations import validate_inventory


def missing_links_fixture():
    sources, inventory = fixture()
    # Remove the only citation of CTX1 while retaining its classified role.
    inventory["requirements"][0]["obligations"].pop()
    inventory["requirements"][0]["context"].extend([
        {"source_id": "CTX1", "role": "exception", "reason": "Qualifies the same component."},
        {"source_id": "CTX1", "role": "unresolved_conflict", "reason": "Its relationship needs explicit source grounding."}])
    excerpt = {"id": "CTX2", "quote": "The timing limit excludes periods when the external service is unavailable."}
    sources[1]["source"] = {"context": [excerpt]}
    inventory["requirements"][1]["context"] = [
        {"source_id": "CTX2", "role": "exception", "reason": "Qualifies the timing scope."}]
    inventory["requirements"][1]["obligations"][0]["limitations"].append(excerpt["quote"])
    return sources, inventory


class InventoryContextDiagnosticTests(unittest.TestCase):
    def test_aggregates_every_missing_row_source_and_role_without_mutation(self):
        sources, inventory = missing_links_fixture()
        before_sources, before_inventory = deepcopy(sources), deepcopy(inventory)
        with self.assertRaises(ValueError) as raised:
            validate_inventory(inventory, sources)
        diagnostic = str(raised.exception)
        self.assertIn("requirement_id=R1, source_id=CTX1, roles=additional_obligation,exception,unresolved_conflict", diagnostic)
        self.assertIn("requirement_id=R2, source_id=CTX2, roles=exception", diagnostic)
        self.assertEqual(diagnostic.count("requirement_id="), 2)
        self.assertIn("limitations prose alone are not citations", diagnostic)
        self.assertEqual(inventory, before_inventory)
        self.assertEqual(sources, before_sources)

    def test_all_three_role_types_still_require_local_literal_evidence(self):
        for role in ("additional_obligation", "exception", "unresolved_conflict"):
            with self.subTest(role=role):
                sources, inventory = fixture()
                inventory["requirements"][0]["context"][0]["role"] = role
                inventory["requirements"][0]["obligations"].pop()
                with self.assertRaisesRegex(ValueError, f"requirement_id=R1, source_id=CTX1, roles={role}"):
                    validate_inventory(inventory, sources)

    def test_citing_context_in_another_requirement_does_not_supply_local_link(self):
        sources, inventory = fixture()
        inventory["requirements"][0]["obligations"].pop()
        inventory["requirements"][1]["context"].append(
            {"source_id": "CTX1", "role": "explanatory", "reason": "Related source context remains identifiable."})
        inventory["requirements"][1]["obligations"][0]["source_basis"].append(
            {"source_id": "CTX1", "quote": sources[0]["source"]["context"][0]["quote"]})
        with self.assertRaisesRegex(ValueError, "requirement_id=R1, source_id=CTX1"):
            validate_inventory(inventory, sources)

    def test_valid_local_citations_qualify_existing_components_without_new_behavior(self):
        sources, inventory = missing_links_fixture()
        for index, sid in enumerate(("CTX1", "CTX2")):
            quote = next(row["quote"] for row in sources[index]["source"]["context"] if row["id"] == sid)
            inventory["requirements"][index]["obligations"][0]["source_basis"].append(
                {"source_id": sid, "quote": quote})
        before = deepcopy(inventory)
        validated = validate_inventory(inventory, sources)
        self.assertEqual(validated, before)
        self.assertEqual(inventory, before)
        self.assertEqual([len(row["obligations"]) for row in validated["requirements"]], [1, 1])

    def test_nonliteral_citations_remain_invalid_without_automatic_replacement(self):
        sources, inventory = fixture()
        inventory["requirements"][0]["obligations"][1]["source_basis"][0]["quote"] = "A paraphrase is not the actual excerpt."
        before = deepcopy(inventory)
        with self.assertRaisesRegex(ValueError, "literal quotation"):
            validate_inventory(inventory, sources)
        self.assertEqual(inventory, before)


if __name__ == "__main__":
    unittest.main()
