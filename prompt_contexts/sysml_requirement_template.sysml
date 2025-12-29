package ExampleRequirementTemplate {
	import Requirements::*;
	import ScalarValues::*;
	import AnalysisCases::*;

	doc /*
	 * Template fragment illustrating how a textual requirement, supporting context,
	 * and solver-backed evidence can be represented using SysML v2 constructs.
	 * The generated models reuse the same pattern with project-specific names.
	 */

	part def TemplateContext {
		doc /*
		 * Holds original statements and interpretative artefacts.
		 * Suggested modelling steps after auto-generation:
		 * 1. Add domain-specific parts (system, environment, interfaces) and reference them from dedicated requirements.
		 * 2. Translate textual intents into constraint usages or behaviours tied to those parts.
		 * 3. Capture additional evidence artefacts (simulation models, tests) and relate them via analyses/views.
		 */
		attribute originalStatement : ScalarValues::String;
		attribute naturalInterpretation : ScalarValues::String;
		attribute informalLemma : ScalarValues::String;
		attribute reasoningSketch : ScalarValues::String;
	}

	part def TemplateEvidence {
		doc /* Links to generated SAT/UNSAT SMT artefacts and solver status. */
		attribute satArtifactPath : ScalarValues::String;
		attribute unsatArtifactPath : ScalarValues::String;
		attribute satStatus : ScalarValues::String;
		attribute unsatStatus : ScalarValues::String;
	}

	requirement def TemplateRequirement :> RequirementCheck {
		doc /*
		 * Requirement attaches both the textual context and solver evidence.
		 * Constraints ensure the upstream translation generated non-empty data.
		 */
		subject context : TemplateContext;
		subject evidence : TemplateEvidence;
		require constraint { context.originalStatement != "" }
	}

	analysis def TemplateSmtAnalysis :> AnalysisCase {
		doc /*
		 * Analysis references the requirement and evidence as subjects.
		 * The objective reuses the requirement and encodes expected solver outcomes.
		 */
		subject context : TemplateContext;
		subject proof : TemplateEvidence;
		in requirement target : TemplateRequirement;
		objective solverConsistency {
			require target;
			require constraint { proof.satStatus == "sat" & proof.unsatStatus == "unsat" }
		}
	}

	view def TemplateEvidenceOverview {
		doc /* Simple view exposing all template elements for traceability. */
		expose TemplateContext::*;
		expose TemplateEvidence::*;
		expose TemplateRequirement::*;
		expose TemplateSmtAnalysis::*;
	}
}
