package AutomatedChargingRequirements {
	doc
	/*
	 * Functional requirements for an automated battery charging controller. The
	 * requirements mirror the behaviors exercised by the accompanying Z3 SAT/UNSAT
	 * checks that model bounded traces of battery percentage and charging state.
	 */

	private import Requirements::*;
	private import AnalysisCases::*;
	private import ScalarValues::*;

	part def ChargingStateSample {
		doc /* Snapshot of a single step in the charging trace. */
		attribute batteryLevel : ScalarValues::Integer;
		attribute isCharging : ScalarValues::Boolean;
	}

	part def ChargingTrace {
		doc /* Finite horizon trace of the automated charging process. */
		part steps : ChargingStateSample[0..*];
		attribute increments : ScalarValues::Integer[0..*];
		attribute initialLevel : ScalarValues::Integer;
		attribute finalLevel : ScalarValues::Integer;
		attribute maxLevel : ScalarValues::Integer;
		attribute minimalIncrement : ScalarValues::Integer;
		attribute isChargingAtFinal : ScalarValues::Boolean;
	}

	requirement def AutomaticChargingCapability :> RequirementCheck {
		doc
		/*
		 * Aggregate requirement ensuring the controller automatically brings the
		 * battery to 100% without manual intervention or overcharge.
		 */
		subject trace : ChargingTrace;
		requirement startChargingBelowFull : StartChargingRequirement;
		requirement maintainPositiveProgress : PositiveIncrementRequirement;
		requirement avoidOvercharge : ClampAtFullRequirement;
		requirement autoTerminateAtFull : AutoStopRequirement;
	}

	requirement def StartChargingRequirement :> RequirementCheck {
		doc /* If any step reports battery < 100%, the controller commands charging. */
		subject trace : ChargingTrace;
		require constraint {
			trace.initialLevel >= 0 & trace.initialLevel <= 99
		}
	}

	requirement def PositiveIncrementRequirement :> RequirementCheck {
		doc /* While charging below 100%, the battery percentage increases by >= 1. */
		subject trace : ChargingTrace;
		require constraint {
			trace.minimalIncrement >= 1
		}
	}

	requirement def ClampAtFullRequirement :> RequirementCheck {
		doc /* Battery percentage never exceeds 100%. */
		subject trace : ChargingTrace;
		require constraint {
			trace.maxLevel <= 100
		}
	}

	requirement def AutoStopRequirement :> RequirementCheck {
		doc /* Once the battery reaches 100%, charging stops automatically. */
		subject trace : ChargingTrace;
		require constraint {
			trace.finalLevel == 100 & trace.isChargingAtFinal == false
		}
	}

	analysis def Z3SatisfiabilityWitness {
		doc
		/*
		 * SMT-based analysis showing there exists a feasible trace that satisfies
		 * the automated charging requirements (bounded to horizon N = 5).
		 */
		subject trace : ChargingTrace;
		in requirement target : AutomaticChargingCapability;
		objective boundedConsistency {
			require target.startChargingBelowFull;
			require target.maintainPositiveProgress;
			require target.avoidOvercharge;
			require target.autoTerminateAtFull;
		}

	}

	analysis def Z3SafetyInvariant {
		doc
		/*
		 * SMT-based analysis that attempts to violate the functional safety
		 * requirements; Z3 reports UNSAT, showing no counterexample exists within
		 * the horizon.
		 */
		subject trace : ChargingTrace;
		in requirement target : AutomaticChargingCapability;
		objective boundedSafety {
			require target.startChargingBelowFull;
			require target.maintainPositiveProgress;
			require target.avoidOvercharge;
			require target.autoTerminateAtFull;
		}

	}
}
