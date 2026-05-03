from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from .adaptation_store import AdaptationRepository, InMemoryAdaptationRepository
from .audit import AuditLog, AuditSink
from .decision_quality import assess_cognitive_adaptation, assess_scenario_inject
from .models import (
    AgentApprovalDecision,
    CognitiveAdaptationRequest,
    CognitiveAdaptationResponse,
    CognitiveDimension,
    CognitiveDimensionEstimate,
    CognitiveStateSnapshot,
    Confidence,
    ScenarioApprovalRequest,
    ScenarioApprovalResponse,
    ScenarioInjectRecommendation,
    SourceReference,
    TrainingEvidence,
    TraceMetadata,
)
from .registry import DOD_AI_PRINCIPLES, MODEL_VERSIONS, prompt_hash
from .retrieval import ContextRetriever, LocalContextRetriever, RetrievedContext
from .shared_data import (
    InMemorySharedDataSink,
    SharedDataSink,
    build_cognitive_adaptation_update_event,
    build_scenario_approval_update_event,
    canonical_hash,
    dedupe_source_refs,
    input_source_hashes,
)


SKILL_DIMENSIONS: tuple[CognitiveDimension, ...] = (
    "sensemaking",
    "critical_thinking",
    "systems_thinking",
    "leadership_communication",
    "execution_reliability",
    "team_trust",
)

STATE_DIMENSIONS: tuple[CognitiveDimension, ...] = (
    "cognitive_load",
    "sleep_fatigue",
    "nutrition_strain",
)

ALL_DIMENSIONS: tuple[CognitiveDimension, ...] = (*SKILL_DIMENSIONS, *STATE_DIMENSIONS)

_NEGATIVE_KEYWORDS: dict[CognitiveDimension, tuple[str, ...]] = {
    "sensemaking": ("ambiguous", "missed cue", "misread", "conflicting", "unclear picture", "lost context"),
    "critical_thinking": ("assumption", "single option", "failed to question", "confirmation", "hypothesis trap"),
    "systems_thinking": ("second-order", "terrain", "timing", "support", "logistics", "civilian", "comms relay"),
    "leadership_communication": ("unclear order", "backbrief", "handoff", "subordinate", "dissent", "coordination"),
    "execution_reliability": ("late", "missed step", "checklist", "forgot", "rework", "standard"),
    "team_trust": ("trust", "cohesion", "blame", "hesitation", "withheld", "handoff"),
    "cognitive_load": ("overwhelmed", "saturated", "slow", "fixated", "tunnel vision", "stress"),
    "sleep_fatigue": ("fatigue", "sleep", "tired", "microsleep", "exhausted", "night movement"),
    "nutrition_strain": ("hydration", "nutrition", "calorie", "cramp", "heat", "water"),
}

_POSITIVE_KEYWORDS: dict[CognitiveDimension, tuple[str, ...]] = {
    "sensemaking": ("recognized", "reframed", "updated picture", "pattern"),
    "critical_thinking": ("challenged", "alternative", "red team", "assumption check"),
    "systems_thinking": ("anticipated", "second order", "linked", "tradeoff"),
    "leadership_communication": ("clear order", "backbrief complete", "confirmed", "delegated"),
    "execution_reliability": ("on time", "standard met", "disciplined", "clean handoff"),
    "team_trust": ("trusted", "cohesive", "mutual", "supported"),
    "cognitive_load": ("calm", "prioritized", "paced"),
    "sleep_fatigue": ("rested", "recovered", "alert"),
    "nutrition_strain": ("hydrated", "fed", "recovered"),
}

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass
class CognitiveAdaptationService:
    audit_log: AuditSink = field(default_factory=AuditLog)
    shared_data_sink: SharedDataSink = field(default_factory=InMemorySharedDataSink)
    retriever: ContextRetriever = field(default_factory=LocalContextRetriever)
    repository: AdaptationRepository = field(default_factory=InMemoryAdaptationRepository)

    def adapt(self, request: CognitiveAdaptationRequest) -> CognitiveAdaptationResponse:
        adaptation_id = f"adapt-{uuid4()}"
        state = estimate_cognitive_state(request)
        retrieved_context = self.retriever.retrieve(_retrieval_query(state), limit=3)
        source_refs = _adaptation_source_refs(request, retrieved_context)
        recommendations, blocked = direct_scenarios(request, state)
        recommendations = [_with_decision_quality(request, item) for item in recommendations]
        blocked = [_with_decision_quality(request, item) for item in blocked]
        decision_quality, utility_estimate, reliance_guidance = assess_cognitive_adaptation(
            request,
            state,
            recommendations,
            blocked,
        )
        trace = _trace_metadata(request, state, source_refs)
        response = CognitiveAdaptationResponse(
            adaptation_id=adaptation_id,
            mission_id=request.mission_id,
            team_id=request.team_id,
            status="pending_approval" if request.require_human_approval else "completed",
            state=state,
            recommendations=recommendations,
            blocked_recommendations=blocked,
            trace=trace,
            approval_required=request.require_human_approval,
            decision_quality=decision_quality,
            utility_estimate=utility_estimate,
            reliance_guidance=reliance_guidance,
        )
        response = self.repository.save(response)
        self.audit_log.append(
            "cognitive_adaptation_requested",
            {
                "adaptation_id": adaptation_id,
                "mission_id": request.mission_id,
                "team_id": request.team_id,
                "instructor_id": request.instructor_id,
                "primary_dimension": state.primary_development_dimension,
                "recommendation_count": len(recommendations),
                "blocked_count": len(blocked),
            },
        )
        self.shared_data_sink.append_update_event(build_cognitive_adaptation_update_event(request, response))
        return response

    def get(self, adaptation_id: str) -> CognitiveAdaptationResponse | None:
        return self.repository.get(adaptation_id)

    def list_by_mission(self, mission_id: str, *, limit: int = 50) -> list[CognitiveAdaptationResponse]:
        return self.repository.list_by_mission(mission_id, limit=limit)

    def record_approval(
        self,
        adaptation_id: str,
        request: ScenarioApprovalRequest,
    ) -> ScenarioApprovalResponse | None:
        adaptation = self.repository.get(adaptation_id)
        if adaptation is None:
            return None
        recommendation = next(
            (
                item
                for item in adaptation.recommendations
                if item.recommendation_id == request.recommendation_id
            ),
            None,
        )
        if recommendation is None:
            raise ValueError("scenario recommendation not found")
        if recommendation.status == "blocked":
            raise ValueError("blocked recommendations cannot be approved")

        status = "completed" if request.decision is AgentApprovalDecision.approved else "rejected"
        approved_inject = recommendation if request.decision is AgentApprovalDecision.approved else None
        approval = ScenarioApprovalResponse(
            adaptation_id=adaptation_id,
            recommendation_id=request.recommendation_id,
            status=status,
            decision=request.decision,
            approved_inject=approved_inject,
        )
        self.repository.save(adaptation.model_copy(update={"status": status}))
        self.audit_log.append(
            "scenario_inject_decision_recorded",
            {
                "adaptation_id": adaptation_id,
                "recommendation_id": request.recommendation_id,
                "decision": request.decision.value,
                "approver_id": request.approver_id,
            },
        )
        self.shared_data_sink.append_update_event(
            build_scenario_approval_update_event(adaptation, request, approval)
        )
        return approval


def estimate_cognitive_state(request: CognitiveAdaptationRequest) -> CognitiveStateSnapshot:
    estimates = [_estimate_dimension(dimension, request.evidence) for dimension in ALL_DIMENSIONS]
    primary = max(
        (estimate for estimate in estimates if estimate.dimension in SKILL_DIMENSIONS),
        key=lambda estimate: estimate.development_priority,
    )
    fatigue_pressure = 1.0 - _estimate_by_dimension(estimates, "sleep_fatigue").current_score
    load_pressure = 1.0 - _estimate_by_dimension(estimates, "cognitive_load").current_score
    mode = _failure_mode(primary.dimension, fatigue_pressure=max(fatigue_pressure, load_pressure))
    summary = (
        f"Primary developmental target is {primary.dimension.replace('_', ' ')} "
        f"with {primary.confidence.value} confidence. {mode}"
    )
    return CognitiveStateSnapshot(
        snapshot_id=f"state-{uuid4()}",
        mission_id=request.mission_id,
        team_id=request.team_id,
        target_soldier_ids=request.target_soldier_ids,
        estimates=estimates,
        primary_development_dimension=primary.dimension,
        likely_failure_mode=mode,
        state_summary=summary,
    )


def direct_scenarios(
    request: CognitiveAdaptationRequest,
    state: CognitiveStateSnapshot,
) -> tuple[list[ScenarioInjectRecommendation], list[ScenarioInjectRecommendation]]:
    fatigue_pressure = 1.0 - _estimate_by_dimension(state.estimates, "sleep_fatigue").current_score
    load_pressure = 1.0 - _estimate_by_dimension(state.estimates, "cognitive_load").current_score
    pressure = max(fatigue_pressure, load_pressure)
    templates = _scenario_templates(state.primary_development_dimension)
    allowed: list[ScenarioInjectRecommendation] = []
    blocked: list[ScenarioInjectRecommendation] = []
    for inject_type, template in templates:
        recommendation = _build_recommendation(
            request,
            state,
            inject_type=inject_type,
            template=template,
            fatigue_pressure=pressure,
        )
        audited = _audit_recommendation(recommendation, request)
        if audited.status == "blocked":
            blocked.append(audited)
        else:
            allowed.append(audited)
    return allowed, blocked


def _with_decision_quality(
    request: CognitiveAdaptationRequest,
    recommendation: ScenarioInjectRecommendation,
) -> ScenarioInjectRecommendation:
    decision_quality, utility_estimate, reliance_guidance = assess_scenario_inject(
        request,
        recommendation,
    )
    return recommendation.model_copy(
        update={
            "decision_quality": decision_quality,
            "utility_estimate": utility_estimate,
            "reliance_guidance": reliance_guidance,
        }
    )


def _estimate_dimension(
    dimension: CognitiveDimension,
    evidence: Sequence[TrainingEvidence],
) -> CognitiveDimensionEstimate:
    scores: list[float] = []
    evidence_refs: list[str] = []
    for item in evidence:
        score = _score_evidence_for_dimension(dimension, item)
        if score is None:
            continue
        scores.append(score)
        evidence_refs.append(_evidence_ref(item))

    current_score = _clamp(sum(scores) / len(scores)) if scores else 0.72
    priority = _clamp((1.0 - current_score) * 0.82 + min(len(scores), 4) * 0.04)
    confidence = _confidence(len(scores))
    trend = _trend(evidence)
    rationale = _dimension_rationale(dimension, current_score, len(scores))
    return CognitiveDimensionEstimate(
        dimension=dimension,
        current_score=current_score,
        development_priority=priority,
        confidence=confidence,
        trend=trend,
        rationale=rationale,
        evidence_refs=evidence_refs,
    )


def _score_evidence_for_dimension(
    dimension: CognitiveDimension,
    evidence: TrainingEvidence,
) -> float | None:
    metric_value = evidence.metrics.get(dimension)
    if metric_value is not None:
        return _clamp(metric_value)

    text = f"{evidence.text} {' '.join(evidence.tags)}".lower()
    negative_hits = sum(keyword in text for keyword in _NEGATIVE_KEYWORDS[dimension])
    positive_hits = sum(keyword in text for keyword in _POSITIVE_KEYWORDS[dimension])
    score: float | None = None
    if negative_hits or positive_hits:
        score = _clamp(0.72 - negative_hits * 0.14 + positive_hits * 0.08)

    if dimension == "sleep_fatigue":
        if "sleep_hours" in evidence.metrics:
            score = _clamp(float(evidence.metrics["sleep_hours"]) / 7.0)
        if "hours_awake" in evidence.metrics:
            score = min(score if score is not None else 1.0, _clamp(1.0 - max(evidence.metrics["hours_awake"] - 12.0, 0.0) / 12.0))
    elif dimension == "cognitive_load":
        for key in ("stress_level", "cognitive_load", "load"):
            if key in evidence.metrics:
                score = _clamp(1.0 - evidence.metrics[key])
    elif dimension == "nutrition_strain":
        for key in ("hydration", "nutrition", "calorie_state"):
            if key in evidence.metrics:
                score = _clamp(evidence.metrics[key])
    return score


def _build_recommendation(
    request: CognitiveAdaptationRequest,
    state: CognitiveStateSnapshot,
    *,
    inject_type: str,
    template: dict[str, str | float],
    fatigue_pressure: float,
) -> ScenarioInjectRecommendation:
    risk_base = float(template["safety_risk"])
    safety_risk = _clamp(risk_base + (0.18 if fatigue_pressure > 0.55 and inject_type != "skill_isolation" else 0.0))
    fatigue_risk = _clamp(fatigue_pressure + (0.12 if inject_type == "direct_pressure" else 0.04))
    exposure_risk = 0.18 if request.target_soldier_ids else 0.1
    risk_level = _risk_level(max(safety_risk, fatigue_risk * 0.85))
    doctrine_refs = _doctrine_refs(state.primary_development_dimension)
    return ScenarioInjectRecommendation(
        recommendation_id=f"scenario-{uuid4()}",
        title=str(template["title"]),
        inject_type=inject_type,  # type: ignore[arg-type]
        target_dimension=state.primary_development_dimension,
        proposed_inject=str(template["inject"]),
        expected_developmental_effect=str(template["effect"]),
        rationale=str(template["rationale"]),
        doctrine_refs=doctrine_refs,
        evidence_refs=_primary_evidence_refs(state),
        safety_checks=[
            "Instructor approval required before execution.",
            "Do not intensify if fatigue, weather, or medical risk changes.",
            "Keep developmental data separate from personnel-selection decisions.",
        ],
        risk_level=risk_level,
        safety_risk=safety_risk,
        fatigue_risk=fatigue_risk,
        unfair_exposure_risk=exposure_risk,
        expected_learning_gain=float(template["learning_gain"]),
        transfer_value=float(template["transfer_value"]),
        confidence=_estimate_by_dimension(state.estimates, state.primary_development_dimension).confidence,
    )


def _audit_recommendation(
    recommendation: ScenarioInjectRecommendation,
    request: CognitiveAdaptationRequest,
) -> ScenarioInjectRecommendation:
    block_reason: str | None = None
    if _RISK_ORDER[recommendation.risk_level] > _RISK_ORDER[request.constraints.max_safety_risk]:
        block_reason = (
            f"Risk level {recommendation.risk_level} exceeds configured "
            f"maximum {request.constraints.max_safety_risk}."
        )
    elif recommendation.inject_type in request.constraints.blocked_inject_types:
        block_reason = f"Inject type {recommendation.inject_type} is blocked by constraint."
    elif not request.constraints.allow_environmental_stress and "terrain" in recommendation.proposed_inject.lower():
        block_reason = "Environmental stress is disabled by constraint."

    if block_reason is None:
        return recommendation
    return recommendation.model_copy(
        update={
            "status": "blocked",
            "block_reason": block_reason,
        }
    )


def _scenario_templates(
    dimension: CognitiveDimension,
) -> tuple[tuple[str, dict[str, str | float]], ...]:
    direct = {
        "sensemaking": "Introduce two conflicting spot reports and require the leader to state what changed in the situation model.",
        "critical_thinking": "Inject a plausible but false confirming report and require an assumption check before action.",
        "systems_thinking": "Delay the comms relay while adding civilian movement on the flank and a timing change for support.",
        "leadership_communication": "Create subordinate dissent during a handoff and require a concise backbrief.",
        "execution_reliability": "Interrupt the checklist flow with a time-sensitive task-standard confirmation.",
        "team_trust": "Force a cross-team handoff where success depends on sharing incomplete information early.",
    }.get(dimension, "Add a focused cue that exposes the current developmental weakness.")
    isolate = {
        "sensemaking": "Pause tactical escalation and run a short cue-sort drill using the last three observations.",
        "critical_thinking": "Give a low-noise branch plan and require two alternatives plus one disconfirming cue.",
        "systems_thinking": "Strip contact pressure and have the leader map terrain, timing, support, and second-order effects.",
        "leadership_communication": "Run a backbrief-only repetition with role clarity and confirmation checks.",
        "execution_reliability": "Repeat the task standard in a controlled lane with one measurable constraint.",
        "team_trust": "Run a low-pressure information-sharing repetition with explicit trust behaviors.",
    }.get(dimension, "Isolate the weak sub-skill in a lower-noise repetition.")
    transfer = {
        "sensemaking": "Move the same ambiguous cue pattern into a different terrain and time window.",
        "critical_thinking": "Transfer the assumption check to a new problem with a different likely first answer.",
        "systems_thinking": "Test the same second-order relationship in a logistics or casualty-evacuation branch.",
        "leadership_communication": "Transfer the communication demand to a dispersed element with delayed feedback.",
        "execution_reliability": "Transfer the task standard to a different role while preserving timing pressure.",
        "team_trust": "Transfer the trust demand to a new partner pair and compare handoff quality.",
    }.get(dimension, "Test transfer of the weak sub-skill in a different tactical context.")
    return (
        (
            "direct_pressure",
            {
                "title": "Target the weak cognitive dimension directly",
                "inject": direct,
                "effect": "Develops the currently weakest cognitive dimension under realistic mission pressure.",
                "rationale": "Adaptive training should target measured understanding instead of raising difficulty blindly.",
                "safety_risk": 0.44,
                "learning_gain": 0.78,
                "transfer_value": 0.58,
            },
        ),
        (
            "skill_isolation",
            {
                "title": "Isolate the sub-skill in a lower-noise context",
                "inject": isolate,
                "effect": "Builds the weak sub-skill without adding unnecessary environmental or social load.",
                "rationale": "Lower-noise repetitions help separate skill gaps from state-dependent fatigue or overload.",
                "safety_risk": 0.18,
                "learning_gain": 0.64,
                "transfer_value": 0.38,
            },
        ),
        (
            "transfer_test",
            {
                "title": "Test transfer in a different tactical situation",
                "inject": transfer,
                "effect": "Checks whether the soldier can apply the same cognitive move under changed context.",
                "rationale": "Transfer value matters because training should improve future performance, not only the current lane.",
                "safety_risk": 0.36,
                "learning_gain": 0.62,
                "transfer_value": 0.82,
            },
        ),
    )


def _adaptation_source_refs(
    request: CognitiveAdaptationRequest,
    retrieved_context: Sequence[RetrievedContext],
) -> list[SourceReference]:
    refs = [
        SourceReference(
            ref=f"postgres://missions_current/{request.mission_id}",
            role="mission",
            source_hash=canonical_hash({"mission_id": request.mission_id}),
        )
    ]
    refs.extend(
        SourceReference(
            ref=_evidence_ref(evidence),
            role=f"evidence:{evidence.source_type}",
            source_hash=canonical_hash(evidence.model_dump(mode="json")),
            metadata={
                "evidence_id": evidence.evidence_id,
                "soldier_ids": evidence.soldier_ids,
                "task_code": evidence.task_code,
            },
        )
        for evidence in request.evidence
    )
    refs.extend(
        SourceReference(
            ref=f"pgvector://system2_context_chunks/{context.metadata.get('chunk_id', context.title)}",
            role="doctrine_or_context",
            source_hash=canonical_hash(
                {"source": context.source, "title": context.title, "content": context.content}
            ),
            metadata={"source": context.source, "score": context.score},
        )
        for context in retrieved_context
    )
    return dedupe_source_refs(refs)


def _trace_metadata(
    request: CognitiveAdaptationRequest,
    state: CognitiveStateSnapshot,
    source_refs: Sequence[SourceReference],
) -> TraceMetadata:
    return TraceMetadata(
        model_versions=MODEL_VERSIONS,
        feature_hash=canonical_hash(
            {
                "mission_id": request.mission_id,
                "team_id": request.team_id,
                "evidence_ids": [evidence.evidence_id for evidence in request.evidence],
                "state": state.model_dump(mode="json"),
            }
        ),
        prompt_hash=prompt_hash(),
        seed=0,
        dod_ai_principles=DOD_AI_PRINCIPLES,
        source_refs=list(source_refs),
        input_source_hashes=input_source_hashes(source_refs),
    )


def _retrieval_query(state: CognitiveStateSnapshot) -> str:
    return (
        f"{state.primary_development_dimension} adaptive training "
        "AAR H2F safety doctrine instructor approval"
    )


def _dimension_rationale(dimension: CognitiveDimension, score: float, count: int) -> str:
    if count == 0:
        return f"No direct evidence touched {dimension.replace('_', ' ')}; neutral prior retained."
    if score < 0.45:
        return f"Evidence indicates a near-term developmental weakness in {dimension.replace('_', ' ')}."
    if score < 0.65:
        return f"Evidence suggests {dimension.replace('_', ' ')} should be monitored and trained."
    return f"Evidence does not indicate an immediate weakness in {dimension.replace('_', ' ')}."


def _failure_mode(dimension: CognitiveDimension, *, fatigue_pressure: float) -> str:
    fatigue_note = (
        " Fatigue or cognitive load is elevated, so avoid interpreting this as fixed talent."
        if fatigue_pressure > 0.45
        else ""
    )
    mode = {
        "sensemaking": "Likely failure mode is weak cue integration under ambiguity.",
        "critical_thinking": "Likely failure mode is premature closure around the first plausible answer.",
        "systems_thinking": "Likely failure mode is missing second-order relationships across people, terrain, timing, or support.",
        "leadership_communication": "Likely failure mode is unclear intent transfer and weak backbrief discipline.",
        "execution_reliability": "Likely failure mode is task-standard drift under time pressure.",
        "team_trust": "Likely failure mode is degraded information sharing and handoff confidence.",
    }.get(dimension, "Likely failure mode is state-dependent performance degradation.")
    return mode + fatigue_note


def _doctrine_refs(dimension: CognitiveDimension) -> list[str]:
    refs = [
        "ADP 6-22 Army Leadership Requirements Model",
        "FM 7-0 training assessment and AAR cycle",
        "DoD Responsible AI principles: human judgment, traceability, governability",
    ]
    if dimension in {"sensemaking", "critical_thinking", "systems_thinking"}:
        refs.append("Project Athena cognitive constructs: sensemaking, critical thinking, systems thinking")
    if dimension in {"cognitive_load", "sleep_fatigue", "nutrition_strain"}:
        refs.append("FM 7-22 H2F readiness domains")
    return refs


def _primary_evidence_refs(state: CognitiveStateSnapshot) -> list[str]:
    primary = _estimate_by_dimension(state.estimates, state.primary_development_dimension)
    return primary.evidence_refs[:5]


def _estimate_by_dimension(
    estimates: Sequence[CognitiveDimensionEstimate],
    dimension: CognitiveDimension,
) -> CognitiveDimensionEstimate:
    return next(estimate for estimate in estimates if estimate.dimension == dimension)


def _confidence(evidence_count: int) -> Confidence:
    if evidence_count >= 3:
        return Confidence.high
    if evidence_count >= 1:
        return Confidence.medium
    return Confidence.low


def _trend(evidence: Sequence[TrainingEvidence]) -> str:
    joined = " ".join(" ".join([item.text, *item.tags]) for item in evidence)
    text = joined.lower()
    if "improving" in text or "recovered" in text:
        return "improving"
    if "declining" in text or "worse" in text or "degraded" in text:
        return "declining"
    return "stable" if evidence else "unknown"


def _risk_level(value: float) -> str:
    if value >= 0.68:
        return "high"
    if value >= 0.34:
        return "medium"
    return "low"


def _evidence_ref(evidence: TrainingEvidence) -> str:
    return evidence.source_ref or f"evidence://{evidence.source_type}/{evidence.evidence_id}"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
