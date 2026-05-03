from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from .audit import AuditLog, AuditSink
from .decision_quality import assess_operational_twin_run, assess_scenario_option
from .llm import JsonAgentClient
from .models import (
    AgentStageTrace,
    ArtifactInput,
    ArtifactRecord,
    ControlProperties,
    EnvironmentState,
    EvidenceBundle,
    EvidenceBundleArtifact,
    EvidenceBundleHashChain,
    EvidenceBundleModelTrace,
    EvidenceBundleObservation,
    EvidenceBundlePolicyChecks,
    LessonLearned,
    ObservationInput,
    ObservationRecord,
    OperationalStateVector,
    OperationalTwinDecision,
    OperationalTwinOutcome,
    OperationalTwinOutcomeRequest,
    OperationalTwinOutcomeResponse,
    OperationalTwinRequest,
    OperationalTwinResponse,
    ScenarioOption,
    ScenarioOptionDecisionRequest,
    ScenarioOptionDecisionResponse,
    ScenarioPredictedEffect,
    StateEstimate,
    StateUncertainty,
    TwinObservationKind,
    TwinSubjectRef,
)
from .registry import MODEL_VERSIONS
from .shared_data import (
    InMemorySharedDataSink,
    SharedDataSink,
    build_operational_twin_decision_update_event,
    build_operational_twin_outcome_update_event,
    build_operational_twin_update_event,
    canonical_hash,
)


_OBSERVATION_KINDS = {
    "voice_fact",
    "ocr_fact",
    "telemetry_fact",
    "weather_fact",
    "sleep_food_fact",
    "image_fact",
    "manual_note",
}
_CRITIC_STATUSES = {"pass", "modify", "escalate", "reject"}
_STALE_SOURCE_THRESHOLD = timedelta(hours=12)
_PROHIBITED_OBSERVATION_TERMS = (
    "motive",
    "intentional sabotage",
    "because he wanted",
    "because she wanted",
    "lazy",
    "low iq",
    "innate",
    "natural talent",
    "fixed talent",
    "diagnosed",
    "diagnosis",
    "ptsd",
    "depression",
    "race",
    "gender",
    "religion",
    "ethnicity",
)
_PROMPT_INJECTION_TERMS = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "system prompt",
    "developer message",
    "approve unsafe",
    "bypass human",
    "override policy",
    "do not follow",
)


OPERATIONAL_TWIN_RUNS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS system2_operational_twin_runs (
    twin_run_id text PRIMARY KEY,
    mission_id text NOT NULL,
    team_id text NOT NULL,
    status text NOT NULL,
    mode text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    action_hash text,
    payload jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_system2_operational_twin_runs_twin_run_id
    ON system2_operational_twin_runs (twin_run_id);

CREATE INDEX IF NOT EXISTS idx_system2_operational_twin_runs_mission_id
    ON system2_operational_twin_runs (mission_id);

CREATE INDEX IF NOT EXISTS idx_system2_operational_twin_runs_team_id
    ON system2_operational_twin_runs (team_id);

CREATE INDEX IF NOT EXISTS idx_system2_operational_twin_runs_status
    ON system2_operational_twin_runs (status);

CREATE INDEX IF NOT EXISTS idx_system2_operational_twin_runs_mode
    ON system2_operational_twin_runs (mode);

CREATE INDEX IF NOT EXISTS idx_system2_operational_twin_runs_created_at
    ON system2_operational_twin_runs (created_at);

CREATE INDEX IF NOT EXISTS idx_system2_operational_twin_runs_updated_at
    ON system2_operational_twin_runs (updated_at);
"""


ConnectionFactory = Callable[[], Any]


class OperationalTwinRepository(Protocol):
    def save(self, run: OperationalTwinResponse) -> OperationalTwinResponse:
        ...

    def get(self, twin_run_id: str) -> OperationalTwinResponse | None:
        ...

    def last_action_hash(self) -> str:
        ...

    def record_action_hash(self, action_hash: str) -> None:
        ...


@dataclass
class InMemoryOperationalTwinRepository:
    _runs: dict[str, OperationalTwinResponse] = field(default_factory=dict)
    _last_action_hash: str = "0" * 64

    def save(self, run: OperationalTwinResponse) -> OperationalTwinResponse:
        self._runs[run.twin_run_id] = run
        return run

    def get(self, twin_run_id: str) -> OperationalTwinResponse | None:
        return self._runs.get(twin_run_id)

    def last_action_hash(self) -> str:
        return self._last_action_hash

    def record_action_hash(self, action_hash: str) -> None:
        self._last_action_hash = action_hash.removeprefix("sha256:")


class PostgresOperationalTwinRepository:
    def __init__(
        self,
        database_url: str,
        *,
        connection_factory: ConnectionFactory | None = None,
        auto_migrate: bool = True,
    ) -> None:
        self.database_url = database_url
        self._connection_factory = connection_factory
        self._last_action_hash: str | None = None
        if auto_migrate:
            self.migrate()

    def migrate(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(OPERATIONAL_TWIN_RUNS_SCHEMA_SQL)
            connection.commit()

    def save(self, run: OperationalTwinResponse) -> OperationalTwinResponse:
        payload = dump_operational_twin_run(run)
        action_hash = run.evidence_bundle.hash_chain.current_action_hash
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO system2_operational_twin_runs (
                        twin_run_id, mission_id, team_id, status, mode,
                        created_at, updated_at, action_hash, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (twin_run_id) DO UPDATE SET
                        mission_id = EXCLUDED.mission_id,
                        team_id = EXCLUDED.team_id,
                        status = EXCLUDED.status,
                        mode = EXCLUDED.mode,
                        updated_at = EXCLUDED.updated_at,
                        action_hash = EXCLUDED.action_hash,
                        payload = EXCLUDED.payload
                    """,
                    (
                        run.twin_run_id,
                        run.mission_id,
                        run.team_id,
                        run.status,
                        run.mode,
                        run.created_at_utc,
                        run.updated_at_utc,
                        action_hash,
                        json.dumps(payload, sort_keys=True, default=str),
                    ),
                )
            connection.commit()
        self._last_action_hash = action_hash
        return run

    def get(self, twin_run_id: str) -> OperationalTwinResponse | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM system2_operational_twin_runs WHERE twin_run_id = %s",
                    (twin_run_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        payload = row["payload"] if isinstance(row, dict) else row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return load_operational_twin_run(payload)

    def last_action_hash(self) -> str:
        if self._last_action_hash is not None:
            return self._last_action_hash.removeprefix("sha256:")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT action_hash, payload
                    FROM system2_operational_twin_runs
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
        if row is None:
            return "0" * 64
        action_hash = row["action_hash"] if isinstance(row, dict) else row[0]
        payload = row["payload"] if isinstance(row, dict) else row[1]
        if action_hash:
            self._last_action_hash = str(action_hash).removeprefix("sha256:")
            return self._last_action_hash
        if isinstance(payload, str):
            payload = json.loads(payload)
        loaded = load_operational_twin_run(payload)
        self._last_action_hash = loaded.evidence_bundle.hash_chain.current_action_hash
        return self._last_action_hash

    def record_action_hash(self, action_hash: str) -> None:
        self._last_action_hash = action_hash.removeprefix("sha256:")

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()

        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "Postgres operational twin storage requires the 'infra' optional dependencies. "
                "Install with: pip install -e '.[infra]'"
            ) from exc

        return psycopg.connect(self.database_url)


def dump_operational_twin_run(run: OperationalTwinResponse) -> dict[str, Any]:
    return run.model_dump(mode="json")


def load_operational_twin_run(payload: dict[str, Any]) -> OperationalTwinResponse:
    return OperationalTwinResponse.model_validate(payload)


@dataclass(frozen=True)
class PerceptionAgent:
    llm_client: JsonAgentClient | None = None
    agent_provider: str = "deterministic"
    max_retries: int = 1

    def run(
        self,
        *,
        request: OperationalTwinRequest,
        artifacts: Sequence[ArtifactRecord],
        baseline_observations: Sequence[ObservationRecord],
    ) -> tuple[list[ObservationRecord], list[AgentStageTrace]]:
        observations, trace = _agentic_observations(
            request=request,
            artifacts=artifacts,
            baseline_observations=baseline_observations,
            llm_client=self.llm_client,
            agent_provider=self.agent_provider,
            max_retries=self.max_retries,
        )
        validated = _validate_observation_pipeline(request, artifacts, observations)
        if validated != list(observations):
            trace.append(
                _trace(
                    stage="perception",
                    provider="deterministic",
                    model=MODEL_VERSIONS["operational_twin_perception"],
                    status="completed",
                    summary=(
                        "Validated source-artifact links, normalized confidence, "
                        "propagated controls, and redacted prohibited observation assertions."
                    ),
                    input_payload=[item.model_dump(mode="json") for item in observations],
                    output_payload=[item.model_dump(mode="json") for item in validated],
                )
            )
        return validated, trace


@dataclass(frozen=True)
class StateEstimatorAgent:
    llm_client: JsonAgentClient | None = None
    agent_provider: str = "deterministic"
    max_retries: int = 1

    def run(
        self,
        *,
        request: OperationalTwinRequest,
        observations: Sequence[ObservationRecord],
        environment_state: EnvironmentState | None,
        baseline_state_vector: OperationalStateVector,
        baseline_uncertainty: StateUncertainty,
    ) -> tuple[OperationalStateVector, StateUncertainty, list[AgentStageTrace]]:
        return _agentic_state_estimate(
            request=request,
            observations=observations,
            environment_state=environment_state,
            baseline_state_vector=baseline_state_vector,
            baseline_uncertainty=baseline_uncertainty,
            llm_client=self.llm_client,
            agent_provider=self.agent_provider,
            max_retries=self.max_retries,
        )


@dataclass(frozen=True)
class ScenarioDirectorAgent:
    llm_client: JsonAgentClient | None = None
    agent_provider: str = "deterministic"
    max_retries: int = 1

    def run(
        self,
        *,
        request: OperationalTwinRequest,
        observations: Sequence[ObservationRecord],
        state_estimate: StateEstimate,
        evidence_bundle: EvidenceBundle,
        baseline_options: Sequence[ScenarioOption],
    ) -> tuple[list[ScenarioOption], list[AgentStageTrace]]:
        options, trace = _agentic_scenario_options(
            request=request,
            observations=observations,
            state_estimate=state_estimate,
            evidence_bundle=evidence_bundle,
            baseline_options=baseline_options,
            llm_client=self.llm_client,
            agent_provider=self.agent_provider,
            max_retries=self.max_retries,
        )
        governed = _apply_option_governance_checks(
            request=request,
            evidence_bundle=evidence_bundle,
            state_vector=state_estimate.state_vector,
            options=options,
        )
        return governed, trace


@dataclass(frozen=True)
class CriticAgent:
    llm_client: JsonAgentClient | None = None
    agent_provider: str = "deterministic"
    max_retries: int = 1

    def run(
        self,
        *,
        request: OperationalTwinRequest,
        observations: Sequence[ObservationRecord],
        state_estimate: StateEstimate,
        evidence_bundle: EvidenceBundle,
        options: Sequence[ScenarioOption],
    ) -> tuple[list[ScenarioOption], list[AgentStageTrace]]:
        reviewed, trace = _agentic_critic_review(
            request=request,
            observations=observations,
            state_estimate=state_estimate,
            evidence_bundle=evidence_bundle,
            options=options,
            llm_client=self.llm_client,
            agent_provider=self.agent_provider,
            max_retries=self.max_retries,
        )
        governed = _apply_option_governance_checks(
            request=request,
            evidence_bundle=evidence_bundle,
            state_vector=state_estimate.state_vector,
            options=reviewed,
        )
        return governed, trace


@dataclass(frozen=True)
class LessonAgent:
    def from_decision(
        self,
        run: OperationalTwinResponse,
        option: ScenarioOption,
        decision: OperationalTwinDecision,
    ) -> LessonLearned | None:
        return _lesson_from_decision(run, option, decision)

    def from_outcome(
        self,
        run: OperationalTwinResponse,
        option: ScenarioOption,
        outcome: OperationalTwinOutcome,
    ) -> LessonLearned:
        severity = "high" if outcome.safety_incident else "medium" if outcome.instructor_rating <= 3 else "low"
        return LessonLearned(
            lesson_id=f"lesson-{uuid4()}",
            mission_id=run.mission_id,
            category="operational_twin_outcome",
            summary=f"Outcome for {option.title}: {outcome.observed_outcome_summary}",
            root_cause=option.predicted_effect.target_state_change,
            recommended_training_delta=(
                "Calibrate future training options against the observed outcome and AAR notes."
            ),
            recommended_mission_delta=(
                "Retain the outcome as evidence for future rehearsal and COA calibration."
            ),
            severity=severity,
            status="draft",
            evidence_bundle_id=outcome.evidence_bundle_id,
            controls=outcome.controls,
        )


@dataclass
class OperationalTwinService:
    audit_log: AuditSink = field(default_factory=AuditLog)
    shared_data_sink: SharedDataSink = field(default_factory=InMemorySharedDataSink)
    repository: OperationalTwinRepository = field(default_factory=InMemoryOperationalTwinRepository)
    agent_provider: str = "deterministic"
    llm_client: JsonAgentClient | None = None
    llm_model: str = "deterministic"
    agent_max_retries: int = 1
    lesson_agent: LessonAgent = field(default_factory=LessonAgent)

    def run(self, request: OperationalTwinRequest) -> OperationalTwinResponse:
        artifact_inputs = _artifact_inputs(request)
        artifacts = [_artifact_record(item) for item in artifact_inputs]
        baseline_observations = _observation_records(request, artifacts)
        observations, agent_trace = PerceptionAgent(
            llm_client=self.llm_client,
            agent_provider=self.agent_provider,
            max_retries=self.agent_max_retries,
        ).run(
            request=request,
            artifacts=artifacts,
            baseline_observations=baseline_observations,
        )
        if not observations:
            raise ValueError("operational twin runs require at least one artifact or observation")

        twin_run_id = f"twin-{uuid4()}"
        evidence_bundle_id = f"eb-{uuid4()}"
        environment_state = _environment_state(request)
        baseline_state_vector = _estimate_state_vector(observations, environment_state)
        baseline_uncertainty = _estimate_uncertainty(observations, baseline_state_vector)
        state_vector, uncertainty, state_trace = StateEstimatorAgent(
            llm_client=self.llm_client,
            agent_provider=self.agent_provider,
            max_retries=self.agent_max_retries,
        ).run(
            request=request,
            observations=observations,
            environment_state=environment_state,
            baseline_state_vector=baseline_state_vector,
            baseline_uncertainty=baseline_uncertainty,
        )
        agent_trace.extend(state_trace)
        state_estimate = StateEstimate(
            state_estimate_id=f"se-{uuid4()}",
            subject_type="team",
            subject_id=request.team_id,
            state_vector=state_vector,
            uncertainty=uncertainty,
            evidence_bundle_id=evidence_bundle_id,
            model_version=MODEL_VERSIONS["operational_twin_state_estimator"],
            controls=request.controls,
        )
        policy_checks = _policy_checks(request, observations, state_vector)
        evidence_bundle = _evidence_bundle(
            request=request,
            evidence_bundle_id=evidence_bundle_id,
            artifacts=artifacts,
            observations=observations,
            state_estimate=state_estimate,
            policy_checks=policy_checks,
            previous_action_hash=self.repository.last_action_hash(),
        )
        baseline_options = _scenario_options(
            request=request,
            observations=observations,
            state_estimate=state_estimate,
            evidence_bundle=evidence_bundle,
        )
        scenario_options, scenario_trace = ScenarioDirectorAgent(
            llm_client=self.llm_client,
            agent_provider=self.agent_provider,
            max_retries=self.agent_max_retries,
        ).run(
            request=request,
            observations=observations,
            state_estimate=state_estimate,
            evidence_bundle=evidence_bundle,
            baseline_options=baseline_options,
        )
        agent_trace.extend(scenario_trace)
        scenario_options, critic_trace = CriticAgent(
            llm_client=self.llm_client,
            agent_provider=self.agent_provider,
            max_retries=self.agent_max_retries,
        ).run(
            request=request,
            observations=observations,
            state_estimate=state_estimate,
            evidence_bundle=evidence_bundle,
            options=scenario_options,
        )
        agent_trace.extend(critic_trace)
        scenario_options = [
            _with_option_decision_quality(request, evidence_bundle, state_estimate, item)
            for item in scenario_options
        ]
        decision_quality, utility_estimate, reliance_guidance = assess_operational_twin_run(
            request,
            evidence_bundle,
            state_estimate,
            scenario_options,
        )
        now = datetime.now(UTC)
        response = OperationalTwinResponse(
            twin_run_id=twin_run_id,
            mission_id=request.mission_id,
            mode=request.mode,
            team_id=request.team_id,
            artifacts=artifacts,
            observations=observations,
            environment_state=environment_state,
            state_estimate=state_estimate,
            evidence_bundle=evidence_bundle,
            scenario_options=scenario_options,
            agent_trace=agent_trace,
            decision_quality=decision_quality,
            utility_estimate=utility_estimate,
            reliance_guidance=reliance_guidance,
            created_at_utc=now,
            updated_at_utc=now,
        )
        response = self.repository.save(response)
        self.repository.record_action_hash(evidence_bundle.hash_chain.current_action_hash)
        self.audit_log.append(
            "operational_twin_run_created",
            {
                "twin_run_id": response.twin_run_id,
                "mission_id": request.mission_id,
                "team_id": request.team_id,
                "mode": request.mode,
                "artifact_count": len(artifacts),
                "observation_count": len(observations),
                "scenario_option_count": len(scenario_options),
                "evidence_bundle_id": evidence_bundle.bundle_id,
                "agent_provider": self.agent_provider,
            },
        )
        self.shared_data_sink.append_update_event(
            build_operational_twin_update_event(request, response)
        )
        return response

    def get(self, twin_run_id: str) -> OperationalTwinResponse | None:
        return self.repository.get(twin_run_id)

    def record_decision(
        self,
        twin_run_id: str,
        request: ScenarioOptionDecisionRequest,
    ) -> ScenarioOptionDecisionResponse | None:
        run = self.repository.get(twin_run_id)
        if run is None:
            return None

        option = next(
            (
                item
                for item in run.scenario_options
                if item.scenario_option_id == request.scenario_option_id
            ),
            None,
        )
        if option is None:
            raise ValueError("scenario option not found")
        if option.status != "draft":
            raise ValueError("scenario option has already been decided")
        if request.decision == "approved" and option.critic_status == "reject":
            raise ValueError("critic-rejected options cannot be approved")

        status = _status_from_decision(request.decision)
        decision = OperationalTwinDecision(
            decision_id=f"decision-{uuid4()}",
            target_object_id=option.scenario_option_id,
            actor_id=request.actor_id,
            decision=request.decision,
            comment=request.comment,
            evidence_bundle_id=option.evidence_bundle_id,
        )
        lesson = self.lesson_agent.from_decision(run, option, decision)
        updated_options = [
            item.model_copy(update={"status": status})
            if item.scenario_option_id == option.scenario_option_id
            else item
            for item in run.scenario_options
        ]
        lessons = [*run.lessons_learned, lesson] if lesson is not None else run.lessons_learned
        new_run_status = "completed" if request.decision == "approved" else "partially_decided"
        updated_run = run.model_copy(
            update={
                "status": new_run_status,
                "scenario_options": updated_options,
                "decisions": [*run.decisions, decision],
                "lessons_learned": lessons,
                "updated_at_utc": datetime.now(UTC),
            }
        )
        self.repository.save(updated_run)
        response = ScenarioOptionDecisionResponse(
            twin_run_id=twin_run_id,
            scenario_option_id=option.scenario_option_id,
            status=status,
            decision=decision,
            lesson_learned=lesson,
        )
        self.audit_log.append(
            "operational_twin_option_decision_recorded",
            {
                "twin_run_id": twin_run_id,
                "mission_id": run.mission_id,
                "scenario_option_id": option.scenario_option_id,
                "decision": request.decision,
                "actor_id": request.actor_id,
                "lesson_id": lesson.lesson_id if lesson is not None else None,
            },
        )
        self.shared_data_sink.append_update_event(
            build_operational_twin_decision_update_event(updated_run, request, response)
        )
        return response

    def record_outcome(
        self,
        twin_run_id: str,
        request: OperationalTwinOutcomeRequest,
    ) -> OperationalTwinOutcomeResponse | None:
        run = self.repository.get(twin_run_id)
        if run is None:
            return None

        option = next(
            (
                item
                for item in run.scenario_options
                if item.scenario_option_id == request.selected_option_id
            ),
            None,
        )
        if option is None:
            raise ValueError("scenario option not found")
        if option.status != "approved":
            raise ValueError("outcomes can only be captured for approved options")

        outcome = OperationalTwinOutcome(
            outcome_id=f"outcome-{uuid4()}",
            selected_option_id=option.scenario_option_id,
            observed_outcome_summary=request.observed_outcome_summary,
            instructor_rating=request.instructor_rating,
            safety_incident=request.safety_incident,
            targeted_state_improvement_estimate=request.targeted_state_improvement_estimate,
            aar_notes=request.aar_notes,
            actor_id=request.actor_id,
            evidence_bundle_id=option.evidence_bundle_id,
            controls=option.controls,
        )
        lesson = self.lesson_agent.from_outcome(run, option, outcome)
        updated_run = run.model_copy(
            update={
                "status": "outcome_recorded",
                "outcomes": [*run.outcomes, outcome],
                "lessons_learned": [*run.lessons_learned, lesson],
                "updated_at_utc": datetime.now(UTC),
            }
        )
        self.repository.save(updated_run)
        response = OperationalTwinOutcomeResponse(
            twin_run_id=twin_run_id,
            selected_option_id=option.scenario_option_id,
            status="outcome_recorded",
            outcome=outcome,
            lesson_learned=lesson,
            recorded_at_utc=outcome.recorded_at_utc,
        )
        self.audit_log.append(
            "operational_twin_outcome_recorded",
            {
                "twin_run_id": twin_run_id,
                "mission_id": run.mission_id,
                "selected_option_id": option.scenario_option_id,
                "outcome_id": outcome.outcome_id,
                "lesson_id": lesson.lesson_id,
                "actor_id": request.actor_id,
                "instructor_rating": request.instructor_rating,
                "safety_incident": request.safety_incident,
            },
        )
        self.shared_data_sink.append_update_event(
            build_operational_twin_outcome_update_event(updated_run, request, response)
        )
        return response


def _with_option_decision_quality(
    request: OperationalTwinRequest,
    evidence_bundle: EvidenceBundle,
    state_estimate: StateEstimate,
    option: ScenarioOption,
) -> ScenarioOption:
    decision_quality, utility_estimate, reliance_guidance = assess_scenario_option(
        request,
        evidence_bundle,
        state_estimate,
        option,
    )
    return option.model_copy(
        update={
            "decision_quality": decision_quality,
            "utility_estimate": utility_estimate,
            "reliance_guidance": reliance_guidance,
        }
    )


def _agentic_observations(
    *,
    request: OperationalTwinRequest,
    artifacts: Sequence[ArtifactRecord],
    baseline_observations: Sequence[ObservationRecord],
    llm_client: JsonAgentClient | None,
    agent_provider: str,
    max_retries: int = 1,
) -> tuple[list[ObservationRecord], list[AgentStageTrace]]:
    input_payload = {
        "mission_id": request.mission_id,
        "team_id": request.team_id,
        "mode": request.mode,
        "artifacts": [item.model_dump(mode="json") for item in artifacts],
        "baseline_observations": [
            item.model_dump(mode="json") for item in baseline_observations
        ],
    }
    if not _llm_enabled(llm_client, agent_provider):
        deterministic = list(baseline_observations)
        return deterministic, [
            _trace(
                stage="perception",
                provider="deterministic",
                model=MODEL_VERSIONS["operational_twin_perception"],
                status="completed",
                summary="Normalized artifacts and explicit observations with deterministic perception.",
                input_payload=input_payload,
                output_payload=[item.model_dump(mode="json") for item in deterministic],
            )
        ]

    started_at = datetime.now(UTC)
    last_error: Exception | None = None
    user_payload = json.dumps(input_payload, sort_keys=True, default=str)
    for _attempt in range(max_retries + 1):
        try:
            payload = llm_client.complete_json(
                stage="perception",
                system=_perception_system_prompt(),
                user=user_payload,
            )
            if not isinstance(payload.get("observations"), list):
                raise ValueError("perception response must include an observations array")
            generated = _observations_from_agent_payload(request, artifacts, payload)
            observations = _dedupe_observations([*baseline_observations, *generated])
            if not generated:
                raise ValueError("perception response returned no usable source-linked observations")
            return observations, [
                _trace(
                    stage="perception",
                    provider=llm_client.provider,
                    model=llm_client.model,
                    status="completed",
                    summary=f"OpenAI perception added {len(generated)} source-linked observations.",
                    started_at=started_at,
                    input_payload=input_payload,
                    output_payload=[item.model_dump(mode="json") for item in observations],
                )
            ]
        except Exception as exc:
            last_error = exc

    deterministic = list(baseline_observations)
    return deterministic, [
        _trace(
            stage="perception",
            provider=_provider_name(llm_client, agent_provider),
            model=_model_name(llm_client),
            status="fallback",
            summary="Agentic perception failed; deterministic observations retained.",
            error=str(last_error) if last_error is not None else None,
            fallback_reason=str(last_error) if last_error is not None else "unknown",
            started_at=started_at,
            input_payload=input_payload,
            output_payload=[item.model_dump(mode="json") for item in deterministic],
        )
    ]


def _agentic_state_estimate(
    *,
    request: OperationalTwinRequest,
    observations: Sequence[ObservationRecord],
    environment_state: EnvironmentState | None,
    baseline_state_vector: OperationalStateVector,
    baseline_uncertainty: StateUncertainty,
    llm_client: JsonAgentClient | None,
    agent_provider: str,
    max_retries: int = 1,
) -> tuple[OperationalStateVector, StateUncertainty, list[AgentStageTrace]]:
    input_payload = {
        "mission_id": request.mission_id,
        "team_id": request.team_id,
        "mode": request.mode,
        "environment_state": (
            environment_state.model_dump(mode="json")
            if environment_state is not None
            else None
        ),
        "observations": [item.model_dump(mode="json") for item in observations],
        "baseline_state_vector": baseline_state_vector.model_dump(mode="json"),
        "baseline_uncertainty": baseline_uncertainty.model_dump(mode="json"),
    }
    if not _llm_enabled(llm_client, agent_provider):
        return baseline_state_vector, baseline_uncertainty, [
            _trace(
                stage="state",
                provider="deterministic",
                model=MODEL_VERSIONS["operational_twin_state_estimator"],
                status="completed",
                summary="Estimated latent state with deterministic evidence scoring.",
                input_payload=input_payload,
                output_payload={
                    "state_vector": baseline_state_vector.model_dump(mode="json"),
                    "uncertainty": baseline_uncertainty.model_dump(mode="json"),
                },
            )
        ]

    started_at = datetime.now(UTC)
    last_error: Exception | None = None
    user_payload = json.dumps(input_payload, sort_keys=True, default=str)
    for _attempt in range(max_retries + 1):
        try:
            payload = llm_client.complete_json(
                stage="state",
                system=_state_system_prompt(),
                user=user_payload,
            )
            if not isinstance(payload.get("state_vector", payload), dict):
                raise ValueError("state response must include a state vector object")
            vector = _state_vector_from_agent_payload(payload, baseline_state_vector)
            uncertainty = _uncertainty_from_agent_payload(payload, baseline_uncertainty)
            return vector, uncertainty, [
                _trace(
                    stage="state",
                    provider=llm_client.provider,
                    model=llm_client.model,
                    status="completed",
                    summary="OpenAI state estimator produced the provisional latent state vector.",
                    started_at=started_at,
                    input_payload=input_payload,
                    output_payload={
                        "state_vector": vector.model_dump(mode="json"),
                        "uncertainty": uncertainty.model_dump(mode="json"),
                    },
                )
            ]
        except Exception as exc:
            last_error = exc

    return baseline_state_vector, baseline_uncertainty, [
        _trace(
            stage="state",
            provider=_provider_name(llm_client, agent_provider),
            model=_model_name(llm_client),
            status="fallback",
            summary="Agentic state estimation failed; deterministic state retained.",
            error=str(last_error) if last_error is not None else None,
            fallback_reason=str(last_error) if last_error is not None else "unknown",
            started_at=started_at,
            input_payload=input_payload,
            output_payload={
                "state_vector": baseline_state_vector.model_dump(mode="json"),
                "uncertainty": baseline_uncertainty.model_dump(mode="json"),
            },
        )
    ]


def _agentic_scenario_options(
    *,
    request: OperationalTwinRequest,
    observations: Sequence[ObservationRecord],
    state_estimate: StateEstimate,
    evidence_bundle: EvidenceBundle,
    baseline_options: Sequence[ScenarioOption],
    llm_client: JsonAgentClient | None,
    agent_provider: str,
    max_retries: int = 1,
) -> tuple[list[ScenarioOption], list[AgentStageTrace]]:
    input_payload = {
        "mission_id": request.mission_id,
        "team_id": request.team_id,
        "mode": request.mode,
        "training_objective": request.training_objective,
        "state_estimate": state_estimate.model_dump(mode="json"),
        "evidence_bundle": evidence_bundle.model_dump(mode="json"),
        "observations": [item.model_dump(mode="json") for item in observations],
        "baseline_options": [item.model_dump(mode="json") for item in baseline_options],
    }
    if not _llm_enabled(llm_client, agent_provider):
        return list(baseline_options), [
            _trace(
                stage="scenario",
                provider="deterministic",
                model=MODEL_VERSIONS["operational_twin_scenario_director"],
                status="completed",
                summary="Drafted scenario options with deterministic templates.",
                input_payload=input_payload,
                output_payload=[item.model_dump(mode="json") for item in baseline_options],
            )
        ]

    started_at = datetime.now(UTC)
    last_error: Exception | None = None
    user_payload = json.dumps(input_payload, sort_keys=True, default=str)
    for _attempt in range(max_retries + 1):
        try:
            payload = llm_client.complete_json(
                stage="scenario",
                system=_scenario_system_prompt(),
                user=user_payload,
            )
            options = _options_from_agent_payload(
                request=request,
                evidence_bundle=evidence_bundle,
                state_estimate=state_estimate,
                payload=payload,
            )
            if len(options) != 3:
                raise ValueError("scenario director must return exactly three options")
            return options, [
                _trace(
                    stage="scenario",
                    provider=llm_client.provider,
                    model=llm_client.model,
                    status="completed",
                    summary="OpenAI scenario director drafted exactly three governed options.",
                    started_at=started_at,
                    input_payload=input_payload,
                    output_payload=[item.model_dump(mode="json") for item in options],
                )
            ]
        except Exception as exc:
            last_error = exc

    deterministic = list(baseline_options)
    return deterministic, [
        _trace(
            stage="scenario",
            provider=_provider_name(llm_client, agent_provider),
            model=_model_name(llm_client),
            status="fallback",
            summary="Agentic scenario direction failed; deterministic options retained.",
            error=str(last_error) if last_error is not None else None,
            fallback_reason=str(last_error) if last_error is not None else "unknown",
            started_at=started_at,
            input_payload=input_payload,
            output_payload=[item.model_dump(mode="json") for item in deterministic],
        )
    ]


def _agentic_critic_review(
    *,
    request: OperationalTwinRequest,
    observations: Sequence[ObservationRecord],
    state_estimate: StateEstimate,
    evidence_bundle: EvidenceBundle,
    options: Sequence[ScenarioOption],
    llm_client: JsonAgentClient | None,
    agent_provider: str,
    max_retries: int = 1,
) -> tuple[list[ScenarioOption], list[AgentStageTrace]]:
    input_payload = {
        "mission_id": request.mission_id,
        "team_id": request.team_id,
        "mode": request.mode,
        "state_estimate": state_estimate.model_dump(mode="json"),
        "evidence_bundle": evidence_bundle.model_dump(mode="json"),
        "observations": [item.model_dump(mode="json") for item in observations],
        "options": [item.model_dump(mode="json") for item in options],
    }
    if not _llm_enabled(llm_client, agent_provider):
        return list(options), [
            _trace(
                stage="critic",
                provider="deterministic",
                model=MODEL_VERSIONS["operational_twin_critic"],
                status="completed",
                summary="Applied deterministic safety, evidence, and confidence critic checks.",
                input_payload=input_payload,
                output_payload=[item.model_dump(mode="json") for item in options],
            )
        ]

    started_at = datetime.now(UTC)
    last_error: Exception | None = None
    user_payload = json.dumps(input_payload, sort_keys=True, default=str)
    for _attempt in range(max_retries + 1):
        try:
            payload = llm_client.complete_json(
                stage="critic",
                system=_critic_system_prompt(),
                user=user_payload,
            )
            if not isinstance(payload.get("reviews"), list):
                raise ValueError("critic response must include a reviews array")
            reviewed = _apply_agent_critic_reviews(options, payload)
            return reviewed, [
                _trace(
                    stage="critic",
                    provider=llm_client.provider,
                    model=llm_client.model,
                    status="completed",
                    summary="OpenAI critic reviewed grounding, risk, option diversity, and human-approval gates.",
                    started_at=started_at,
                    input_payload=input_payload,
                    output_payload=[item.model_dump(mode="json") for item in reviewed],
                )
            ]
        except Exception as exc:
            last_error = exc

    deterministic = list(options)
    return deterministic, [
        _trace(
            stage="critic",
            provider=_provider_name(llm_client, agent_provider),
            model=_model_name(llm_client),
            status="fallback",
            summary="Agentic critic failed; deterministic critic statuses retained.",
            error=str(last_error) if last_error is not None else None,
            fallback_reason=str(last_error) if last_error is not None else "unknown",
            started_at=started_at,
            input_payload=input_payload,
            output_payload=[item.model_dump(mode="json") for item in deterministic],
        )
    ]


def _artifact_inputs(request: OperationalTwinRequest) -> list[ArtifactInput]:
    inputs = [_propagate_artifact_controls(request, item) for item in request.artifacts]
    if request.environment:
        inputs.append(
            ArtifactInput(
                kind="weather",
                content=_summary_text(request.environment),
                source_system="environment_snapshot",
                controls=request.controls,
                metadata=request.environment,
            )
        )
    if not inputs and request.observations:
        inputs.append(
            ArtifactInput(
                kind="manual_note",
                content="Manual observation batch",
                source_system=request.operator_id,
                controls=request.controls,
                metadata={"observation_count": len(request.observations)},
            )
        )
    return inputs


def _propagate_artifact_controls(
    request: OperationalTwinRequest,
    artifact: ArtifactInput,
) -> ArtifactInput:
    if artifact.controls == ControlProperties() and request.controls != ControlProperties():
        return artifact.model_copy(update={"controls": request.controls})
    return artifact


def _artifact_record(artifact: ArtifactInput) -> ArtifactRecord:
    payload = artifact.model_dump(mode="json")
    artifact_id = artifact.artifact_id or _short_id("art", payload)
    return ArtifactRecord(
        artifact_id=artifact_id,
        kind=artifact.kind,
        uri=artifact.uri,
        sha256=canonical_hash(payload).removeprefix("sha256:"),
        captured_at_utc=artifact.captured_at_utc,
        source_system=artifact.source_system,
        controls=artifact.controls,
        metadata=artifact.metadata,
    )


def _observation_records(
    request: OperationalTwinRequest,
    artifacts: Sequence[ArtifactRecord],
) -> list[ObservationRecord]:
    default_artifact_id = artifacts[0].artifact_id if artifacts else None
    records = [
        _observation_record_from_input(request, item, default_artifact_id)
        for item in request.observations
    ]
    records.extend(_observations_from_artifacts(request, artifacts))
    return _validate_observation_pipeline(request, artifacts, _dedupe_observations(records))


def _validate_observation_pipeline(
    request: OperationalTwinRequest,
    artifacts: Sequence[ArtifactRecord],
    observations: Sequence[ObservationRecord],
) -> list[ObservationRecord]:
    artifact_ids = {item.artifact_id for item in artifacts}
    artifact_controls = {item.artifact_id: item.controls for item in artifacts}
    default_artifact_id = artifacts[0].artifact_id if artifacts else None
    validated: list[ObservationRecord] = []
    for observation in observations:
        source_ids = [source_id for source_id in observation.source_artifact_ids if source_id in artifact_ids]
        if not source_ids and default_artifact_id is not None:
            source_ids = [default_artifact_id]
        if not source_ids:
            continue
        confidence = round(_clamp(observation.confidence, low=0.0, high=1.0), 3)
        controls = observation.controls
        if controls == ControlProperties() and source_ids[0] in artifact_controls:
            controls = artifact_controls[source_ids[0]]
        content, confidence = _sanitize_observation_content(observation.content, confidence)
        subject_ref = observation.subject_ref or TwinSubjectRef(
            subject_type="team",
            subject_id=request.team_id,
        )
        validated.append(
            observation.model_copy(
                update={
                    "subject_ref": subject_ref,
                    "source_artifact_ids": source_ids,
                    "confidence": confidence,
                    "controls": controls,
                    "content": content,
                }
            )
        )
    return _dedupe_observations(validated)


def _observation_record_from_input(
    request: OperationalTwinRequest,
    observation: ObservationInput,
    default_artifact_id: str | None,
) -> ObservationRecord:
    source_artifact_ids = list(observation.source_artifact_ids)
    if not source_artifact_ids and default_artifact_id is not None:
        source_artifact_ids = [default_artifact_id]
    return ObservationRecord(
        observation_id=observation.observation_id
        or _short_id("obs", observation.model_dump(mode="json")),
        mission_id=request.mission_id,
        subject_ref=observation.subject_ref,
        source_artifact_ids=source_artifact_ids,
        kind=observation.kind,
        content=observation.content,
        timestamp_utc=observation.timestamp_utc,
        geo=observation.geo,
        confidence=observation.confidence,
        controls=observation.controls,
    )


def _observations_from_artifacts(
    request: OperationalTwinRequest,
    artifacts: Sequence[ArtifactRecord],
) -> list[ObservationRecord]:
    source_by_id = {artifact.artifact_id: artifact for artifact in artifacts}
    raw_by_id = {item.artifact_id: item for item in request.artifacts if item.artifact_id is not None}
    generated: list[ObservationRecord] = []
    for artifact in artifacts:
        raw = raw_by_id.get(artifact.artifact_id)
        if raw is None:
            raw = _raw_artifact_match(request, artifact)
        text = raw.content if raw is not None else None
        if not text and not artifact.metadata:
            continue
        subject_ref = TwinSubjectRef(
            subject_type="environment" if artifact.kind == "weather" else "team",
            subject_id="environment" if artifact.kind == "weather" else request.team_id,
        )
        summary = text or _summary_text(artifact.metadata)
        if artifact.kind == "ocr_text" and _contains_prompt_injection(summary):
            summary = (
                "Untrusted processed OCR text contained instruction-like content; "
                "retained only as evidence metadata and ignored as instructions."
            )
        content: dict[str, Any] = {
            "summary": summary,
            "source_kind": artifact.kind,
        }
        content.update(artifact.metadata)
        generated.append(
            ObservationRecord(
                observation_id=_short_id("obs", {"artifact_id": artifact.artifact_id, "content": content}),
                mission_id=request.mission_id,
                subject_ref=subject_ref,
                source_artifact_ids=[source_by_id[artifact.artifact_id].artifact_id],
                kind=_observation_kind_for_artifact(artifact.kind),
                content=content,
                timestamp_utc=artifact.captured_at_utc,
                confidence=_artifact_confidence(artifact.kind),
                controls=artifact.controls,
            )
        )
    return generated


def _raw_artifact_match(
    request: OperationalTwinRequest,
    artifact: ArtifactRecord,
) -> ArtifactInput | None:
    for item in request.artifacts:
        if item.artifact_id == artifact.artifact_id:
            return item
        if item.artifact_id is None and _short_id("art", item.model_dump(mode="json")) == artifact.artifact_id:
            return item
    if artifact.kind == "weather":
        return ArtifactInput(
            kind="weather",
            content=_summary_text(request.environment),
            source_system="environment_snapshot",
            controls=request.controls,
            metadata=request.environment,
        )
    return None


def _sanitize_observation_content(
    content: dict[str, Any],
    confidence: float,
) -> tuple[dict[str, Any], float]:
    text = _summary_text(content).lower()
    if any(term in text for term in _PROHIBITED_OBSERVATION_TERMS):
        sanitized = {
            "summary": (
                "Source text included a prohibited inference; retained only as "
                "non-inferential evidence metadata."
            ),
            "redacted_prohibited_assertion": True,
        }
        return sanitized, round(min(confidence, 0.35), 3)
    if _contains_prompt_injection(text):
        sanitized = dict(content)
        sanitized["summary"] = (
            "Source text contained instruction-like content; treated as untrusted evidence "
            "and ignored as operational instructions."
        )
        sanitized["untrusted_prompt_like_text"] = True
        sanitized.pop("text", None)
        return sanitized, round(min(confidence, 0.60), 3)
    return content, confidence


def _contains_prompt_injection(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in _PROMPT_INJECTION_TERMS)


def _environment_state(request: OperationalTwinRequest) -> EnvironmentState | None:
    if not request.environment:
        return None
    return EnvironmentState(
        environment_state_id=f"env-{uuid4()}",
        mission_id=request.mission_id,
        weather=_string_or_none(request.environment.get("weather")),
        terrain=_string_or_none(request.environment.get("terrain")),
        visibility=_string_or_none(request.environment.get("visibility")),
        temperature_c=_float_or_none(request.environment.get("temperature_c")),
        precipitation=_string_or_none(request.environment.get("precipitation")),
        wind_speed=_float_or_none(request.environment.get("wind_speed")),
        controls=request.controls,
    )


def _estimate_state_vector(
    observations: Sequence[ObservationRecord],
    environment: EnvironmentState | None,
) -> OperationalStateVector:
    text = " ".join(_observation_text(item).lower() for item in observations)
    fatigue_measurements = [_fatigue_measurement(item) for item in observations]
    fatigue_measurements = [item for item in fatigue_measurements if item is not None]
    measured_fatigue = (
        sum(fatigue_measurements) / len(fatigue_measurements)
        if fatigue_measurements
        else 0.30
    )
    fatigue_hits = _hit_ratio(
        text,
        ("fatigue", "sleep", "tired", "exhausted", "hours awake", "night movement", "cold"),
        divisor=4,
    )
    weather_burden = _weather_burden(environment)
    fatigue_burden = _clamp(0.24 + measured_fatigue * 0.56 + fatigue_hits * 0.16 + weather_burden * 0.14)

    clarity_penalty = min(
        0.52,
        _hit_ratio(
            text,
            (
                "missed comms",
                "missed acknowledgement",
                "conflicting",
                "ambiguous",
                "unclear",
                "contradictory",
                "lost",
                "misread",
                "delayed comms",
            ),
            divisor=5,
        )
        * 0.45
        + fatigue_burden * 0.13,
    )
    situational_clarity = _clamp(0.76 - clarity_penalty + _positive_signal(text) * 0.05)

    cohesion_penalty = _hit_ratio(
        text,
        ("dissent", "handoff", "hesitation", "withheld", "blame", "coordination friction"),
        divisor=4,
    ) * 0.34
    cohesion = _clamp(0.70 - cohesion_penalty + _hit_ratio(text, ("cohesive", "trusted", "mutual"), divisor=3) * 0.10)

    decision_penalty = _hit_ratio(
        text,
        (
            "assumption",
            "single option",
            "confirmation",
            "fixated",
            "failed to question",
            "late decision",
            "decision latency",
        ),
        divisor=5,
    ) * 0.44
    leader_decision_quality = _clamp(0.70 - decision_penalty - fatigue_burden * 0.08 + _positive_signal(text) * 0.07)

    tempo_risk = _clamp(
        0.28
        + _hit_ratio(
            text,
            (
                "delayed",
                "late",
                "timing",
                "compressed",
                "missed comms",
                "relay",
                "route disruption",
                "casualty",
            ),
            divisor=6,
        )
        * 0.46
        + weather_burden * 0.16
        + fatigue_burden * 0.12
    )

    weakest_skill_gap = 1.0 - min(situational_clarity, cohesion, leader_decision_quality)
    current_challenge = _clamp(0.32 + fatigue_burden * 0.32 + tempo_risk * 0.22 + weakest_skill_gap * 0.20)
    training_challenge_gap = _clamp(0.58 - current_challenge, low=-1.0, high=1.0)

    return OperationalStateVector(
        fatigue_burden=round(fatigue_burden, 3),
        situational_clarity=round(situational_clarity, 3),
        cohesion=round(cohesion, 3),
        leader_decision_quality=round(leader_decision_quality, 3),
        mission_tempo_risk=round(tempo_risk, 3),
        training_challenge_gap=round(training_challenge_gap, 3),
    )


def _estimate_uncertainty(
    observations: Sequence[ObservationRecord],
    state_vector: OperationalStateVector,
) -> StateUncertainty:
    source_count = len({source for item in observations for source in item.source_artifact_ids})
    kind_count = len({item.kind for item in observations})
    average_confidence = sum(item.confidence for item in observations) / len(observations)
    overall = _clamp(0.55 - min(len(observations), 8) * 0.035 - source_count * 0.025 - kind_count * 0.02)
    overall = _clamp(overall + (1.0 - average_confidence) * 0.25, low=0.12, high=0.82)
    fatigue_evidence = any(_fatigue_measurement(item) is not None for item in observations)
    by_field = {
        "fatigue_burden": _clamp(overall + (0.0 if fatigue_evidence else 0.10), low=0.08, high=0.90),
        "situational_clarity": _clamp(overall + (0.03 if state_vector.situational_clarity > 0.68 else -0.02), low=0.08, high=0.90),
        "cohesion": _clamp(overall + 0.06, low=0.08, high=0.90),
        "leader_decision_quality": _clamp(overall + 0.03, low=0.08, high=0.90),
        "mission_tempo_risk": _clamp(overall, low=0.08, high=0.90),
        "training_challenge_gap": _clamp(overall + 0.05, low=0.08, high=0.90),
    }
    return StateUncertainty(overall=round(overall, 3), by_field={key: round(value, 3) for key, value in by_field.items()})


def _policy_checks(
    request: OperationalTwinRequest,
    observations: Sequence[ObservationRecord],
    state_vector: OperationalStateVector,
) -> EvidenceBundlePolicyChecks:
    classification_ok = _classification_ok(request, observations)
    control_marking_ok = _controls_ok(request, observations)
    stale_source_ok = _sources_fresh(observations)
    evidence_threshold_ok = len(observations) >= 2
    fatigue_overload_ok = not (
        state_vector.fatigue_burden > 0.88 and state_vector.mission_tempo_risk > 0.72
    )
    safety_ok = fatigue_overload_ok
    governance_findings = _policy_findings(
        classification_ok=classification_ok,
        control_marking_ok=control_marking_ok,
        stale_source_ok=stale_source_ok,
        evidence_threshold_ok=evidence_threshold_ok,
        fatigue_overload_ok=fatigue_overload_ok,
        human_approval_required=request.require_human_approval,
    )
    return EvidenceBundlePolicyChecks(
        classification_ok=classification_ok,
        evidence_threshold_ok=evidence_threshold_ok,
        safety_ok=safety_ok,
        human_approval_required=request.require_human_approval,
        stale_source_ok=stale_source_ok,
        control_marking_ok=control_marking_ok,
        fatigue_overload_ok=fatigue_overload_ok,
        governance_findings=governance_findings,
    )


def _evidence_bundle(
    *,
    request: OperationalTwinRequest,
    evidence_bundle_id: str,
    artifacts: Sequence[ArtifactRecord],
    observations: Sequence[ObservationRecord],
    state_estimate: StateEstimate,
    policy_checks: EvidenceBundlePolicyChecks,
    previous_action_hash: str,
) -> EvidenceBundle:
    state_payload = state_estimate.model_dump(mode="json")
    claim_text = _claim_text(state_estimate)
    current_action_hash = canonical_hash(
        {
            "mission_id": request.mission_id,
            "team_id": request.team_id,
            "state_estimate": state_payload,
            "observation_ids": [item.observation_id for item in observations],
            "previous_action_hash": previous_action_hash,
        }
    ).removeprefix("sha256:")
    return EvidenceBundle(
        bundle_id=evidence_bundle_id,
        claim_type="operational_twin_scenario_recommendation",
        claim_text=claim_text,
        mission_id=request.mission_id,
        subject_refs=_subject_refs(request, observations),
        source_artifacts=[
            EvidenceBundleArtifact(
                artifact_id=item.artifact_id,
                kind=item.kind,
                sha256=item.sha256,
                captured_at_utc=item.captured_at_utc,
                source_system=item.source_system,
            )
            for item in artifacts
        ],
        derived_observations=[
            EvidenceBundleObservation(
                observation_id=item.observation_id,
                kind=item.kind,
                confidence=item.confidence,
                summary=_content_summary(item.content),
            )
            for item in observations
        ],
        state_inputs={
            "state_estimate_id": state_estimate.state_estimate_id,
            "state_vector": state_estimate.state_vector.model_dump(mode="json"),
            "uncertainty": state_estimate.uncertainty.model_dump(mode="json"),
        },
        models=[
            EvidenceBundleModelTrace(
                stage="perception",
                model_name="rule_based_multimodal_normalizer",
                model_version=MODEL_VERSIONS["operational_twin_perception"],
                output_confidence=_mean([item.confidence for item in observations]),
            ),
            EvidenceBundleModelTrace(
                stage="state",
                model_name="probabilistic_state_estimator",
                model_version=MODEL_VERSIONS["operational_twin_state_estimator"],
                output_confidence=1.0 - state_estimate.uncertainty.overall,
            ),
            EvidenceBundleModelTrace(
                stage="scenario",
                model_name="bounded_scenario_director",
                model_version=MODEL_VERSIONS["operational_twin_scenario_director"],
                output_confidence=1.0 - state_estimate.uncertainty.overall,
            ),
            EvidenceBundleModelTrace(
                stage="critic",
                model_name="policy_and_grounding_critic",
                model_version=MODEL_VERSIONS["operational_twin_critic"],
                output_confidence=0.90 if policy_checks.safety_ok else 0.62,
            ),
        ],
        policy_checks=policy_checks,
        hash_chain=EvidenceBundleHashChain(
            previous_action_hash=previous_action_hash,
            current_action_hash=current_action_hash,
        ),
        controls=request.controls,
    )


def _scenario_options(
    *,
    request: OperationalTwinRequest,
    observations: Sequence[ObservationRecord],
    state_estimate: StateEstimate,
    evidence_bundle: EvidenceBundle,
) -> list[ScenarioOption]:
    target = _target_dimension(observations, state_estimate.state_vector)
    templates = list(_option_templates(target, request.mode))
    if (
        not evidence_bundle.policy_checks.evidence_threshold_ok
        or state_estimate.uncertainty.overall > 0.65
        or not evidence_bundle.policy_checks.stale_source_ok
    ):
        templates[0] = _hold_option_template(request.mode)
    options: list[ScenarioOption] = []
    for index, template in enumerate(templates):
        risk = _option_risk(index, state_estimate.state_vector)
        confidence = _clamp(1.0 - state_estimate.uncertainty.overall - risk * 0.08, low=0.05, high=0.95)
        critic_status, reasons = _critic_status(
            evidence_bundle=evidence_bundle,
            risk_score=risk,
            confidence=confidence,
            state_vector=state_estimate.state_vector,
        )
        options.append(
            ScenarioOption(
                scenario_option_id=f"opt-{uuid4()}",
                mission_id=request.mission_id,
                option_type=_option_type(request.mode, index),
                title=str(template["title"]),
                narrative=str(template["narrative"]),
                predicted_effect=ScenarioPredictedEffect(
                    target_state_change=str(template["target_state_change"]),
                    expected_learning_value=float(template["learning"])
                    if request.mode == "training"
                    else None,
                    expected_mission_benefit=float(template["mission_benefit"])
                    if request.mode == "mission"
                    else None,
                ),
                risk_score=round(risk, 3),
                confidence=round(confidence, 3),
                critic_status=critic_status,
                critic_reasons=reasons,
                evidence_bundle_id=evidence_bundle.bundle_id,
                status="draft",
                controls=request.controls,
            )
        )
    return options


def _observations_from_agent_payload(
    request: OperationalTwinRequest,
    artifacts: Sequence[ArtifactRecord],
    payload: dict[str, Any],
) -> list[ObservationRecord]:
    items = payload.get("observations")
    if not isinstance(items, list):
        return []
    artifact_ids = {item.artifact_id for item in artifacts}
    default_sources = [item.artifact_id for item in artifacts[:2]]
    observations: list[ObservationRecord] = []
    for index, item in enumerate(items[:12]):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "manual_note"))
        if kind not in _OBSERVATION_KINDS:
            kind = "manual_note"
        content = item.get("content")
        if not isinstance(content, dict):
            content = {"summary": str(item.get("summary", ""))}
        if not _summary_text(content).strip():
            continue
        raw_source_ids = item.get("source_artifact_ids")
        if isinstance(raw_source_ids, list):
            source_ids = [
                str(source_id)
                for source_id in raw_source_ids
                if str(source_id) in artifact_ids
            ]
        else:
            source_ids = []
        if not source_ids:
            source_ids = default_sources
        if not source_ids:
            continue
        subject_ref = _subject_ref_from_agent_item(item, request)
        observations.append(
            ObservationRecord(
                observation_id=str(item.get("observation_id") or f"obs-agent-{uuid4()}"),
                mission_id=request.mission_id,
                subject_ref=subject_ref,
                source_artifact_ids=source_ids,
                kind=kind,  # type: ignore[arg-type]
                content=content,
                timestamp_utc=datetime.now(UTC),
                confidence=_clamp(_float_or_none(item.get("confidence")) or 0.72, low=0.20, high=0.98),
                controls=request.controls,
            )
        )
    return observations


def _subject_ref_from_agent_item(
    item: dict[str, Any],
    request: OperationalTwinRequest,
) -> TwinSubjectRef | None:
    subject = item.get("subject_ref")
    if not isinstance(subject, dict):
        return TwinSubjectRef(subject_type="team", subject_id=request.team_id)
    subject_type = str(subject.get("subject_type", "team"))
    subject_id = str(subject.get("subject_id", request.team_id))
    if subject_type not in {"person", "team", "mission", "environment"}:
        subject_type = "team"
    return TwinSubjectRef(subject_type=subject_type, subject_id=subject_id)  # type: ignore[arg-type]


def _state_vector_from_agent_payload(
    payload: dict[str, Any],
    fallback: OperationalStateVector,
) -> OperationalStateVector:
    raw = payload.get("state_vector")
    if not isinstance(raw, dict):
        raw = payload
    fallback_payload = fallback.model_dump(mode="json")
    values = {
        key: _bounded_state_value(raw.get(key), fallback_payload[key], low=-1.0 if key == "training_challenge_gap" else 0.0)
        for key in (
            "fatigue_burden",
            "situational_clarity",
            "cohesion",
            "leader_decision_quality",
            "mission_tempo_risk",
            "training_challenge_gap",
        )
    }
    return OperationalStateVector(**values)


def _uncertainty_from_agent_payload(
    payload: dict[str, Any],
    fallback: StateUncertainty,
) -> StateUncertainty:
    raw = payload.get("uncertainty")
    if not isinstance(raw, dict):
        return fallback
    overall = _clamp(_float_or_none(raw.get("overall")) or fallback.overall, low=0.05, high=0.95)
    by_field_payload = raw.get("by_field")
    by_field: dict[str, float] = {}
    if isinstance(by_field_payload, dict):
        for key, fallback_value in fallback.by_field.items():
            by_field[key] = _clamp(
                _float_or_none(by_field_payload.get(key)) or fallback_value,
                low=0.05,
                high=0.95,
            )
    else:
        by_field = dict(fallback.by_field)
    return StateUncertainty(
        overall=round(overall, 3),
        by_field={key: round(value, 3) for key, value in by_field.items()},
    )


def _options_from_agent_payload(
    *,
    request: OperationalTwinRequest,
    evidence_bundle: EvidenceBundle,
    state_estimate: StateEstimate,
    payload: dict[str, Any],
) -> list[ScenarioOption]:
    raw_options = payload.get("scenario_options") or payload.get("options")
    if not isinstance(raw_options, list):
        return []
    options: list[ScenarioOption] = []
    for index, item in enumerate(raw_options[:3]):
        if not isinstance(item, dict):
            continue
        risk_score = _clamp(_float_or_none(item.get("risk_score")) or _option_risk(index, state_estimate.state_vector))
        confidence = _clamp(_float_or_none(item.get("confidence")) or (1.0 - state_estimate.uncertainty.overall), low=0.05, high=0.95)
        critic_status, critic_reasons = _critic_status(
            evidence_bundle=evidence_bundle,
            risk_score=risk_score,
            confidence=confidence,
            state_vector=state_estimate.state_vector,
        )
        predicted = item.get("predicted_effect")
        if not isinstance(predicted, dict):
            predicted = {}
        target_state_change = str(
            predicted.get("target_state_change")
            or item.get("target_state_change")
            or "Improve the targeted mission or training state."
        )
        options.append(
            ScenarioOption(
                scenario_option_id=str(item.get("scenario_option_id") or f"opt-{uuid4()}"),
                mission_id=request.mission_id,
                option_type=_option_type(request.mode, index),  # type: ignore[arg-type]
                title=str(item.get("title") or f"Agent option {index + 1}"),
                narrative=str(item.get("narrative") or item.get("proposed_action") or ""),
                predicted_effect=ScenarioPredictedEffect(
                    target_state_change=target_state_change,
                    expected_learning_value=_optional_bounded_float(
                        predicted.get("expected_learning_value")
                        if "expected_learning_value" in predicted
                        else item.get("expected_learning_value")
                    )
                    if request.mode == "training"
                    else None,
                    expected_mission_benefit=_optional_bounded_float(
                        predicted.get("expected_mission_benefit")
                        if "expected_mission_benefit" in predicted
                        else item.get("expected_mission_benefit")
                    )
                    if request.mode == "mission"
                    else None,
                ),
                risk_score=round(risk_score, 3),
                confidence=round(confidence, 3),
                critic_status=critic_status,
                critic_reasons=critic_reasons,
                evidence_bundle_id=evidence_bundle.bundle_id,
                status="draft",
                controls=request.controls,
            )
        )
    return [item for item in options if item.narrative.strip()]


def _apply_agent_critic_reviews(
    options: Sequence[ScenarioOption],
    payload: dict[str, Any],
) -> list[ScenarioOption]:
    reviews = payload.get("reviews")
    if not isinstance(reviews, list):
        return list(options)
    updated = list(options)
    for raw_review in reviews:
        if not isinstance(raw_review, dict):
            continue
        parsed_index = _float_or_none(raw_review.get("index"))
        index = int(parsed_index) if parsed_index is not None else -1
        if index < 0 or index >= len(updated):
            continue
        option = updated[index]
        agent_status = str(raw_review.get("critic_status", option.critic_status))
        if agent_status not in _CRITIC_STATUSES:
            agent_status = option.critic_status
        merged_status = _more_restrictive_status(option.critic_status, agent_status)
        reasons = raw_review.get("critic_reasons") or raw_review.get("reasons")
        if isinstance(reasons, list):
            merged_reasons = [str(reason) for reason in reasons if str(reason).strip()]
        else:
            merged_reasons = []
        if not merged_reasons:
            merged_reasons = list(option.critic_reasons)
        agent_risk = _float_or_none(raw_review.get("risk_score"))
        agent_confidence = _float_or_none(raw_review.get("confidence"))
        updated[index] = option.model_copy(
            update={
                "critic_status": merged_status,
                "critic_reasons": merged_reasons,
                "risk_score": round(max(option.risk_score, _clamp(agent_risk or option.risk_score)), 3),
                "confidence": round(min(option.confidence, _clamp(agent_confidence or option.confidence)), 3),
            }
        )
    return updated


def _apply_option_governance_checks(
    *,
    request: OperationalTwinRequest,
    evidence_bundle: EvidenceBundle,
    state_vector: OperationalStateVector,
    options: Sequence[ScenarioOption],
) -> list[ScenarioOption]:
    updated: list[ScenarioOption] = []
    duplicate_indexes = _duplicate_option_indexes(options)
    for index, option in enumerate(options[:3]):
        deterministic_status, deterministic_reasons = _critic_status(
            evidence_bundle=evidence_bundle,
            risk_score=option.risk_score,
            confidence=option.confidence,
            state_vector=state_vector,
        )
        status = _more_restrictive_status(option.critic_status, deterministic_status)
        reasons = _dedupe_reasons([*option.critic_reasons, *deterministic_reasons])
        if index in duplicate_indexes:
            status = _more_restrictive_status(status, "escalate")
            reasons = _dedupe_reasons([*reasons, "Duplicate option detection requires review."])
        if option.evidence_bundle_id != evidence_bundle.bundle_id:
            status = _more_restrictive_status(status, "reject")
            reasons = _dedupe_reasons([*reasons, "Option does not cite the current evidence bundle."])
        if request.require_human_approval and option.status != "draft":
            status = _more_restrictive_status(status, "reject")
            reasons = _dedupe_reasons([*reasons, "Generated options must remain draft until human decision."])
        updated.append(
            option.model_copy(
                update={
                    "critic_status": status,
                    "critic_reasons": reasons,
                    "evidence_bundle_id": evidence_bundle.bundle_id,
                    "status": "draft",
                    "controls": request.controls,
                }
            )
        )
    if len(updated) != 3:
        raise ValueError("operational twin scenario generation must preserve exactly three options")
    return updated


def _duplicate_option_indexes(options: Sequence[ScenarioOption]) -> set[int]:
    duplicate_indexes: set[int] = set()
    for left_index, left in enumerate(options):
        for right_index, right in enumerate(options[left_index + 1 :], start=left_index + 1):
            if (
                _text_similarity(left.title, right.title) > 0.82
                or _text_similarity(left.narrative, right.narrative) > 0.72
            ):
                duplicate_indexes.add(left_index)
                duplicate_indexes.add(right_index)
    return duplicate_indexes


def _text_similarity(left: str, right: str) -> float:
    left_terms = _significant_terms(left)
    right_terms = _significant_terms(right)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


def _significant_terms(value: str) -> set[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "for",
        "in",
        "of",
        "or",
        "the",
        "to",
        "with",
        "option",
        "primary",
        "alternate",
        "safer",
        "fallback",
    }
    return {
        token.strip(".,:;!?()[]{}").lower()
        for token in value.split()
        if len(token.strip(".,:;!?()[]{}")) > 2
        and token.strip(".,:;!?()[]{}").lower() not in stopwords
    }


def _dedupe_reasons(reasons: Sequence[str]) -> list[str]:
    deduped: dict[str, str] = {}
    for reason in reasons:
        stripped = str(reason).strip()
        if stripped:
            deduped[stripped.lower()] = stripped
    return list(deduped.values())


def _trace(
    *,
    stage: str,
    provider: str,
    model: str,
    status: str,
    summary: str,
    error: str | None = None,
    fallback_reason: str | None = None,
    started_at: datetime | None = None,
    input_payload: Any | None = None,
    output_payload: Any | None = None,
) -> AgentStageTrace:
    resolved_started_at = started_at or datetime.now(UTC)
    completed_at = datetime.now(UTC)
    return AgentStageTrace(
        stage=stage,
        provider=provider,
        model=model,
        status=status,  # type: ignore[arg-type]
        summary=summary,
        error=error,
        input_hash=canonical_hash(input_payload) if input_payload is not None else None,
        output_hash=canonical_hash(output_payload) if output_payload is not None else None,
        duration_ms=max(0, int((completed_at - resolved_started_at).total_seconds() * 1000)),
        fallback_reason=fallback_reason,
        started_at=resolved_started_at,
        completed_at=completed_at,
    )


def _llm_enabled(llm_client: JsonAgentClient | None, agent_provider: str) -> bool:
    return llm_client is not None and agent_provider in {"auto", "openai"}


def _provider_name(llm_client: JsonAgentClient | None, agent_provider: str) -> str:
    if llm_client is not None:
        return llm_client.provider
    return agent_provider


def _model_name(llm_client: JsonAgentClient | None) -> str:
    if llm_client is not None:
        return llm_client.model
    return "unconfigured"


def _bounded_state_value(value: Any, fallback: float, *, low: float = 0.0) -> float:
    parsed = _float_or_none(value)
    if parsed is None:
        parsed = fallback
    return round(_clamp(parsed, low=low, high=1.0), 3)


def _optional_bounded_float(value: Any) -> float | None:
    parsed = _float_or_none(value)
    if parsed is None:
        return None
    return round(_clamp(parsed), 3)


def _more_restrictive_status(current: str, proposed: str) -> str:
    order = {"pass": 0, "modify": 1, "escalate": 2, "reject": 3}
    return proposed if order[proposed] > order[current] else current


def _perception_system_prompt() -> str:
    return (
        "You are the Perception Agent for a governed mission/training operational twin. "
        "Extract observable atomic facts only. Do not infer motives, psychology, guilt, "
        "fitness, protected attributes, or personnel judgments. Return observations "
        "matching the configured JSON schema. Each item must include kind, "
        "content.summary, source_artifact_ids, confidence, and subject_ref."
    )


def _state_system_prompt() -> str:
    return (
        "You are the Cognitive State Estimator Agent. Estimate only provisional latent "
        "state from observable evidence. Return JSON with state_vector and uncertainty. "
        "State vector keys are fatigue_burden, situational_clarity, cohesion, "
        "leader_decision_quality, mission_tempo_risk, and training_challenge_gap. "
        "All values except training_challenge_gap are 0..1; training_challenge_gap is -1..1. "
        "Keep uncertainty calibrated and avoid fixed-talent claims."
    )


def _scenario_system_prompt() -> str:
    return (
        "You are the Scenario Director Agent for a human-approved operational twin. "
        "Return exactly three distinct options under scenario_options. In training "
        "mode, options are scenario injects; in mission mode, options are advisory "
        "rehearsal variants or COAs. Each option must include title, narrative, "
        "predicted_effect.target_state_change, predicted_effect.expected_learning_value, "
        "predicted_effect.expected_mission_benefit, risk_score, and confidence. Use null "
        "for the predicted-effect field that does not apply to the current mode. Do not "
        "approve anything and do not bypass human review."
    )


def _critic_system_prompt() -> str:
    return (
        "You are the Safety and Doctrine Critic Agent. Review options for evidence "
        "grounding, duplicate logic, unsafe escalation, fatigue overload, classification "
        "or control issues, and automation-bias risk. Return JSON with reviews. Each "
        "review has index, critic_status as pass/modify/escalate/reject, critic_reasons, "
        "risk_score, and confidence. Never weaken deterministic safety gates."
    )


def _option_templates(target: str, mode: str) -> tuple[dict[str, str | float], ...]:
    if target == "systems_thinking":
        primary = "Add delayed comms relay plus civilian movement on the flank and a support-timing change."
        isolate = "Pause contact pressure and have the leader map terrain, timing, support, and second-order effects."
        alternate = "Move the same relationship into a logistics or casualty-evacuation branch."
        target_change = "Improve systems thinking across terrain, timing, support, and second-order effects."
    elif target == "critical_thinking":
        primary = "Introduce a plausible confirming report that is wrong and require an assumption check."
        isolate = "Run a low-noise branch-plan drill requiring two alternatives and one disconfirming cue."
        alternate = "Transfer the assumption check to a new problem where the first answer is attractive but brittle."
        target_change = "Improve hypothesis testing and reduce premature closure."
    elif target == "sensemaking":
        primary = "Introduce two conflicting spot reports and require the leader to update the situation model aloud."
        isolate = "Run a short cue-sort repetition using the last three observations without adding contact pressure."
        alternate = "Move the ambiguity pattern into a different terrain and time window."
        target_change = "Improve cue recognition, ambiguity handling, and situation model updates."
    elif target == "leadership_communication":
        primary = "Create subordinate dissent during a handoff and require a concise backbrief."
        isolate = "Run a backbrief-only repetition with role clarity and confirmation checks."
        alternate = "Transfer the communication demand to a dispersed element with delayed feedback."
        target_change = "Improve intent transfer, coordination, and backbrief discipline."
    elif target == "state_management":
        primary = "Hold general difficulty and target a short recovery-informed decision repetition."
        isolate = "Lower environmental noise and test the same decision with hydration, sleep, and pacing noted."
        alternate = "Run a brief rehearsal variant that separates fatigue effects from skill execution."
        target_change = "Reduce state-dependent performance risk before increasing challenge."
    else:
        primary = "Add a focused task-standard interruption with a measurable decision checkpoint."
        isolate = "Repeat the task standard in a controlled lane with one explicit constraint."
        alternate = "Transfer the task standard to a different role while preserving timing pressure."
        target_change = "Improve execution reliability under controlled pressure."

    if mode == "mission":
        primary = primary.replace("Add", "Plan for").replace("Introduce", "Plan for")
        isolate = isolate.replace("Run", "Rehearse").replace("Pause", "Rehearse without")
        alternate = alternate.replace("Move", "Prepare")

    return (
        {
            "title": f"Primary option: target {target.replace('_', ' ')}",
            "narrative": primary,
            "target_state_change": target_change,
            "learning": 0.82,
            "mission_benefit": 0.74,
        },
        {
            "title": "Safer fallback: isolate the weak sub-skill",
            "narrative": isolate,
            "target_state_change": target_change,
            "learning": 0.66,
            "mission_benefit": 0.58,
        },
        {
            "title": "Alternate option: test transfer in a different branch",
            "narrative": alternate,
            "target_state_change": target_change,
            "learning": 0.70,
            "mission_benefit": 0.66,
        },
    )


def _hold_option_template(mode: str) -> dict[str, str | float]:
    action = (
        "Hold COA recommendation and collect one fresh independent source before commander review."
        if mode == "mission"
        else "Hold scenario pressure and collect one fresh independent observation before the next inject."
    )
    return {
        "title": "Hold option: collect more evidence",
        "narrative": action,
        "target_state_change": "Improve evidence confidence before selecting a consequential option.",
        "learning": 0.42,
        "mission_benefit": 0.38,
    }


def _critic_status(
    *,
    evidence_bundle: EvidenceBundle,
    risk_score: float,
    confidence: float,
    state_vector: OperationalStateVector,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if (
        not evidence_bundle.policy_checks.classification_ok
        or not evidence_bundle.policy_checks.control_marking_ok
    ):
        reasons.append("Classification or control marking check failed.")
        return "reject", reasons
    if not evidence_bundle.policy_checks.safety_ok:
        reasons.append("Safety envelope is exceeded by fatigue and tempo risk.")
        return "reject", reasons

    status = "pass"
    if not evidence_bundle.policy_checks.human_approval_required:
        status = "escalate"
        reasons.append("Missing human approval gate for a consequential option.")
    if not evidence_bundle.policy_checks.evidence_threshold_ok:
        status = "escalate"
        reasons.append("Fewer than two observations support the recommendation.")
    if not evidence_bundle.policy_checks.stale_source_ok:
        status = "escalate"
        reasons.append("One or more source observations exceed the stale-source threshold.")
    if confidence < 0.64:
        status = "escalate"
        reasons.append("Option confidence is below approval-ready threshold.")
    if risk_score > 0.74:
        status = "escalate"
        reasons.append("Risk score requires commander or senior trainer review.")
    elif state_vector.fatigue_burden > 0.72 and risk_score > 0.44:
        status = "modify" if status == "pass" else status
        reasons.append("Fatigue burden is elevated; do not intensify general difficulty.")
    if not reasons:
        reasons.append("Grounded in current evidence bundle and remains human-approval gated.")
    return status, reasons


def _lesson_from_decision(
    run: OperationalTwinResponse,
    option: ScenarioOption,
    decision: OperationalTwinDecision,
) -> LessonLearned | None:
    if decision.decision != "approved":
        return None
    severity = "high" if option.risk_score > 0.68 else "medium" if option.risk_score > 0.38 else "low"
    return LessonLearned(
        lesson_id=f"lesson-{uuid4()}",
        mission_id=run.mission_id,
        category="operational_twin_approved_option",
        summary=f"Approved option: {option.title}.",
        root_cause=option.predicted_effect.target_state_change,
        recommended_training_delta=option.narrative,
        recommended_mission_delta="Preserve evidence-linked approval and compare outcome in the next AAR.",
        severity=severity,
        status="draft",
        evidence_bundle_id=option.evidence_bundle_id,
        controls=option.controls,
    )


def _status_from_decision(decision: str) -> str:
    return {
        "approved": "approved",
        "rejected": "rejected",
        "escalated": "escalated",
    }[decision]


def _option_type(mode: str, index: int) -> str:
    if mode == "training":
        return "training_inject"
    return "rehearsal_variant" if index == 1 else "mission_coa"


def _option_risk(index: int, vector: OperationalStateVector) -> float:
    base = (0.42, 0.20, 0.34)[index]
    return _clamp(
        base
        + vector.fatigue_burden * (0.20 if index != 1 else 0.08)
        + vector.mission_tempo_risk * (0.14 if index != 1 else 0.05)
    )


def _target_dimension(
    observations: Sequence[ObservationRecord],
    vector: OperationalStateVector,
) -> str:
    text = " ".join(_observation_text(item).lower() for item in observations)
    if _contains_any(text, ("second-order", "terrain", "timing", "support", "logistics", "civilian", "comms relay")):
        return "systems_thinking"
    if _contains_any(text, ("assumption", "single option", "confirmation", "hypothesis")):
        return "critical_thinking"
    if _contains_any(text, ("ambiguous", "conflicting", "unclear", "misread", "missed cue")):
        return "sensemaking"
    if _contains_any(text, ("backbrief", "handoff", "subordinate", "coordination friction", "dissent")):
        return "leadership_communication"
    if vector.fatigue_burden > 0.74:
        return "state_management"
    return "execution_reliability"


def _claim_text(state_estimate: StateEstimate) -> str:
    vector = state_estimate.state_vector
    return (
        "Draft scenario options are based on fatigue burden "
        f"{vector.fatigue_burden:.2f}, situational clarity "
        f"{vector.situational_clarity:.2f}, mission tempo risk "
        f"{vector.mission_tempo_risk:.2f}, and uncertainty "
        f"{state_estimate.uncertainty.overall:.2f}."
    )


def _subject_refs(
    request: OperationalTwinRequest,
    observations: Sequence[ObservationRecord],
) -> list[TwinSubjectRef]:
    refs = [
        TwinSubjectRef(subject_type="mission", subject_id=request.mission_id),
        TwinSubjectRef(subject_type="team", subject_id=request.team_id),
    ]
    for observation in observations:
        if observation.subject_ref is not None:
            refs.append(observation.subject_ref)
    deduped: dict[tuple[str, str], TwinSubjectRef] = {}
    for ref in refs:
        deduped[(ref.subject_type, ref.subject_id)] = ref
    return list(deduped.values())


def _classification_ok(
    request: OperationalTwinRequest,
    observations: Sequence[ObservationRecord],
) -> bool:
    markings = [request.controls.classification_marking]
    markings.extend(item.controls.classification_marking for item in observations)
    return all(marking for marking in markings) and len(set(markings)) == 1


def _controls_ok(
    request: OperationalTwinRequest,
    observations: Sequence[ObservationRecord],
) -> bool:
    expected = request.controls
    return all(
        item.controls.classification_marking == expected.classification_marking
        and item.controls.releasability == expected.releasability
        and item.controls.need_to_know_domain == expected.need_to_know_domain
        and item.controls.source_handling_code == expected.source_handling_code
        for item in observations
    )


def _sources_fresh(observations: Sequence[ObservationRecord]) -> bool:
    now = datetime.now(UTC)
    return all(now - _as_utc(item.timestamp_utc) <= _STALE_SOURCE_THRESHOLD for item in observations)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _policy_findings(
    *,
    classification_ok: bool,
    control_marking_ok: bool,
    stale_source_ok: bool,
    evidence_threshold_ok: bool,
    fatigue_overload_ok: bool,
    human_approval_required: bool,
) -> list[str]:
    findings: list[str] = []
    if not classification_ok:
        findings.append("classification mismatch")
    if not control_marking_ok:
        findings.append("marking/control mismatch")
    if not stale_source_ok:
        findings.append("stale source threshold exceeded")
    if not evidence_threshold_ok:
        findings.append("evidence threshold not met")
    if not fatigue_overload_ok:
        findings.append("fatigue overload threshold exceeded")
    if not human_approval_required:
        findings.append("missing human approval gate")
    return findings


def _observation_kind_for_artifact(kind: str) -> TwinObservationKind:
    return {
        "transcript": "voice_fact",
        "ocr_text": "ocr_fact",
        "telemetry": "telemetry_fact",
        "weather": "weather_fact",
        "sleep_food_log": "sleep_food_fact",
        "manual_note": "manual_note",
        "system1_observation": "manual_note",
        "mission_context": "manual_note",
        "terrain": "weather_fact",
    }[kind]


def _artifact_confidence(kind: str) -> float:
    return {
        "weather": 0.96,
        "terrain": 0.94,
        "mission_context": 0.92,
        "system1_observation": 0.90,
        "telemetry": 0.90,
        "sleep_food_log": 0.88,
        "ocr_text": 0.86,
        "transcript": 0.88,
        "manual_note": 0.80,
    }[kind]


def _dedupe_observations(records: Sequence[ObservationRecord]) -> list[ObservationRecord]:
    deduped: dict[str, ObservationRecord] = {}
    for record in records:
        deduped[record.observation_id] = record
    return list(deduped.values())


def _fatigue_measurement(observation: ObservationRecord) -> float | None:
    sleep_hours = _number_from_content(observation.content, ("sleep_hours", "median_sleep_hours"))
    if sleep_hours is not None:
        return _clamp((7.0 - sleep_hours) / 7.0)
    hours_awake = _number_from_content(observation.content, ("hours_awake",))
    if hours_awake is not None:
        return _clamp(max(hours_awake - 12.0, 0.0) / 12.0)
    fatigue_index = _number_from_content(observation.content, ("fatigue_index", "fatigue_burden"))
    if fatigue_index is not None:
        return _clamp(fatigue_index)
    return None


def _weather_burden(environment: EnvironmentState | None) -> float:
    if environment is None:
        return 0.0
    burden = 0.0
    if environment.temperature_c is not None:
        if environment.temperature_c < 5 or environment.temperature_c > 32:
            burden += 0.35
    if environment.wind_speed is not None and environment.wind_speed > 15:
        burden += 0.20
    joined = " ".join(
        item.lower()
        for item in (
            environment.weather,
            environment.terrain,
            environment.visibility,
            environment.precipitation,
        )
        if item
    )
    if _contains_any(joined, ("rain", "snow", "low", "limited", "mud", "rough", "cold")):
        burden += 0.25
    return _clamp(burden)


def _positive_signal(text: str) -> float:
    return _hit_ratio(
        text,
        ("handled", "recognized", "clear", "confirmed", "anticipated", "updated", "disciplined"),
        divisor=5,
    )


def _hit_ratio(text: str, keywords: Sequence[str], *, divisor: int) -> float:
    return _clamp(sum(keyword in text for keyword in keywords) / divisor)


def _contains_any(text: str, keywords: Sequence[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _observation_text(observation: ObservationRecord) -> str:
    return _summary_text(observation.content)


def _content_summary(content: dict[str, Any]) -> str:
    if isinstance(content.get("summary"), str):
        return str(content["summary"])[:240]
    if isinstance(content.get("text"), str):
        return str(content["text"])[:240]
    return _summary_text(content)[:240]


def _summary_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(f"{key}: {_summary_text(item)}" for key, item in value.items())
    if isinstance(value, list):
        return " ".join(_summary_text(item) for item in value)
    if value is None:
        return ""
    return str(value)


def _number_from_content(content: dict[str, Any], keys: Sequence[str]) -> float | None:
    for key, value in content.items():
        if key in keys:
            parsed = _float_or_none(value)
            if parsed is not None:
                return parsed
        if isinstance(value, dict):
            parsed = _number_from_content(value, keys)
            if parsed is not None:
                return parsed
    return None


def _mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 3)


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, *, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _short_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{canonical_hash(value).removeprefix('sha256:')[:16]}"
