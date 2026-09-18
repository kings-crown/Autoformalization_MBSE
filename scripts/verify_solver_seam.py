#!/usr/bin/env python3
"""Verification harness for the pluggable solver seam in requirements_pipeline.

No LLM/network required: it drives the real SolverRunner code against live Z3
using hand-encoded SMT-LIB for contradiction clusters from the ADD drone
requirements doc (examples/synthetic/add_drone_requirements.csv), and checks the
portfolio cross-check logic. Exits non-zero if any check fails.

Run:  python3 scripts/verify_solver_seam.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import requirements_pipeline as rp  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))


# --- SMT-LIB fragments for ADD clusters (set-logic intentionally omitted: the
#     seam's ensure_set_logic must add it) ---------------------------------

FRAG_SAT = """
(declare-fun cruise_alt () Int)
(assert (>= cruise_alt 30))
(assert (<= cruise_alt 120))
(check-sat)
"""

# Cluster A: cruise_alt <= 120 (R007) and = 150 (R023)  -> UNSAT
FRAG_A = """
(set-option :produce-unsat-cores true)
(declare-fun cruise_alt () Int)
(declare-fun en_ADD_R007 () Bool)
(declare-fun en_ADD_R023 () Bool)
(assert (! (=> en_ADD_R007 (<= cruise_alt 120)) :named req_ADD_R007))
(assert (! (=> en_ADD_R023 (= cruise_alt 150)) :named req_ADD_R023))
(assert en_ADD_R007)
(assert en_ADD_R023)
(check-sat)
(get-unsat-core)
"""

# Cluster C (3-way transitive): start<=100 (R014), consumed=80 (R012),
#   (start-consumed)>=25 (R013)  ->  start>=105  -> UNSAT
FRAG_C = """
(set-option :produce-unsat-cores true)
(declare-fun batt_start () Int)
(declare-fun batt_consumed () Int)
(declare-fun en_ADD_R012 () Bool)
(declare-fun en_ADD_R013 () Bool)
(declare-fun en_ADD_R014 () Bool)
(assert (! (=> en_ADD_R012 (= batt_consumed 80)) :named req_ADD_R012))
(assert (! (=> en_ADD_R013 (>= (- batt_start batt_consumed) 25)) :named req_ADD_R013))
(assert (! (=> en_ADD_R014 (<= batt_start 100)) :named req_ADD_R014))
(assert en_ADD_R012)
(assert en_ADD_R013)
(assert en_ADD_R014)
(check-sat)
(get-unsat-core)
"""

# Cluster E (same-state boolean): low_battery -> ascend (R018) and -> descend
#   (R028), with domain axiom ascend -> not descend  -> UNSAT
FRAG_E = """
(set-option :produce-unsat-cores true)
(declare-fun low_battery () Bool)
(declare-fun ascend () Bool)
(declare-fun descend () Bool)
(declare-fun en_ADD_R018 () Bool)
(declare-fun en_ADD_R028 () Bool)
(assert (! (=> en_ADD_R018 (=> low_battery ascend)) :named req_ADD_R018))
(assert (! (=> en_ADD_R028 (=> low_battery descend)) :named req_ADD_R028))
(assert (=> ascend (not descend)))
(assert low_battery)
(assert en_ADD_R018)
(assert en_ADD_R028)
(check-sat)
(get-unsat-core)
"""


class _FakeRunner(rp.SolverRunner):
    """Stand-in for a second solver with a fixed verdict (cvc5 not installed)."""

    def __init__(self, name: str, verdict: str) -> None:
        self.name = name
        self._verdict = verdict

    def run(self, fragment: str):  # noqa: ANN001, ANN201
        return {"status": "ok", "result": self._verdict, "solver": self.name}


def main() -> int:
    # 1. ensure_set_logic: inserts when missing, after (set-option ...)
    out = rp.ensure_set_logic(FRAG_A)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    opt_idx = next(i for i, ln in enumerate(lines) if ln.startswith("(set-option"))
    logic_idx = next(i for i, ln in enumerate(lines) if ln.startswith("(set-logic"))
    check("ensure_set_logic inserts (set-logic QF_LIA)", "(set-logic QF_LIA)" in out)
    check("ensure_set_logic placed after (set-option ...)", logic_idx == opt_idx + 1,
          f"opt@{opt_idx} logic@{logic_idx}")

    # 2. idempotent: a fragment that already declares a logic is untouched
    already = "(set-logic QF_LRA)\n(declare-fun x () Real)\n(check-sat)\n"
    check("ensure_set_logic idempotent / respects existing logic",
          rp.ensure_set_logic(already) == already)

    # 3. QF_LIA detection only works AFTER the logic line is inserted
    check("_is_qf_lia_fragment False before insert", not rp._is_qf_lia_fragment(FRAG_A))
    check("_is_qf_lia_fragment True after insert", rp._is_qf_lia_fragment(out))

    # 4. Z3 path: SAT fragment via the public seam (run_z3_fragment, default z3)
    r_sat = rp.run_z3_fragment(FRAG_SAT)
    check("Z3 SAT fragment -> status ok", r_sat.get("status") == "ok", str(r_sat)[:120])
    check("Z3 SAT fragment -> verdict sat", rp._solver_verdict(r_sat) == "sat",
          repr(rp._solver_verdict(r_sat)))

    # 5. Z3 path: each injected cluster -> UNSAT with the expected named core
    for label, frag, expected_core in (
        ("A", FRAG_A, ["req_ADD_R007", "req_ADD_R023"]),
        ("C", FRAG_C, ["req_ADD_R012", "req_ADD_R013", "req_ADD_R014"]),
        ("E", FRAG_E, ["req_ADD_R018", "req_ADD_R028"]),
    ):
        res = rp.run_z3_fragment(frag)
        verdict = rp._solver_verdict(res)
        body = res.get("result", "")
        core_ok = all(name in body for name in expected_core)
        check(f"Cluster {label}: Z3 verdict unsat", verdict == "unsat", repr(verdict))
        check(f"Cluster {label}: unsat core = {expected_core}", core_ok, body.replace("\n", " "))

    # 6. Portfolio cross-check: disagreement raises an alert
    portfolio_div = rp.PortfolioRunner(rp.Z3Runner(), _FakeRunner("cvc5", "sat"))
    res_div = portfolio_div.run(rp.ensure_set_logic(FRAG_A))  # z3=unsat, fake=sat
    cc = res_div.get("cross_check", {})
    check("Portfolio surfaces disagreement alert", "alert" in cc, str(cc))
    check("Portfolio preserves primary (z3) result", res_div.get("status") == "ok"
          and rp._solver_verdict(res_div) == "unsat")

    # 7. Portfolio with a missing secondary solver degrades gracefully (no alert)
    portfolio_missing = rp.PortfolioRunner(rp.Z3Runner(), rp.Cvc5Runner(path="cvc5-not-installed"))
    res_missing = portfolio_missing.run(rp.ensure_set_logic(FRAG_SAT))
    cc2 = res_missing.get("cross_check", {})
    check("Portfolio handles missing secondary (no false alert)",
          "alert" not in cc2 and "note" in cc2, str(cc2))
    check("Portfolio still returns usable primary result on missing secondary",
          res_missing.get("status") == "ok" and rp._solver_verdict(res_missing) == "sat")

    # --- report -----------------------------------------------------------
    width = max(len(n) for n, _, _ in RESULTS)
    failures = 0
    print("\nSolver-seam verification\n" + "=" * (width + 10))
    for name, passed, detail in RESULTS:
        status = "PASS" if passed else "FAIL"
        line = f"[{status}] {name.ljust(width)}"
        if not passed and detail:
            line += f"  <- {detail}"
        print(line)
        failures += 0 if passed else 1
    print("=" * (width + 10))
    print(f"{len(RESULTS) - failures}/{len(RESULTS)} checks passed")

    # environment note
    import shutil
    print("\ncvc5 on PATH:", bool(shutil.which("cvc5")),
          "(live cvc5/portfolio run needs cvc5 installed; logic verified via stub)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
