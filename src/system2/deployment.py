from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from .audit import AuditLog, AuditSink
from .models import (
    ArtifactInput,
    DecisionContext,
    DeploymentApprovalRequest,
    DeploymentApprovalResponse,
    DeploymentOptionRecommendation,
    DeploymentOutcome,
    DeploymentOutcomeRequest,
    DeploymentOutcomeResponse,
    DeploymentPosture,
    DeploymentRecommendationDecision,
    DeploymentRecommendationRequest,
    DeploymentRecommendationResponse,
    DeploymentRecommendationStatus,
    IndividualDeploymentRecommendation,
    LessonLearned,
    OperationalStateVector,
    OperationalTwinRequest,
    OperationalTwinResponse,
    PlatoonDeploymentRecommendation,
    ScenarioOption,
    SourceReference,
)
from .operational_twin import OperationalTwinService
from .shared_data import (
    InMemorySharedDataSink,
    SharedDataSink,
    build_deployment_approval_update_event,
    build_deployment_outcome_update_event,
    build_deployment_recommendation_update_event,
    canonical_hash,
)


DEPLOYMENT_RECOMMENDATIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS system2_deployment_recommendations (
    deployment_recommendation_id text PRIMARY KEY,
    mission_id text NOT NULL,
    team_id text NOT NULL,
    scope text NOT NULL,
    status text NOT NULL,
    source_twin_run_id text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    payload jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_system2_deployment_recommendations_mission_id
    ON system2_deployment_recommendations (mission_id);

CREATE INDEX IF NOT EXISTS idx_system2_deployment_recommendations_team_id
    ON system2_deployment_recommendations (team_id);

CREATE INDEX IF NOT EXISTS idx_system2_deployment_recommendations_status
    ON system2_deployment_recommendations (status);

CREATE INDEX IF NOT EXISTS idx_system2_deployment_recommendations_source_twin
    ON system2_deployment_recommendations (source_twin_run_id);

CREATE INDEX IF NOT EXISTS idx_system2_deployment_recommendations_created_at
    ON system2_deployment_recommendations (created_at);
"""


ConnectionFactory = Callable[[], Any]


class DeploymentRecommendationRepository(Protocol):
    def save(
        self, recommendation: DeploymentRecommendationResponse
    ) -> DeploymentRecommendationResponse:
        ...

    def get(self, deployment_recommendation_id: str) -> DeploymentRecommendationResponse | None:
        ...

    def list_by_mission(
        self, mission_id: str, *, limit: int = 50
    ) -> list[DeploymentRecommendationResponse]:
        ...


@dataclass
class InMemoryDeploymentRecommendationRepository:
    _recommendations: dict[str, DeploymentRecommendationResponse] = field(default_factory=dict)

    def save(
        self, recommendation: DeploymentRecommendationResponse
    ) -> DeploymentRecommendationResponse:
        self._recommendations[recommendation.deployment_recommendation_id] = recommendation
        return recommendation

    def get(self, deployment_recommendation_id: str) -> DeploymentRecommendationResponse | None:
        return self._recommendations.get(deployment_recommendation_id)

    def list_by_mission(
        self, mission_id: str, *, limit: int = 50
    ) -> list[DeploymentRecommendationResponse]:
        matches = [
            recommendation
            for recommendation in self._recommendations.values()
            if recommendation.mission_id == mission_id
        ]
        matches.sort(key=lambda recommendation: recommendation.created_at_utc, reverse=True)
        return matches[:limit]


class PostgresDeploymentRecommendationRepository:
    def __init__(
        self,
        database_url: str,
        *,
        connection_factory: ConnectionFactory | None = None,
        auto_migrate: bool = True,
    ) -> None:
        self.database_url = database_url
        self._connection_factory = connection_factory
        if auto_migrate:
            self.migrate()

    def migrate(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(DEPLOYMENT_RECOMMENDATIONS_SCHEMA_SQL)
            connection.commit()

    def save(
        self, recommendation: DeploymentRecommendationResponse
    ) -> DeploymentRecommendationResponse:
        payload = dump_deployment_recommendation(recommendation)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO system2_deployment_recommendations (
                        deployment_recommendation_id, mission_id, team_id, scope,
                        status, source_twin_run_id, created_at, updated_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (deployment_recommendation_id) DO UPDATE SET
                        mission_id = EXCLUDED.mission_id,
                        team_id = EXCLUDED.team_id,
                        scope = EXCLUDED.scope,
                        status = EXCLUDED.status,
                        source_twin_run_id = EXCLUDED.source_twin_run_id,
                        updated_at = EXCLUDED.updated_at,
                        payload = EXCLUDED.payload
                    """,
                    (
                        recommendation.deployment_recommendation_id,
                        recommendation.mission_id,
                        recommendation.team_id,
                        recommendation.scope,
                        recommendation.status,
                        recommendation.source_twin_run_id,
                        recommendation.created_at_utc,
                        recommendation.updated_at_utc,
                        json.dumps(payload, sort_keys=True, default=str),
                    ),
                )
            connection.commit()
        return recommendation

    def get(self, deployment_recommendation_id: str) -> DeploymentRecommendationResponse | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payload
                    FROM system2_deployment_recommendations
                    WHERE deployment_recommendation_id = %s
                    """,
                    (deployment_recommendation_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        payload = row["payload"] if isinstance(row, dict) else row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return load_deployment_recommendation(payload)

    def list_by_mission(
        self, mission_id: str, *, limit: int = 50
    ) -> list[DeploymentRecommendationResponse]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payload
                    FROM system2_deployment_recommendations
                    WHERE mission_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (mission_id, limit),
                )
                rows = cursor.fetchall()
        return [
            load_deployment_recommendation(row["payload"] if isinstance(row, dict) else row[0])
            for row in rows
        ]

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()

        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "Postgres deployment recommendation storage requires the 'infra' "
                "optional dependencies. Install with: pip install -e '.[infra]'"
            ) from exc

        return psycopg.connect(self.database_url)


def dump_deployment_recommendation(
    recommendation: DeploymentRecommendationResponse,
) -> dict[str, Any]:
    return recommendation.model_dump(mode="json")


def load_deployment_recommendation(payload: dict[str, Any]) -> DeploymentRecommendationResponse:
    return DeploymentRecommendationResponse.model_validate(payload)


@dataclass
class DeploymentRecommendationService:
    operational_twin_service: OperationalTwinService
    audit_log: AuditSink = field(default_factory=AuditLog)
    shared_data_sink: SharedDataSink = field(default_factory=InMemorySharedDataSink)
    repository: DeploymentRecommendationRepository = field(
        default_factory=InMemoryDeploymentRecommendationRepository
    )

    def recommend(self, request: DeploymentRecommendationRequest) -> DeploymentRecommendationResponse:
        twin_request = _deployment_twin_request(request)
        twin_run = self.operational_twin_service.run(twin_request)
        response = _deployment_response(request, twin_run)
        response = self.repository.save(response)
        self.audit_log.append(
            "deployment_recommendation_created",
            {
                "deployment_recommendation_id": response.deployment_recommendation_id,
                "mission_id": response.mission_id,
                "team_id": response.team_id,
                "scope": response.scope,
                "posture": response.platoon_recommendation.posture,
                "source_twin_run_id": response.source_twin_run_id,
            },
        )
        self.shared_data_sink.append_update_event(
            build_deployment_recommendation_update_event(request, response)
        )
        return response

    def get(
        self, deployment_recommendation_id: str
    ) -> DeploymentRecommendationResponse | None:
        return self.repository.get(deployment_recommendation_id)

    def list_by_mission(
        self, mission_id: str, *, limit: int = 50
    ) -> list[DeploymentRecommendationResponse]:
        return self.repository.list_by_mission(mission_id, limit=limit)

    def record_approval(
        self,
        deployment_recommendation_id: str,
        request: DeploymentApprovalRequest,
    ) -> DeploymentApprovalResponse | None:
        recommendation = self.repository.get(deployment_recommendation_id)
        if recommendation is None:
            return None
        if recommendation.status != "pending_approval":
            raise ValueError("deployment recommendation is not awaiting approval")

        selected_option_id = request.selected_option_id
        if request.decision == "approved":
            selected_option_id = selected_option_id or recommendation.platoon_recommendation.recommended_option_id
            if selected_option_id is None:
                raise ValueError("approved deployment recommendations require a selected option")
        selected_option = _deployment_option(recommendation, selected_option_id)
        if selected_option_id is not None and selected_option is None:
            raise ValueError("deployment option not found")
        if request.decision == "approved" and selected_option is not None:
            if selected_option.critic_status == "reject":
                raise ValueError("critic-rejected deployment options cannot be approved")

        decision = DeploymentRecommendationDecision(
            decision_id=f"deployment-decision-{uuid4()}",
            deployment_recommendation_id=recommendation.deployment_recommendation_id,
            source_twin_run_id=recommendation.source_twin_run_id,
            selected_option_id=selected_option_id,
            actor_id=request.actor_id,
            decision=request.decision,
            approved_posture=(
                request.approved_posture or recommendation.platoon_recommendation.posture
                if request.decision == "approved"
                else request.approved_posture
            ),
            comment=request.comment,
        )
        status = _deployment_status_from_decision(request.decision)
        option_status = _option_status_from_decision(request.decision)
        lesson = _lesson_from_deployment_decision(recommendation, decision)
        lessons = [*recommendation.lessons_learned, lesson] if lesson is not None else recommendation.lessons_learned
        updated = recommendation.model_copy(
            update={
                "status": status,
                "options": _with_selected_option_status(
                    recommendation.options,
                    selected_option_id,
                    option_status,
                ),
                "decisions": [*recommendation.decisions, decision],
                "lessons_learned": lessons,
                "updated_at_utc": datetime.now(UTC),
            }
        )
        self.repository.save(updated)
        response = DeploymentApprovalResponse(
            deployment_recommendation_id=recommendation.deployment_recommendation_id,
            status=status,
            decision=decision,
            lesson_learned=lesson,
            decided_at_utc=decision.timestamp_utc,
        )
        self.audit_log.append(
            "deployment_recommendation_decision_recorded",
            {
                "deployment_recommendation_id": recommendation.deployment_recommendation_id,
                "mission_id": recommendation.mission_id,
                "team_id": recommendation.team_id,
                "decision": request.decision,
                "selected_option_id": selected_option_id,
                "actor_id": request.actor_id,
                "lesson_id": lesson.lesson_id if lesson is not None else None,
            },
        )
        self.shared_data_sink.append_update_event(
            build_deployment_approval_update_event(recommendation, request, response)
        )
        return response

    def record_outcome(
        self,
        deployment_recommendation_id: str,
        request: DeploymentOutcomeRequest,
    ) -> DeploymentOutcomeResponse | None:
        recommendation = self.repository.get(deployment_recommendation_id)
        if recommendation is None:
            return None
        if recommendation.status not in {"approved", "completed", "outcome_recorded"}:
            raise ValueError("outcomes can only be captured for approved deployment recommendations")

        selected_option_id = (
            request.selected_option_id
            or _approved_selected_option_id(recommendation)
            or recommendation.platoon_recommendation.recommended_option_id
        )
        if selected_option_id is not None and _deployment_option(recommendation, selected_option_id) is None:
            raise ValueError("deployment option not found")

        outcome = DeploymentOutcome(
            outcome_id=f"deployment-outcome-{uuid4()}",
            deployment_recommendation_id=recommendation.deployment_recommendation_id,
            source_twin_run_id=recommendation.source_twin_run_id,
            selected_option_id=selected_option_id,
            observed_outcome_summary=request.observed_outcome_summary,
            commander_rating=request.commander_rating,
            safety_incident=request.safety_incident,
            near_miss=request.near_miss,
            mission_effectiveness_estimate=request.mission_effectiveness_estimate,
            recommendation_accepted=request.recommendation_accepted,
            recommendation_helpful=request.recommendation_helpful,
            overridden_posture=request.overridden_posture,
            missed_factor=request.missed_factor,
            should_have_escalated=request.should_have_escalated,
            aar_notes=request.aar_notes,
            actor_id=request.actor_id,
            controls=recommendation.platoon_recommendation.required_controls,
        )
        lesson = _lesson_from_deployment_outcome(recommendation, outcome)
        updated = recommendation.model_copy(
            update={
                "status": "outcome_recorded",
                "outcomes": [*recommendation.outcomes, outcome],
                "lessons_learned": [*recommendation.lessons_learned, lesson],
                "updated_at_utc": datetime.now(UTC),
            }
        )
        self.repository.save(updated)
        response = DeploymentOutcomeResponse(
            deployment_recommendation_id=recommendation.deployment_recommendation_id,
            status="outcome_recorded",
            outcome=outcome,
            lesson_learned=lesson,
            recorded_at_utc=outcome.recorded_at_utc,
        )
        self.audit_log.append(
            "deployment_recommendation_outcome_recorded",
            {
                "deployment_recommendation_id": recommendation.deployment_recommendation_id,
                "mission_id": recommendation.mission_id,
                "team_id": recommendation.team_id,
                "selected_option_id": selected_option_id,
                "outcome_id": outcome.outcome_id,
                "lesson_id": lesson.lesson_id,
                "actor_id": request.actor_id,
                "commander_rating": request.commander_rating,
                "safety_incident": request.safety_incident,
                "near_miss": request.near_miss,
            },
        )
        self.shared_data_sink.append_update_event(
            build_deployment_outcome_update_event(recommendation, request, response)
        )
        return response


def _deployment_twin_request(request: DeploymentRecommendationRequest) -> OperationalTwinRequest:
    artifacts = [
        *_processed_observation_artifacts(request.processed_observations),
        ArtifactInput(
            kind="mission_context",
            content=request.mission_context,
            source_system="system2_deployment_request",
            metadata={"constraints": request.constraints, "scope": request.scope},
        ),
    ]
    if request.terrain:
        artifacts.append(
            ArtifactInput(
                kind="terrain",
                content=request.terrain,
                source_system="system2_deployment_request",
            )
        )
    if request.weather:
        artifacts.append(
            ArtifactInput(
                kind="weather",
                content=_summary_text(request.weather),
                source_system="system2_deployment_request",
                metadata=request.weather,
            )
        )
    if request.readiness:
        artifacts.append(
            ArtifactInput(
                kind="sleep_food_log",
                content=_summary_text(request.readiness),
                source_system="system2_deployment_request",
                metadata=request.readiness,
            )
        )

    return OperationalTwinRequest(
        mission_id=request.mission_id,
        operator_id=request.requester_id,
        mode="mission",
        team_id=request.team_id,
        training_objective=(
            "Recommend individual or platoon deployment posture from processed "
            "System 1 evidence, mission context, terrain, weather, and readiness."
        ),
        artifacts=artifacts,
        environment=_environment_from_request(request),
        require_human_approval=request.require_human_approval,
        decision_context=request.decision_context or _deployment_decision_context(request),
    )


def _deployment_response(
    request: DeploymentRecommendationRequest,
    twin_run: OperationalTwinResponse,
) -> DeploymentRecommendationResponse:
    readiness = _readiness_score(twin_run.state_estimate.state_vector)
    option = _recommended_option(twin_run.scenario_options)
    posture = _deployment_posture(twin_run, readiness, option)
    risk = _risk_level(max((item.risk_score for item in twin_run.scenario_options), default=0.0))
    controls = _required_controls(twin_run, posture)
    evidence_refs = [ref.ref for ref in _source_refs(request, twin_run)]
    platoon = PlatoonDeploymentRecommendation(
        team_id=request.team_id,
        posture=posture,
        readiness_score=readiness,
        risk_level=risk,
        recommended_option_id=option.scenario_option_id if option is not None else None,
        rationale=_platoon_rationale(twin_run, posture, readiness),
        required_controls=controls,
        evidence_refs=evidence_refs,
    )
    individuals = [
        IndividualDeploymentRecommendation(
            soldier_id=soldier_id,
            posture=posture,
            readiness_score=readiness,
            risk_level=risk,
            recommended_role=None,
            rationale=(
                "Individual posture inherits the team recommendation because "
                "this request did not include a separate soldier-level roster score."
            ),
            required_controls=controls,
            evidence_refs=evidence_refs,
        )
        for soldier_id in request.target_soldier_ids
    ]
    return DeploymentRecommendationResponse(
        deployment_recommendation_id=f"deploy-{uuid4()}",
        mission_id=request.mission_id,
        team_id=request.team_id,
        scope=request.scope,
        status="pending_approval" if request.require_human_approval else "completed",
        source_twin_run_id=twin_run.twin_run_id,
        platoon_recommendation=platoon,
        individual_recommendations=individuals,
        options=[_option_recommendation(item) for item in twin_run.scenario_options],
        agent_trace=twin_run.agent_trace,
        source_refs=_source_refs(request, twin_run),
        decision_quality=twin_run.decision_quality,
        utility_estimate=twin_run.utility_estimate,
        reliance_guidance=twin_run.reliance_guidance,
    )


def _processed_observation_artifacts(artifacts: list[ArtifactInput]) -> list[ArtifactInput]:
    if artifacts:
        return artifacts
    return [
        ArtifactInput(
            kind="system1_observation",
            content="No processed System 1 observations were supplied; use mission context only.",
            source_system="system2_deployment_request",
            metadata={"missing_processed_observations": True},
        )
    ]


def _deployment_decision_context(request: DeploymentRecommendationRequest) -> DecisionContext:
    return DecisionContext(
        decision_point=f"{request.scope} deployment recommendation",
        actor_role="commander or authorized deployment reviewer",
        objective="Recommend deployment posture while preserving human command judgment.",
        constraints=[
            "processed System 1 evidence only",
            "mission terrain and weather context required",
            "human approval before final deployment action",
            *request.constraints,
        ],
        time_pressure="high",
        reversibility="partially_reversible",
        stakeholder_impact="high",
        fallback_action="Hold deployment change and route to commander review.",
    )


def _environment_from_request(request: DeploymentRecommendationRequest) -> dict[str, Any]:
    environment = dict(request.weather)
    if request.terrain and "terrain" not in environment:
        environment["terrain"] = request.terrain
    return environment


def _recommended_option(options: list[ScenarioOption]) -> ScenarioOption | None:
    candidates = [item for item in options if item.critic_status != "reject"]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item.utility_estimate.net_utility_score,
            item.confidence,
            -item.risk_score,
        ),
    )


def _deployment_posture(
    twin_run: OperationalTwinResponse,
    readiness: float,
    option: ScenarioOption | None,
) -> DeploymentPosture:
    if twin_run.decision_quality.readiness == "escalate" or option is None:
        return "escalate_review"
    if readiness < 0.45 or twin_run.state_estimate.state_vector.fatigue_burden > 0.78:
        return "hold"
    if option.risk_score > 0.55 or option.critic_status in {"modify", "escalate"}:
        return "deploy_with_controls"
    return "deploy"


def _readiness_score(vector: OperationalStateVector) -> float:
    values = [
        1.0 - vector.fatigue_burden,
        vector.situational_clarity,
        vector.cohesion,
        vector.leader_decision_quality,
        1.0 - vector.mission_tempo_risk,
    ]
    return round(_clamp(sum(values) / len(values)), 3)


def _required_controls(
    twin_run: OperationalTwinResponse,
    posture: DeploymentPosture,
) -> list[str]:
    controls = [
        "Named human approval required before deployment action.",
        "Review source references, decision quality, and critic reasons.",
    ]
    vector = twin_run.state_estimate.state_vector
    if vector.fatigue_burden > 0.55:
        controls.append("Mitigate fatigue before execution or reduce task duration.")
    if vector.mission_tempo_risk > 0.55:
        controls.append("Confirm timing, comms, and support synchronization before movement.")
    if posture in {"deploy_with_controls", "escalate_review", "hold"}:
        controls.append("Document commander rationale if overriding the recommended posture.")
    if any(item.critic_status == "modify" for item in twin_run.scenario_options):
        controls.append("Apply critic modifications before treating any COA as executable.")
    return controls


def _option_recommendation(option: ScenarioOption) -> DeploymentOptionRecommendation:
    return DeploymentOptionRecommendation(
        scenario_option_id=option.scenario_option_id,
        title=option.title,
        option_type=option.option_type,
        recommendation=option.narrative,
        risk_score=option.risk_score,
        confidence=option.confidence,
        critic_status=option.critic_status,
        critic_reasons=option.critic_reasons,
        status=option.status,
        decision_quality=option.decision_quality,
        utility_estimate=option.utility_estimate,
        reliance_guidance=option.reliance_guidance,
    )


def _deployment_status_from_decision(
    decision: str,
) -> DeploymentRecommendationStatus:
    if decision == "approved":
        return "approved"
    if decision == "escalated":
        return "escalated"
    return "rejected"


def _option_status_from_decision(decision: str) -> str:
    if decision == "approved":
        return "approved"
    if decision == "escalated":
        return "escalated"
    return "rejected"


def _deployment_option(
    recommendation: DeploymentRecommendationResponse,
    selected_option_id: str | None,
) -> DeploymentOptionRecommendation | None:
    if selected_option_id is None:
        return None
    return next(
        (
            item
            for item in recommendation.options
            if item.scenario_option_id == selected_option_id
        ),
        None,
    )


def _with_selected_option_status(
    options: list[DeploymentOptionRecommendation],
    selected_option_id: str | None,
    status: str,
) -> list[DeploymentOptionRecommendation]:
    if selected_option_id is None:
        return options
    return [
        item.model_copy(update={"status": status})
        if item.scenario_option_id == selected_option_id
        else item
        for item in options
    ]


def _approved_selected_option_id(
    recommendation: DeploymentRecommendationResponse,
) -> str | None:
    for decision in reversed(recommendation.decisions):
        if decision.decision == "approved":
            return decision.selected_option_id
    return None


def _lesson_from_deployment_decision(
    recommendation: DeploymentRecommendationResponse,
    decision: DeploymentRecommendationDecision,
) -> LessonLearned | None:
    if decision.decision == "rejected":
        return None
    severity = "high" if decision.decision == "escalated" else recommendation.platoon_recommendation.risk_level
    return LessonLearned(
        lesson_id=f"lesson-{uuid4()}",
        mission_id=recommendation.mission_id,
        category="deployment_recommendation_decision",
        summary=(
            f"Deployment recommendation {decision.decision} for "
            f"{recommendation.team_id}: {decision.comment}"
        ),
        root_cause=(
            "Decision was based on processed System 1 evidence, operational twin "
            "state, critic findings, and named human review."
        ),
        recommended_training_delta=(
            "Review whether evidence, critic reasons, and controls matched the "
            "human decision rationale."
        ),
        recommended_mission_delta=(
            "Preserve recommendation, decision, and outcome linkage for AAR "
            "calibration."
        ),
        severity=severity,
        evidence_bundle_id=recommendation.source_twin_run_id,
    )


def _lesson_from_deployment_outcome(
    recommendation: DeploymentRecommendationResponse,
    outcome: DeploymentOutcome,
) -> LessonLearned:
    if outcome.safety_incident or outcome.near_miss or outcome.should_have_escalated:
        severity = "high"
    elif outcome.commander_rating <= 3 or not outcome.recommendation_helpful:
        severity = "medium"
    else:
        severity = "low"
    missed_factor = outcome.missed_factor or "No missed factor recorded."
    return LessonLearned(
        lesson_id=f"lesson-{uuid4()}",
        mission_id=recommendation.mission_id,
        category="deployment_recommendation_outcome",
        summary=(
            f"Outcome for deployment recommendation "
            f"{recommendation.deployment_recommendation_id}: "
            f"{outcome.observed_outcome_summary}"
        ),
        root_cause=missed_factor,
        recommended_training_delta=(
            "Use the outcome record to update golden scenarios, critic checks, "
            "and readiness evidence requirements."
        ),
        recommended_mission_delta=(
            "Calibrate future deployment controls against observed commander "
            "rating, near-miss, safety, and usefulness signals."
        ),
        severity=severity,
        evidence_bundle_id=recommendation.source_twin_run_id,
    )


def _source_refs(
    request: DeploymentRecommendationRequest,
    twin_run: OperationalTwinResponse,
) -> list[SourceReference]:
    refs = [
        SourceReference(
            ref=f"postgres://missions_current/{request.mission_id}",
            role="mission",
            source_hash=canonical_hash(
                {
                    "mission_id": request.mission_id,
                    "mission_context": request.mission_context,
                }
            ),
        ),
        SourceReference(
            ref=f"operational-twin://runs/{twin_run.twin_run_id}",
            role="source_operational_twin_run",
            source_hash=canonical_hash(twin_run.model_dump(mode="json")),
        ),
    ]
    refs.extend(
        SourceReference(
            ref=f"evidence://deployment/{artifact.kind}/{artifact.artifact_id or index}",
            role=f"deployment_evidence:{artifact.kind}",
            source_hash=canonical_hash(artifact.model_dump(mode="json")),
            metadata={"source_system": artifact.source_system},
        )
        for index, artifact in enumerate(request.processed_observations)
    )
    return refs


def _platoon_rationale(
    twin_run: OperationalTwinResponse,
    posture: DeploymentPosture,
    readiness: float,
) -> str:
    return (
        f"Recommended posture is {posture.replace('_', ' ')} with readiness {readiness:.2f}. "
        f"Operational twin readiness is {twin_run.decision_quality.readiness}; "
        f"state uncertainty is {twin_run.state_estimate.uncertainty.overall:.2f}."
    )


def _risk_level(value: float) -> str:
    if value >= 0.67:
        return "high"
    if value >= 0.34:
        return "medium"
    return "low"


def _summary_text(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{key}: {item}" for key, item in sorted(value.items()))
    return str(value)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
