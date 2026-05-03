from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


RoleName = Literal[
    "team_leader",
    "assistant_team_leader",
    "breacher",
    "medic",
    "marksman",
    "comms",
    "assaulter",
]


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class RiskCategory(str, Enum):
    experience = "experience"
    medical = "medical"
    model_disagreement = "model_disagreement"


class AgentRunStatus(str, Enum):
    queued = "queued"
    running = "running"
    awaiting_approval = "awaiting_approval"
    completed = "completed"
    rejected = "rejected"
    failed = "failed"


class AgentStepStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class AgentApprovalDecision(str, Enum):
    approved = "approved"
    rejected = "rejected"


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


DecisionTimePressure = Literal["low", "medium", "high"]
DecisionReversibility = Literal["reversible", "partially_reversible", "irreversible"]
DecisionImpact = Literal["low", "medium", "high"]
DecisionReadiness = Literal["ready", "review", "escalate"]
ReliancePosture = Literal[
    "accept_with_review",
    "challenge_model",
    "defer_for_more_info",
    "escalate",
]


class DecisionContext(StrictBaseModel):
    decision_point: str = "operational recommendation"
    actor_role: str = "authorized human reviewer"
    objective: str = "Improve decision quality while preserving human accountability."
    constraints: list[str] = Field(default_factory=list)
    time_pressure: DecisionTimePressure = "medium"
    reversibility: DecisionReversibility = "partially_reversible"
    stakeholder_impact: DecisionImpact = "medium"
    fallback_action: str = "Pause and route to manual review."


class DecisionQualityAssessment(StrictBaseModel):
    readiness: DecisionReadiness
    framing_completeness: float = Field(ge=0, le=1)
    evidence_sufficiency: float = Field(ge=0, le=1)
    uncertainty_level: float = Field(ge=0, le=1)
    reversibility_score: float = Field(ge=0, le=1)
    value_of_information: str
    escalation_reasons: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class DecisionUtilityEstimate(StrictBaseModel):
    expected_benefit: float = Field(ge=0, le=1)
    expected_harm: float = Field(ge=0, le=1)
    false_positive_cost: float = Field(ge=0, le=1)
    false_negative_cost: float = Field(ge=0, le=1)
    delay_cost: float = Field(ge=0, le=1)
    net_utility_score: float = Field(ge=-1, le=1)
    rationale: str


class RelianceGuidance(StrictBaseModel):
    posture: ReliancePosture
    rationale: str
    required_checks: list[str] = Field(default_factory=list)
    override_allowed: bool = True
    human_accountability: str = "A named human remains accountable for any consequential action."


def default_decision_context() -> DecisionContext:
    return DecisionContext()


def default_decision_quality_assessment() -> DecisionQualityAssessment:
    return DecisionQualityAssessment(
        readiness="review",
        framing_completeness=0.5,
        evidence_sufficiency=0.5,
        uncertainty_level=0.5,
        reversibility_score=0.5,
        value_of_information="Review source evidence and collect more information if uncertainty remains material.",
        notes=["Default assessment; runtime evaluator did not provide a lane-specific assessment."],
    )


def default_decision_utility_estimate() -> DecisionUtilityEstimate:
    return DecisionUtilityEstimate(
        expected_benefit=0.5,
        expected_harm=0.5,
        false_positive_cost=0.5,
        false_negative_cost=0.5,
        delay_cost=0.5,
        net_utility_score=0.0,
        rationale="Default utility estimate; runtime evaluator did not provide a lane-specific estimate.",
    )


def default_reliance_guidance() -> RelianceGuidance:
    return RelianceGuidance(
        posture="challenge_model",
        rationale="Default guidance requires human review before relying on the output.",
        required_checks=["Confirm the evidence, uncertainty, and escalation conditions before action."],
    )


class Soldier(StrictBaseModel):
    soldier_id: str
    unit_id: str
    mos: str
    age_years: int = Field(ge=18, le=45)
    two_mile_run_sec: int = Field(ge=600, le=1500)
    self_efficacy_score: float = Field(ge=1, le=5)
    peer_rating_z: float
    home_unit_ranger_density: float = Field(ge=0, le=1)
    acft_score: int = Field(ge=300, le=600)
    operational_readiness: float = Field(ge=0, le=1)
    prior_missions: int = Field(ge=0)
    medical_risk: float = Field(ge=0, le=1)
    landing_asymmetry_score: float = Field(ge=0, le=1)
    hip_extension_power_w: float = Field(gt=0)
    change_of_direction_index: float = Field(ge=0, le=1)
    fatigue_index: float = Field(ge=0, le=1)
    sandbox_score: float = Field(ge=0, le=1)
    protected_race: str | None = None
    protected_gender: str | None = None
    milestones: dict[str, int] = Field(default_factory=dict)
    competencies: dict[str, int] = Field(default_factory=dict)


class RoleRequirement(StrictBaseModel):
    slot_id: str
    role: RoleName
    required_mos: str | None = None
    min_acft: int = 450


class ScoreRequest(StrictBaseModel):
    mission_id: str = "direct-action-raid"
    candidate_pool_id: str | None = None
    candidate_count: int = Field(default=80, ge=14, le=5000)
    candidates: list[Soldier] | None = None
    seed: int = 42
    roles: list[RoleRequirement] | None = None
    decision_context: DecisionContext | None = None


CognitiveDimension = Literal[
    "sensemaking",
    "critical_thinking",
    "systems_thinking",
    "leadership_communication",
    "execution_reliability",
    "cognitive_load",
    "sleep_fatigue",
    "nutrition_strain",
    "team_trust",
]

EvidenceSourceType = Literal[
    "voice_note",
    "transcript",
    "ocr_text",
    "checklist",
    "patrol_summary",
    "aar",
    "weather",
    "terrain",
    "structured_event",
]


class TrainingEvidence(StrictBaseModel):
    evidence_id: str = Field(min_length=1)
    source_type: EvidenceSourceType
    text: str = Field(min_length=1, max_length=20000)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    soldier_ids: list[str] = Field(default_factory=list)
    team_id: str | None = None
    task_code: str | None = None
    tags: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    source_ref: str | None = None


class AdaptationConstraints(StrictBaseModel):
    max_safety_risk: Literal["low", "medium", "high"] = "medium"
    allow_environmental_stress: bool = True
    blocked_inject_types: list[str] = Field(default_factory=list)


class CognitiveAdaptationRequest(StrictBaseModel):
    mission_id: str = Field(min_length=1)
    instructor_id: str = Field(min_length=1)
    team_id: str = Field(min_length=1)
    target_soldier_ids: list[str] = Field(default_factory=list)
    phase: str | None = None
    evidence: list[TrainingEvidence] = Field(min_length=1)
    constraints: AdaptationConstraints = Field(default_factory=AdaptationConstraints)
    require_human_approval: bool = True
    decision_context: DecisionContext | None = None


class CognitiveDimensionEstimate(StrictBaseModel):
    dimension: CognitiveDimension
    current_score: float = Field(ge=0, le=1)
    development_priority: float = Field(ge=0, le=1)
    confidence: Confidence
    trend: Literal["improving", "stable", "declining", "unknown"]
    rationale: str
    evidence_refs: list[str] = Field(default_factory=list)


class CognitiveStateSnapshot(StrictBaseModel):
    snapshot_id: str
    mission_id: str
    team_id: str
    target_soldier_ids: list[str]
    estimates: list[CognitiveDimensionEstimate]
    primary_development_dimension: CognitiveDimension
    likely_failure_mode: str
    state_summary: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ScenarioInjectRecommendation(StrictBaseModel):
    recommendation_id: str
    title: str
    inject_type: Literal["direct_pressure", "skill_isolation", "transfer_test"]
    target_dimension: CognitiveDimension
    proposed_inject: str
    expected_developmental_effect: str
    rationale: str
    doctrine_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    safety_checks: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"]
    safety_risk: float = Field(ge=0, le=1)
    fatigue_risk: float = Field(ge=0, le=1)
    unfair_exposure_risk: float = Field(ge=0, le=1)
    expected_learning_gain: float = Field(ge=0, le=1)
    transfer_value: float = Field(ge=0, le=1)
    confidence: Confidence
    status: Literal["pending_approval", "blocked"] = "pending_approval"
    block_reason: str | None = None
    decision_quality: DecisionQualityAssessment = Field(
        default_factory=default_decision_quality_assessment
    )
    utility_estimate: DecisionUtilityEstimate = Field(default_factory=default_decision_utility_estimate)
    reliance_guidance: RelianceGuidance = Field(default_factory=default_reliance_guidance)


class CognitiveAdaptationResponse(StrictBaseModel):
    adaptation_id: str
    mission_id: str
    team_id: str
    status: Literal["pending_approval", "completed", "rejected", "failed"]
    state: CognitiveStateSnapshot
    recommendations: list[ScenarioInjectRecommendation]
    blocked_recommendations: list[ScenarioInjectRecommendation] = Field(default_factory=list)
    trace: TraceMetadata
    approval_required: bool = True
    decision_quality: DecisionQualityAssessment = Field(
        default_factory=default_decision_quality_assessment
    )
    utility_estimate: DecisionUtilityEstimate = Field(default_factory=default_decision_utility_estimate)
    reliance_guidance: RelianceGuidance = Field(default_factory=default_reliance_guidance)


class ScenarioApprovalRequest(StrictBaseModel):
    recommendation_id: str = Field(min_length=1)
    decision: AgentApprovalDecision
    approver_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class ScenarioApprovalResponse(StrictBaseModel):
    adaptation_id: str
    recommendation_id: str
    status: Literal["completed", "rejected"]
    decision: AgentApprovalDecision
    approved_inject: ScenarioInjectRecommendation | None = None
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RiskFactor(StrictBaseModel):
    category: RiskCategory
    label: str
    detail: str


class CandidateAssessment(StrictBaseModel):
    slot_id: str
    role: RoleName
    soldier_id: str
    fit_score: float = Field(ge=0, le=1)
    p_success_tabpfn: float = Field(ge=0, le=1)
    p_success_bayes_mean: float = Field(ge=0, le=1)
    model_disagreement: float = Field(ge=0, le=1)
    p_success_bayes_ci: tuple[float, float]
    narrative: str
    key_strengths: list[str]
    risk_factors: list[RiskFactor]
    second_choice_id: str | None = None
    confidence: Confidence


class FairnessAudit(StrictBaseModel):
    status: Literal["pass", "halt"]
    counterfactual_violation_rate: float
    counterfactual_threshold: float = 0.05
    demographic_parity_delta: float
    equalized_odds_delta: float
    proxy_features: list[str]
    notes: list[str]


class CareerYear(StrictBaseModel):
    year: int
    recommended_assignment: str
    p_success: float = Field(ge=0, le=1)
    rationale: str


class CareerForecast(StrictBaseModel):
    soldier_id: str
    horizon_years: int = 5
    path: list[CareerYear]


class SourceReference(StrictBaseModel):
    ref: str = Field(min_length=1)
    role: str = Field(min_length=1)
    source_hash: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TraceMetadata(StrictBaseModel):
    model_versions: dict[str, str]
    feature_hash: str
    prompt_hash: str
    seed: int
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    dod_ai_principles: dict[str, str]
    calibration_bins: list[dict[str, float | int]] = Field(default_factory=list)
    disagreement_histogram: list[dict[str, float | int]] = Field(default_factory=list)
    source_refs: list[SourceReference] = Field(default_factory=list)
    input_source_hashes: dict[str, str] = Field(default_factory=dict)
    context_adjustments: list[dict[str, Any]] = Field(default_factory=list)


class RosterRecommendation(StrictBaseModel):
    mission_id: str
    roster: list[CandidateAssessment]
    second_choice_roster: list[CandidateAssessment]
    fairness_audit: FairnessAudit
    career_forecast: CareerForecast
    trace: TraceMetadata
    decision_quality: DecisionQualityAssessment = Field(
        default_factory=default_decision_quality_assessment
    )
    utility_estimate: DecisionUtilityEstimate = Field(default_factory=default_decision_utility_estimate)
    reliance_guidance: RelianceGuidance = Field(default_factory=default_reliance_guidance)


class AgentRunRequest(StrictBaseModel):
    objective: Literal["mission_roster_recommendation"] = "mission_roster_recommendation"
    score_request: ScoreRequest
    require_human_approval: bool = True
    decision_context: DecisionContext | None = None


class AgentStep(StrictBaseModel):
    name: str
    status: AgentStepStatus
    summary: str
    evidence: dict[str, object] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None


class AgentApprovalRequest(StrictBaseModel):
    decision: AgentApprovalDecision
    approver_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class AgentApproval(StrictBaseModel):
    decision: AgentApprovalDecision
    approver_id: str
    rationale: str
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ContextChunkInput(StrictBaseModel):
    chunk_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    metadata: dict[str, object] = Field(default_factory=dict)
    embedding: list[float] | None = None


class ContextIngestRequest(StrictBaseModel):
    chunks: list[ContextChunkInput] = Field(min_length=1)


class ContextIngestResult(StrictBaseModel):
    backend: str
    chunk_count: int
    chunk_ids: list[str]


class GraphFactInput(StrictBaseModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphIngestRequest(StrictBaseModel):
    facts: list[GraphFactInput] = Field(min_length=1)


class GraphIngestResult(StrictBaseModel):
    backend: str
    fact_count: int


TwinMode = Literal["training", "mission"]

TwinArtifactKind = Literal[
    "audio",
    "transcript",
    "document_image",
    "ocr_text",
    "telemetry",
    "weather",
    "sleep_food_log",
    "photo",
    "manual_note",
]

TwinObservationKind = Literal[
    "voice_fact",
    "ocr_fact",
    "telemetry_fact",
    "weather_fact",
    "sleep_food_fact",
    "photo_fact",
    "manual_note",
]

TwinSubjectType = Literal["person", "team", "mission", "environment"]
ScenarioOptionType = Literal["training_inject", "rehearsal_variant", "mission_coa"]
ScenarioCriticStatus = Literal["pass", "modify", "escalate", "reject"]
ScenarioOptionStatus = Literal["draft", "approved", "rejected", "escalated"]
TwinDecisionValue = Literal["approved", "rejected", "escalated"]


class ControlProperties(StrictBaseModel):
    classification_marking: str = "UNCLASSIFIED//TRAINING"
    releasability: str = "TRAINING"
    need_to_know_domain: str = "synthetic-training"
    source_handling_code: str = "SYNTHETIC"


class TwinSubjectRef(StrictBaseModel):
    subject_type: TwinSubjectType
    subject_id: str = Field(min_length=1)


class ArtifactInput(StrictBaseModel):
    artifact_id: str | None = None
    kind: TwinArtifactKind
    uri: str | None = None
    content: str | None = Field(default=None, max_length=20000)
    captured_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_system: str = "field_tablet"
    controls: ControlProperties = Field(default_factory=ControlProperties)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactRecord(StrictBaseModel):
    artifact_id: str
    kind: TwinArtifactKind
    uri: str | None = None
    sha256: str
    captured_at_utc: datetime
    source_system: str
    controls: ControlProperties
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObservationInput(StrictBaseModel):
    observation_id: str | None = None
    subject_ref: TwinSubjectRef | None = None
    source_artifact_ids: list[str] = Field(default_factory=list)
    kind: TwinObservationKind
    content: dict[str, Any]
    timestamp_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    geo: dict[str, Any] | None = None
    confidence: float = Field(default=0.85, ge=0, le=1)
    controls: ControlProperties = Field(default_factory=ControlProperties)


class ObservationRecord(StrictBaseModel):
    observation_id: str
    mission_id: str
    subject_ref: TwinSubjectRef | None = None
    source_artifact_ids: list[str] = Field(min_length=1)
    kind: TwinObservationKind
    content: dict[str, Any]
    timestamp_utc: datetime
    geo: dict[str, Any] | None = None
    confidence: float = Field(ge=0, le=1)
    controls: ControlProperties


class EnvironmentState(StrictBaseModel):
    environment_state_id: str
    mission_id: str
    weather: str | None = None
    terrain: str | None = None
    visibility: str | None = None
    temperature_c: float | None = None
    precipitation: str | None = None
    wind_speed: float | None = None
    captured_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    controls: ControlProperties = Field(default_factory=ControlProperties)


class OperationalStateVector(StrictBaseModel):
    fatigue_burden: float = Field(ge=0, le=1)
    situational_clarity: float = Field(ge=0, le=1)
    cohesion: float = Field(ge=0, le=1)
    leader_decision_quality: float = Field(ge=0, le=1)
    mission_tempo_risk: float = Field(ge=0, le=1)
    training_challenge_gap: float = Field(ge=-1, le=1)


class StateUncertainty(StrictBaseModel):
    overall: float = Field(ge=0, le=1)
    by_field: dict[str, float] = Field(default_factory=dict)


class StateEstimate(StrictBaseModel):
    state_estimate_id: str
    subject_type: Literal["person", "team", "mission"]
    subject_id: str = Field(min_length=1)
    state_vector: OperationalStateVector
    uncertainty: StateUncertainty
    evidence_bundle_id: str
    model_version: str
    valid_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    controls: ControlProperties = Field(default_factory=ControlProperties)


class EvidenceBundleArtifact(StrictBaseModel):
    artifact_id: str
    kind: TwinArtifactKind
    sha256: str
    captured_at_utc: datetime
    source_system: str


class EvidenceBundleObservation(StrictBaseModel):
    observation_id: str
    kind: TwinObservationKind
    confidence: float = Field(ge=0, le=1)
    summary: str


class EvidenceBundleModelTrace(StrictBaseModel):
    stage: str
    model_name: str
    model_version: str
    output_confidence: float = Field(ge=0, le=1)


class EvidenceBundlePolicyChecks(StrictBaseModel):
    classification_ok: bool
    evidence_threshold_ok: bool
    safety_ok: bool
    human_approval_required: bool = True
    stale_source_ok: bool = True
    control_marking_ok: bool = True
    fatigue_overload_ok: bool = True
    governance_findings: list[str] = Field(default_factory=list)


class EvidenceBundleHashChain(StrictBaseModel):
    previous_action_hash: str
    current_action_hash: str


class EvidenceBundle(StrictBaseModel):
    bundle_id: str
    created_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    claim_type: str
    claim_text: str
    mission_id: str
    subject_refs: list[TwinSubjectRef] = Field(default_factory=list)
    source_artifacts: list[EvidenceBundleArtifact] = Field(default_factory=list)
    derived_observations: list[EvidenceBundleObservation] = Field(default_factory=list)
    state_inputs: dict[str, Any] = Field(default_factory=dict)
    models: list[EvidenceBundleModelTrace] = Field(default_factory=list)
    policy_checks: EvidenceBundlePolicyChecks
    hash_chain: EvidenceBundleHashChain
    controls: ControlProperties = Field(default_factory=ControlProperties)


class ScenarioPredictedEffect(StrictBaseModel):
    target_state_change: str
    expected_learning_value: float | None = Field(default=None, ge=0, le=1)
    expected_mission_benefit: float | None = Field(default=None, ge=0, le=1)


class ScenarioOption(StrictBaseModel):
    scenario_option_id: str
    mission_id: str
    option_type: ScenarioOptionType
    title: str
    narrative: str
    predicted_effect: ScenarioPredictedEffect
    risk_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    critic_status: ScenarioCriticStatus
    critic_reasons: list[str] = Field(default_factory=list)
    evidence_bundle_id: str
    status: ScenarioOptionStatus = "draft"
    controls: ControlProperties = Field(default_factory=ControlProperties)
    decision_quality: DecisionQualityAssessment = Field(
        default_factory=default_decision_quality_assessment
    )
    utility_estimate: DecisionUtilityEstimate = Field(default_factory=default_decision_utility_estimate)
    reliance_guidance: RelianceGuidance = Field(default_factory=default_reliance_guidance)


class LessonLearned(StrictBaseModel):
    lesson_id: str
    mission_id: str
    category: str
    summary: str
    root_cause: str
    recommended_training_delta: str
    recommended_mission_delta: str
    severity: Literal["low", "medium", "high"] = "medium"
    status: Literal["draft", "approved"] = "draft"
    evidence_bundle_id: str
    created_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    controls: ControlProperties = Field(default_factory=ControlProperties)


class OperationalTwinDecision(StrictBaseModel):
    decision_id: str
    target_object_type: Literal["scenario_option"] = "scenario_option"
    target_object_id: str
    actor_id: str
    decision: TwinDecisionValue
    comment: str
    timestamp_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    evidence_bundle_id: str


class OperationalTwinOutcome(StrictBaseModel):
    outcome_id: str
    selected_option_id: str
    observed_outcome_summary: str
    instructor_rating: int = Field(ge=1, le=5)
    safety_incident: bool = False
    targeted_state_improvement_estimate: float = Field(ge=-1, le=1)
    aar_notes: str
    actor_id: str
    recorded_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    evidence_bundle_id: str
    controls: ControlProperties = Field(default_factory=ControlProperties)


class OperationalTwinRequest(StrictBaseModel):
    mission_id: str = Field(min_length=1)
    operator_id: str = Field(min_length=1)
    mode: TwinMode = "training"
    team_id: str = Field(min_length=1)
    training_objective: str | None = None
    artifacts: list[ArtifactInput] = Field(default_factory=list)
    observations: list[ObservationInput] = Field(default_factory=list)
    environment: dict[str, Any] = Field(default_factory=dict)
    controls: ControlProperties = Field(default_factory=ControlProperties)
    require_human_approval: bool = True
    decision_context: DecisionContext | None = None


class AgentStageTrace(StrictBaseModel):
    stage: str
    provider: str
    model: str
    status: Literal["completed", "fallback", "failed"]
    summary: str
    error: str | None = None
    input_hash: str | None = None
    output_hash: str | None = None
    duration_ms: int | None = None
    fallback_reason: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OperationalTwinResponse(StrictBaseModel):
    twin_run_id: str
    mission_id: str
    mode: TwinMode
    team_id: str
    status: Literal["draft", "partially_decided", "completed", "outcome_recorded"] = "draft"
    artifacts: list[ArtifactRecord]
    observations: list[ObservationRecord]
    environment_state: EnvironmentState | None = None
    state_estimate: StateEstimate
    evidence_bundle: EvidenceBundle
    scenario_options: list[ScenarioOption]
    decisions: list[OperationalTwinDecision] = Field(default_factory=list)
    lessons_learned: list[LessonLearned] = Field(default_factory=list)
    outcomes: list[OperationalTwinOutcome] = Field(default_factory=list)
    agent_trace: list[AgentStageTrace] = Field(default_factory=list)
    decision_quality: DecisionQualityAssessment = Field(
        default_factory=default_decision_quality_assessment
    )
    utility_estimate: DecisionUtilityEstimate = Field(default_factory=default_decision_utility_estimate)
    reliance_guidance: RelianceGuidance = Field(default_factory=default_reliance_guidance)
    created_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ScenarioOptionDecisionRequest(StrictBaseModel):
    scenario_option_id: str = Field(min_length=1)
    decision: TwinDecisionValue
    actor_id: str = Field(min_length=1)
    comment: str = Field(min_length=1)


class ScenarioOptionDecisionResponse(StrictBaseModel):
    twin_run_id: str
    scenario_option_id: str
    status: ScenarioOptionStatus
    decision: OperationalTwinDecision
    lesson_learned: LessonLearned | None = None
    decided_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OperationalTwinOutcomeRequest(StrictBaseModel):
    selected_option_id: str = Field(min_length=1)
    observed_outcome_summary: str = Field(min_length=1, max_length=4000)
    instructor_rating: int = Field(ge=1, le=5)
    safety_incident: bool = False
    targeted_state_improvement_estimate: float = Field(ge=-1, le=1)
    aar_notes: str = Field(min_length=1, max_length=8000)
    actor_id: str = Field(min_length=1)


class OperationalTwinOutcomeResponse(StrictBaseModel):
    twin_run_id: str
    selected_option_id: str
    status: Literal["outcome_recorded"]
    outcome: OperationalTwinOutcome
    lesson_learned: LessonLearned
    recorded_at_utc: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentRun(StrictBaseModel):
    run_id: str
    status: AgentRunStatus
    request: AgentRunRequest
    steps: list[AgentStep]
    recommendation: RosterRecommendation | None = None
    approval: AgentApproval | None = None
    decision_quality: DecisionQualityAssessment = Field(
        default_factory=default_decision_quality_assessment
    )
    utility_estimate: DecisionUtilityEstimate = Field(default_factory=default_decision_utility_estimate)
    reliance_guidance: RelianceGuidance = Field(default_factory=default_reliance_guidance)
    error: str | None = None
    created_at: datetime
    updated_at: datetime
