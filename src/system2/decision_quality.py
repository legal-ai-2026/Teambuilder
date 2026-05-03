from __future__ import annotations

from collections.abc import Sequence

from .models import (
    CognitiveAdaptationRequest,
    CognitiveStateSnapshot,
    Confidence,
    DecisionContext,
    DecisionQualityAssessment,
    DecisionUtilityEstimate,
    EvidenceBundle,
    OperationalTwinRequest,
    RelianceGuidance,
    RosterRecommendation,
    ScenarioInjectRecommendation,
    ScenarioOption,
    ScoreRequest,
    StateEstimate,
)


_CONFIDENCE_SCORE = {
    Confidence.high: 0.9,
    Confidence.medium: 0.65,
    Confidence.low: 0.35,
    "high": 0.9,
    "medium": 0.65,
    "low": 0.35,
}

_REVERSIBILITY_SCORE = {
    "reversible": 0.9,
    "partially_reversible": 0.55,
    "irreversible": 0.2,
}

_TIME_PRESSURE_DELAY_COST = {
    "low": 0.18,
    "medium": 0.42,
    "high": 0.72,
}

_IMPACT_COST = {
    "low": 0.2,
    "medium": 0.5,
    "high": 0.82,
}


def default_cognitive_decision_context(request: CognitiveAdaptationRequest) -> DecisionContext:
    return request.decision_context or DecisionContext(
        decision_point="scenario adaptation recommendation",
        actor_role="instructor",
        objective="Target the highest-priority cognitive development need without unsafe load.",
        constraints=[
            f"max_safety_risk={request.constraints.max_safety_risk}",
            f"allow_environmental_stress={request.constraints.allow_environmental_stress}",
            "human approval before execution",
        ],
        time_pressure="medium",
        reversibility="partially_reversible",
        stakeholder_impact="medium",
        fallback_action="Hold the current lane and collect another instructor observation.",
    )


def default_operational_twin_decision_context(request: OperationalTwinRequest) -> DecisionContext:
    objective = (
        "Draft mission options with source-grounded state estimates."
        if request.mode == "mission"
        else "Draft training options with source-grounded state estimates."
    )
    return request.decision_context or DecisionContext(
        decision_point=f"operational twin {request.mode} option drafting",
        actor_role="operator",
        objective=objective,
        constraints=[
            "source-linked observations",
            "critic review",
            "human approval before action",
        ],
        time_pressure="high" if request.mode == "mission" else "medium",
        reversibility="partially_reversible",
        stakeholder_impact="high" if request.mode == "mission" else "medium",
        fallback_action="Escalate to the human controller and continue with the current plan.",
    )


def default_roster_decision_context(request: ScoreRequest) -> DecisionContext:
    return request.decision_context or DecisionContext(
        decision_point="mission roster recommendation",
        actor_role="commander or career manager",
        objective="Recommend role assignments while preserving fairness, uncertainty, and second-choice review.",
        constraints=[
            "protected attributes excluded from scoring",
            "fairness audit required",
            "human approval before final assignment",
        ],
        time_pressure="medium",
        reversibility="partially_reversible",
        stakeholder_impact="high",
        fallback_action="Use manual roster review with second-choice and fairness evidence visible.",
    )


def assess_scenario_inject(
    request: CognitiveAdaptationRequest,
    recommendation: ScenarioInjectRecommendation,
) -> tuple[DecisionQualityAssessment, DecisionUtilityEstimate, RelianceGuidance]:
    context = default_cognitive_decision_context(request)
    risk = max(
        recommendation.safety_risk,
        recommendation.fatigue_risk,
        recommendation.unfair_exposure_risk,
    )
    confidence_score = _confidence_score(recommendation.confidence)
    evidence_sufficiency = _clamp(
        min(len(recommendation.evidence_refs) / 3.0, 1.0) * 0.55
        + confidence_score * 0.45
    )
    uncertainty = _clamp((1.0 - confidence_score) * 0.7 + risk * 0.3)
    escalation_reasons: list[str] = []
    if recommendation.status == "blocked":
        escalation_reasons.append(recommendation.block_reason or "scenario recommendation blocked")
    if risk >= 0.72:
        escalation_reasons.append("scenario risk is high")
    if evidence_sufficiency < 0.45:
        escalation_reasons.append("scenario has sparse direct evidence")
    if not request.require_human_approval:
        escalation_reasons.append("human approval gate is disabled")

    quality = _quality(
        context=context,
        evidence_sufficiency=evidence_sufficiency,
        uncertainty=uncertainty,
        escalation_reasons=escalation_reasons,
        notes=[
            f"Risk is {recommendation.risk_level}; confidence is {recommendation.confidence.value}.",
            "Recommendation remains advisory until an instructor approves it.",
        ],
    )
    utility = _utility(
        context=context,
        expected_benefit=max(recommendation.expected_learning_gain, recommendation.transfer_value),
        expected_harm=risk,
        rationale="Utility balances expected learning gain against safety, fatigue, and exposure risk.",
    )
    return quality, utility, _reliance_guidance(quality, context)


def assess_cognitive_adaptation(
    request: CognitiveAdaptationRequest,
    state: CognitiveStateSnapshot,
    recommendations: Sequence[ScenarioInjectRecommendation],
    blocked_recommendations: Sequence[ScenarioInjectRecommendation],
) -> tuple[DecisionQualityAssessment, DecisionUtilityEstimate, RelianceGuidance]:
    context = default_cognitive_decision_context(request)
    primary = next(
        item
        for item in state.estimates
        if item.dimension == state.primary_development_dimension
    )
    option_count = len(recommendations) + len(blocked_recommendations)
    allowed_risk = max(
        (
            max(item.safety_risk, item.fatigue_risk, item.unfair_exposure_risk)
            for item in recommendations
        ),
        default=0.0,
    )
    blocked_risk = max(
        (
            max(item.safety_risk, item.fatigue_risk, item.unfair_exposure_risk)
            for item in blocked_recommendations
        ),
        default=0.0,
    )
    risk = max(allowed_risk, blocked_risk)
    confidence_score = _confidence_score(primary.confidence)
    evidence_sufficiency = _clamp(
        min(len(request.evidence) / 3.0, 1.0) * 0.45
        + min(option_count / 3.0, 1.0) * 0.15
        + confidence_score * 0.4
    )
    uncertainty = _clamp((1.0 - confidence_score) * 0.55 + risk * 0.35 + (0.1 if blocked_recommendations else 0.0))
    escalation_reasons: list[str] = []
    if blocked_recommendations and not recommendations:
        escalation_reasons.append("all scenario recommendations were blocked")
    elif blocked_recommendations:
        escalation_reasons.append("one or more scenario recommendations were blocked")
    if evidence_sufficiency < 0.45:
        escalation_reasons.append("adaptation evidence is sparse")
    if risk >= 0.72:
        escalation_reasons.append("scenario risk is high")
    if not request.require_human_approval:
        escalation_reasons.append("human approval gate is disabled")

    quality = _quality(
        context=context,
        evidence_sufficiency=evidence_sufficiency,
        uncertainty=uncertainty,
        escalation_reasons=escalation_reasons,
        notes=[
            f"Primary dimension is {state.primary_development_dimension}.",
            state.likely_failure_mode,
        ],
    )
    expected_benefit = max(
        (item.expected_learning_gain for item in recommendations),
        default=0.0,
    )
    utility = _utility(
        context=context,
        expected_benefit=expected_benefit,
        expected_harm=risk,
        rationale="Adaptation utility estimates learning value net of safety and fatigue risk.",
    )
    return quality, utility, _reliance_guidance(quality, context)


def assess_scenario_option(
    request: OperationalTwinRequest,
    evidence_bundle: EvidenceBundle,
    state_estimate: StateEstimate,
    option: ScenarioOption,
) -> tuple[DecisionQualityAssessment, DecisionUtilityEstimate, RelianceGuidance]:
    context = default_operational_twin_decision_context(request)
    evidence_sufficiency = _twin_evidence_sufficiency(evidence_bundle)
    uncertainty = _clamp(
        state_estimate.uncertainty.overall * 0.45
        + (1.0 - option.confidence) * 0.35
        + option.risk_score * 0.2
    )
    escalation_reasons = _policy_escalation_reasons(request, evidence_bundle)
    if option.critic_status in {"reject", "escalate"}:
        escalation_reasons.append(f"critic status is {option.critic_status}")
    if option.risk_score >= 0.72:
        escalation_reasons.append("scenario option risk is high")

    quality = _quality(
        context=context,
        evidence_sufficiency=evidence_sufficiency,
        uncertainty=uncertainty,
        escalation_reasons=escalation_reasons,
        notes=[
            f"Critic status is {option.critic_status}.",
            "Option remains draft until a named human records a decision.",
        ],
    )
    utility = _utility(
        context=context,
        expected_benefit=_option_benefit(option),
        expected_harm=option.risk_score,
        rationale="Operational option utility estimates mission or training benefit net of risk and delay.",
    )
    return quality, utility, _reliance_guidance(quality, context)


def assess_operational_twin_run(
    request: OperationalTwinRequest,
    evidence_bundle: EvidenceBundle,
    state_estimate: StateEstimate,
    options: Sequence[ScenarioOption],
) -> tuple[DecisionQualityAssessment, DecisionUtilityEstimate, RelianceGuidance]:
    context = default_operational_twin_decision_context(request)
    evidence_sufficiency = _twin_evidence_sufficiency(evidence_bundle)
    option_risk = max((item.risk_score for item in options), default=0.0)
    option_uncertainty = max((1.0 - item.confidence for item in options), default=0.5)
    uncertainty = _clamp(
        state_estimate.uncertainty.overall * 0.55
        + option_uncertainty * 0.25
        + option_risk * 0.2
    )
    escalation_reasons = _policy_escalation_reasons(request, evidence_bundle)
    if any(item.critic_status == "reject" for item in options):
        escalation_reasons.append("one or more options were critic-rejected")
    if any(item.critic_status == "escalate" for item in options):
        escalation_reasons.append("one or more options require escalation")
    if len(options) != 3:
        escalation_reasons.append("scenario director did not return exactly three options")

    quality = _quality(
        context=context,
        evidence_sufficiency=evidence_sufficiency,
        uncertainty=uncertainty,
        escalation_reasons=escalation_reasons,
        notes=[
            f"Evidence bundle {evidence_bundle.bundle_id} has {len(evidence_bundle.derived_observations)} observations.",
            "Operational twin output is draft-only until human decision.",
        ],
    )
    utility = _utility(
        context=context,
        expected_benefit=max((_option_benefit(item) for item in options), default=0.0),
        expected_harm=option_risk,
        rationale="Run-level utility summarizes the best draft option against the highest observed option risk.",
    )
    return quality, utility, _reliance_guidance(quality, context)


def assess_roster_recommendation(
    request: ScoreRequest,
    recommendation: RosterRecommendation,
) -> tuple[DecisionQualityAssessment, DecisionUtilityEstimate, RelianceGuidance]:
    context = default_roster_decision_context(request)
    roster = recommendation.roster
    confidence_score = _average([_confidence_score(item.confidence) for item in roster], default=0.5)
    disagreement = _average([item.model_disagreement for item in roster], default=0.5)
    source_score = min(len(recommendation.trace.source_refs) / max(len(roster), 1), 1.0)
    second_choice_score = 1.0 if len(recommendation.second_choice_roster) >= len(roster) else 0.45
    evidence_sufficiency = _clamp(
        confidence_score * 0.45
        + source_score * 0.25
        + second_choice_score * 0.2
        + (0.1 if recommendation.trace.input_source_hashes else 0.0)
    )
    uncertainty = _clamp((1.0 - confidence_score) * 0.45 + disagreement * 0.45)
    risk_factor_rate = _average(
        [min(len(item.risk_factors) / 3.0, 1.0) for item in roster],
        default=0.0,
    )
    uncertainty = _clamp(uncertainty + risk_factor_rate * 0.1)

    escalation_reasons: list[str] = []
    if recommendation.fairness_audit.status == "halt":
        escalation_reasons.append("fairness audit status is halt")
    if any(item.confidence == Confidence.low for item in roster):
        escalation_reasons.append("one or more selected assignments have low confidence")
    if max((item.model_disagreement for item in roster), default=0.0) > 0.25:
        escalation_reasons.append("model disagreement exceeds low-confidence threshold")
    if len(recommendation.second_choice_roster) < len(roster):
        escalation_reasons.append("second-choice roster is incomplete")

    quality = _quality(
        context=context,
        evidence_sufficiency=evidence_sufficiency,
        uncertainty=uncertainty,
        escalation_reasons=escalation_reasons,
        notes=[
            f"Fairness audit status is {recommendation.fairness_audit.status}.",
            "Roster recommendation remains advisory pending authorized human approval.",
        ],
    )
    benefit = _average([item.fit_score for item in roster], default=0.0)
    fairness_harm = 0.75 if recommendation.fairness_audit.status == "halt" else 0.15
    expected_harm = _clamp(disagreement * 0.45 + risk_factor_rate * 0.25 + fairness_harm * 0.3)
    utility = _utility(
        context=context,
        expected_benefit=benefit,
        expected_harm=expected_harm,
        rationale="Roster utility uses fit score as benefit and disagreement, risk factors, and fairness status as harm signals.",
    )
    return quality, utility, _reliance_guidance(quality, context)


def _quality(
    *,
    context: DecisionContext,
    evidence_sufficiency: float,
    uncertainty: float,
    escalation_reasons: Sequence[str],
    notes: Sequence[str],
) -> DecisionQualityAssessment:
    reversibility = _REVERSIBILITY_SCORE[context.reversibility]
    framing = _framing_completeness(context)
    reasons = list(dict.fromkeys(reason for reason in escalation_reasons if reason))
    if context.reversibility == "irreversible" and uncertainty >= 0.45:
        reasons.append("decision is irreversible under material uncertainty")
    if context.stakeholder_impact == "high" and evidence_sufficiency < 0.5:
        reasons.append("high-impact decision has insufficient evidence")

    readiness = "ready"
    if reasons:
        readiness = "escalate"
    elif evidence_sufficiency < 0.62 or uncertainty > 0.42 or framing < 0.72:
        readiness = "review"

    return DecisionQualityAssessment(
        readiness=readiness,
        framing_completeness=round(framing, 3),
        evidence_sufficiency=round(_clamp(evidence_sufficiency), 3),
        uncertainty_level=round(_clamp(uncertainty), 3),
        reversibility_score=round(reversibility, 3),
        value_of_information=_value_of_information(evidence_sufficiency, uncertainty, context),
        escalation_reasons=reasons,
        notes=list(notes),
    )


def _utility(
    *,
    context: DecisionContext,
    expected_benefit: float,
    expected_harm: float,
    rationale: str,
) -> DecisionUtilityEstimate:
    benefit = _clamp(expected_benefit)
    harm = _clamp(expected_harm)
    impact_cost = _IMPACT_COST[context.stakeholder_impact]
    false_positive_cost = _clamp(harm * 0.7 + impact_cost * 0.3)
    false_negative_cost = _clamp((1.0 - benefit) * 0.65 + impact_cost * 0.35)
    delay_cost = _TIME_PRESSURE_DELAY_COST[context.time_pressure]
    net = _clamp(benefit - harm * 0.55 - false_positive_cost * 0.2 - delay_cost * 0.15, low=-1.0, high=1.0)
    return DecisionUtilityEstimate(
        expected_benefit=round(benefit, 3),
        expected_harm=round(harm, 3),
        false_positive_cost=round(false_positive_cost, 3),
        false_negative_cost=round(false_negative_cost, 3),
        delay_cost=round(delay_cost, 3),
        net_utility_score=round(net, 3),
        rationale=rationale,
    )


def _reliance_guidance(
    quality: DecisionQualityAssessment,
    context: DecisionContext,
) -> RelianceGuidance:
    checks = [
        "Verify source references and input hashes.",
        "Review uncertainty, risk, and escalation reasons.",
        "Record a named human decision before consequential action.",
    ]
    if quality.readiness == "escalate":
        return RelianceGuidance(
            posture="escalate",
            rationale="Escalation conditions are present; do not rely on the model output as-is.",
            required_checks=[*checks, context.fallback_action],
        )
    if quality.evidence_sufficiency < 0.55 or quality.uncertainty_level > 0.45:
        return RelianceGuidance(
            posture="defer_for_more_info",
            rationale="Evidence or uncertainty is not strong enough for routine reliance.",
            required_checks=[*checks, quality.value_of_information],
        )
    if quality.readiness == "review":
        return RelianceGuidance(
            posture="challenge_model",
            rationale="The output is usable for review, but the human should actively test assumptions.",
            required_checks=[*checks, "Check at least one plausible alternative before approval."],
        )
    return RelianceGuidance(
        posture="accept_with_review",
        rationale="No escalation condition is present, but the recommendation remains advisory.",
        required_checks=checks,
    )


def _policy_escalation_reasons(
    request: OperationalTwinRequest,
    evidence_bundle: EvidenceBundle,
) -> list[str]:
    checks = evidence_bundle.policy_checks
    reasons = list(checks.governance_findings)
    if not checks.classification_ok:
        reasons.append("classification check failed")
    if not checks.evidence_threshold_ok:
        reasons.append("evidence threshold not met")
    if not checks.safety_ok:
        reasons.append("safety check failed")
    if not checks.stale_source_ok:
        reasons.append("stale source threshold exceeded")
    if not checks.control_marking_ok:
        reasons.append("control marking check failed")
    if not checks.fatigue_overload_ok:
        reasons.append("fatigue overload threshold exceeded")
    if not request.require_human_approval or not checks.human_approval_required:
        reasons.append("human approval gate is disabled")
    return reasons


def _twin_evidence_sufficiency(evidence_bundle: EvidenceBundle) -> float:
    checks = evidence_bundle.policy_checks
    source_count_score = min(len(evidence_bundle.source_artifacts) / 2.0, 1.0)
    observation_count_score = min(len(evidence_bundle.derived_observations) / 3.0, 1.0)
    confidence_score = _average(
        [item.confidence for item in evidence_bundle.derived_observations],
        default=0.45,
    )
    policy_score = _average(
        [
            float(checks.classification_ok),
            float(checks.evidence_threshold_ok),
            float(checks.safety_ok),
            float(checks.stale_source_ok),
            float(checks.control_marking_ok),
            float(checks.fatigue_overload_ok),
        ],
        default=0.0,
    )
    return _clamp(
        source_count_score * 0.2
        + observation_count_score * 0.25
        + confidence_score * 0.35
        + policy_score * 0.2
    )


def _option_benefit(option: ScenarioOption) -> float:
    learning = option.predicted_effect.expected_learning_value
    mission = option.predicted_effect.expected_mission_benefit
    values = [value for value in (learning, mission) if value is not None]
    if not values:
        return option.confidence * 0.55
    return _clamp(max(values) * 0.75 + option.confidence * 0.25)


def _framing_completeness(context: DecisionContext) -> float:
    fields = [
        bool(context.decision_point.strip()),
        bool(context.actor_role.strip()),
        bool(context.objective.strip()),
        bool(context.fallback_action.strip()),
    ]
    base = sum(fields) / len(fields)
    constraint_score = 1.0 if context.constraints else 0.65
    return _clamp(base * 0.8 + constraint_score * 0.2)


def _value_of_information(
    evidence_sufficiency: float,
    uncertainty: float,
    context: DecisionContext,
) -> str:
    if evidence_sufficiency < 0.45:
        return "Collect another independent source or direct observation before approving."
    if uncertainty > 0.55:
        return "Seek disconfirming evidence and compare at least one fallback option."
    if context.time_pressure == "high" and context.reversibility != "irreversible":
        return "Act only through the reversible fallback path if delay would materially increase risk."
    return "Additional information is useful but not required before human review."


def _confidence_score(confidence: Confidence | str) -> float:
    return _CONFIDENCE_SCORE.get(confidence, 0.5)


def _average(values: Sequence[float], *, default: float) -> float:
    if not values:
        return default
    return sum(values) / len(values)


def _clamp(value: float, *, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))
