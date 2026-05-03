from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Literal

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
    candidate_count: int = Field(default=80, ge=14, le=5000)
    candidates: list[Soldier] | None = None
    seed: int = 42
    roles: list[RoleRequirement] | None = None


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


class TraceMetadata(StrictBaseModel):
    model_versions: dict[str, str]
    feature_hash: str
    prompt_hash: str
    seed: int
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    dod_ai_principles: dict[str, str]
    calibration_bins: list[dict[str, float | int]] = Field(default_factory=list)
    disagreement_histogram: list[dict[str, float | int]] = Field(default_factory=list)


class RosterRecommendation(StrictBaseModel):
    mission_id: str
    roster: list[CandidateAssessment]
    second_choice_roster: list[CandidateAssessment]
    fairness_audit: FairnessAudit
    career_forecast: CareerForecast
    trace: TraceMetadata


class AgentRunRequest(StrictBaseModel):
    objective: Literal["mission_roster_recommendation"] = "mission_roster_recommendation"
    score_request: ScoreRequest
    require_human_approval: bool = True


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


class AgentRun(StrictBaseModel):
    run_id: str
    status: AgentRunStatus
    request: AgentRunRequest
    steps: list[AgentStep]
    recommendation: RosterRecommendation | None = None
    approval: AgentApproval | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
