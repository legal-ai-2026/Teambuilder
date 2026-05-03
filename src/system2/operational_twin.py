from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from .audit import AuditLog, AuditSink
from .llm import JsonAgentClient
from .models import (
    AgentStageTrace,
    ArtifactInput,
    ArtifactRecord,
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
    build_operational_twin_update_event,
    canonical_hash,
)


_OBSERVATION_KINDS = {
    "voice_fact",
    "ocr_fact",
    "telemetry_fact",
    "weather_fact",
    "sleep_food_fact",
    "photo_fact",
    "manual_note",
}
_CRITIC_STATUSES = {"pass", "modify", "escalate", "reject"}


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


@dataclass
class OperationalTwinService:
    audit_log: AuditSink = field(default_factory=AuditLog)
    shared_data_sink: SharedDataSink = field(default_factory=InMemorySharedDataSink)
    repository: OperationalTwinRepository = field(default_factory=InMemoryOperationalTwinRepository)
    agent_provider: str = "deterministic"
    llm_client: JsonAgentClient | None = None
    llm_model: str = "deterministic"

    def run(self, request: OperationalTwinRequest) -> OperationalTwinResponse:
        artifact_inputs = _artifact_inputs(request)
        artifacts = [_artifact_record(item) for item in artifact_inputs]
        baseline_observations = _observation_records(request, artifacts)
        observations, agent_trace = _agentic_observations(
            request=request,
            artifacts=artifacts,
            baseline_observations=baseline_observations,
            llm_client=self.llm_client,
            agent_provider=self.agent_provider,
        )
        if not observations:
            raise ValueError("operational twin runs require at least one artifact or observation")

        twin_run_id = f"twin-{uuid4()}"
        evidence_bundle_id = f"eb-{uuid4()}"
        environment_state = _environment_state(request)
        baseline_state_vector = _estimate_state_vector(observations, environment_state)
        baseline_uncertainty = _estimate_uncertainty(observations, baseline_state_vector)
        state_vector, uncertainty, state_trace = _agentic_state_estimate(
            request=request,
            observations=observations,
            environment_state=environment_state,
            baseline_state_vector=baseline_state_vector,
            baseline_uncertainty=baseline_uncertainty,
            llm_client=self.llm_client,
            agent_provider=self.agent_provider,
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
        scenario_options, scenario_trace = _agentic_scenario_options(
            request=request,
            observations=observations,
            state_estimate=state_estimate,
            evidence_bundle=evidence_bundle,
            baseline_options=baseline_options,
            llm_client=self.llm_client,
            agent_provider=self.agent_provider,
        )
        agent_trace.extend(scenario_trace)
        scenario_options, critic_trace = _agentic_critic_review(
            request=request,
            observations=observations,
            state_estimate=state_estimate,
            evidence_bundle=evidence_bundle,
            options=scenario_options,
            llm_client=self.llm_client,
            agent_provider=self.agent_provider,
        )
        agent_trace.extend(critic_trace)
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
        lesson = _lesson_from_decision(run, option, decision)
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


def _agentic_observations(
    *,
    request: OperationalTwinRequest,
    artifacts: Sequence[ArtifactRecord],
    baseline_observations: Sequence[ObservationRecord],
    llm_client: JsonAgentClient | None,
    agent_provider: str,
) -> tuple[list[ObservationRecord], list[AgentStageTrace]]:
    if not _llm_enabled(llm_client, agent_provider):
        return list(baseline_observations), [
            _trace(
                stage="perception",
                provider="deterministic",
                model=MODEL_VERSIONS["operational_twin_perception"],
                status="completed",
                summary="Normalized artifacts and explicit observations with deterministic perception.",
            )
        ]

    started_at = datetime.now(UTC)
    try:
        payload = llm_client.complete_json(
            stage="perception",
            system=_perception_system_prompt(),
            user=json.dumps(
                {
                    "mission_id": request.mission_id,
                    "team_id": request.team_id,
                    "mode": request.mode,
                    "artifacts": [item.model_dump(mode="json") for item in artifacts],
                    "baseline_observations": [
                        item.model_dump(mode="json") for item in baseline_observations
                    ],
                },
                sort_keys=True,
                default=str,
            ),
        )
        generated = _observations_from_agent_payload(request, artifacts, payload)
        observations = _dedupe_observations([*baseline_observations, *generated])
        if not generated:
            return observations, [
                _trace(
                    stage="perception",
                    provider=llm_client.provider,
                    model=llm_client.model,
                    status="fallback",
                    summary="OpenAI perception returned no usable observations; deterministic observations retained.",
                    started_at=started_at,
                )
            ]
        return observations, [
            _trace(
                stage="perception",
                provider=llm_client.provider,
                model=llm_client.model,
                status="completed",
                summary=f"OpenAI perception added {len(generated)} source-linked observations.",
                started_at=started_at,
            )
        ]
    except Exception as exc:
        return list(baseline_observations), [
            _trace(
                stage="perception",
                provider=_provider_name(llm_client, agent_provider),
                model=_model_name(llm_client),
                status="fallback",
                summary="Agentic perception failed; deterministic observations retained.",
                error=str(exc),
                started_at=started_at,
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
) -> tuple[OperationalStateVector, StateUncertainty, list[AgentStageTrace]]:
    if not _llm_enabled(llm_client, agent_provider):
        return baseline_state_vector, baseline_uncertainty, [
            _trace(
                stage="state",
                provider="deterministic",
                model=MODEL_VERSIONS["operational_twin_state_estimator"],
                status="completed",
                summary="Estimated latent state with deterministic evidence scoring.",
            )
        ]

    started_at = datetime.now(UTC)
    try:
        payload = llm_client.complete_json(
            stage="state",
            system=_state_system_prompt(),
            user=json.dumps(
                {
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
                },
                sort_keys=True,
                default=str,
            ),
        )
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
            )
        ]
    except Exception as exc:
        return baseline_state_vector, baseline_uncertainty, [
            _trace(
                stage="state",
                provider=_provider_name(llm_client, agent_provider),
                model=_model_name(llm_client),
                status="fallback",
                summary="Agentic state estimation failed; deterministic state retained.",
                error=str(exc),
                started_at=started_at,
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
) -> tuple[list[ScenarioOption], list[AgentStageTrace]]:
    if not _llm_enabled(llm_client, agent_provider):
        return list(baseline_options), [
            _trace(
                stage="scenario",
                provider="deterministic",
                model=MODEL_VERSIONS["operational_twin_scenario_director"],
                status="completed",
                summary="Drafted scenario options with deterministic templates.",
            )
        ]

    started_at = datetime.now(UTC)
    try:
        payload = llm_client.complete_json(
            stage="scenario",
            system=_scenario_system_prompt(),
            user=json.dumps(
                {
                    "mission_id": request.mission_id,
                    "team_id": request.team_id,
                    "mode": request.mode,
                    "training_objective": request.training_objective,
                    "state_estimate": state_estimate.model_dump(mode="json"),
                    "evidence_bundle": evidence_bundle.model_dump(mode="json"),
                    "observations": [item.model_dump(mode="json") for item in observations],
                    "baseline_options": [item.model_dump(mode="json") for item in baseline_options],
                },
                sort_keys=True,
                default=str,
            ),
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
            )
        ]
    except Exception as exc:
        return list(baseline_options), [
            _trace(
                stage="scenario",
                provider=_provider_name(llm_client, agent_provider),
                model=_model_name(llm_client),
                status="fallback",
                summary="Agentic scenario direction failed; deterministic options retained.",
                error=str(exc),
                started_at=started_at,
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
) -> tuple[list[ScenarioOption], list[AgentStageTrace]]:
    if not _llm_enabled(llm_client, agent_provider):
        return list(options), [
            _trace(
                stage="critic",
                provider="deterministic",
                model=MODEL_VERSIONS["operational_twin_critic"],
                status="completed",
                summary="Applied deterministic safety, evidence, and confidence critic checks.",
            )
        ]

    started_at = datetime.now(UTC)
    try:
        payload = llm_client.complete_json(
            stage="critic",
            system=_critic_system_prompt(),
            user=json.dumps(
                {
                    "mission_id": request.mission_id,
                    "team_id": request.team_id,
                    "mode": request.mode,
                    "state_estimate": state_estimate.model_dump(mode="json"),
                    "evidence_bundle": evidence_bundle.model_dump(mode="json"),
                    "observations": [item.model_dump(mode="json") for item in observations],
                    "options": [item.model_dump(mode="json") for item in options],
                },
                sort_keys=True,
                default=str,
            ),
        )
        reviewed = _apply_agent_critic_reviews(options, payload)
        return reviewed, [
            _trace(
                stage="critic",
                provider=llm_client.provider,
                model=llm_client.model,
                status="completed",
                summary="OpenAI critic reviewed grounding, risk, option diversity, and human-approval gates.",
                started_at=started_at,
            )
        ]
    except Exception as exc:
        return list(options), [
            _trace(
                stage="critic",
                provider=_provider_name(llm_client, agent_provider),
                model=_model_name(llm_client),
                status="fallback",
                summary="Agentic critic failed; deterministic critic statuses retained.",
                error=str(exc),
                started_at=started_at,
            )
        ]


def _artifact_inputs(request: OperationalTwinRequest) -> list[ArtifactInput]:
    inputs = list(request.artifacts)
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
    return _dedupe_observations(records)


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
        content: dict[str, Any] = {
            "summary": text or _summary_text(artifact.metadata),
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
    evidence_threshold_ok = len(observations) >= 2
    safety_ok = not (
        state_vector.fatigue_burden > 0.88 and state_vector.mission_tempo_risk > 0.72
    )
    return EvidenceBundlePolicyChecks(
        classification_ok=classification_ok,
        evidence_threshold_ok=evidence_threshold_ok,
        safety_ok=safety_ok,
        human_approval_required=request.require_human_approval,
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
    templates = _option_templates(target, request.mode)
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


def _trace(
    *,
    stage: str,
    provider: str,
    model: str,
    status: str,
    summary: str,
    error: str | None = None,
    started_at: datetime | None = None,
) -> AgentStageTrace:
    return AgentStageTrace(
        stage=stage,
        provider=provider,
        model=model,
        status=status,  # type: ignore[arg-type]
        summary=summary,
        error=error,
        started_at=started_at or datetime.now(UTC),
        completed_at=datetime.now(UTC),
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
        "fitness, protected attributes, or personnel judgments. Return JSON with an "
        "'observations' array. Each item must include kind, summary or content, "
        "source_artifact_ids, confidence, and optional subject_ref."
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
        "Return exactly three distinct options in JSON under scenario_options. In "
        "training mode, options are scenario injects; in mission mode, options are "
        "advisory rehearsal variants or COAs. Each option must include title, narrative, "
        "predicted_effect.target_state_change, risk_score, and confidence. Do not approve "
        "anything and do not bypass human review."
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


def _critic_status(
    *,
    evidence_bundle: EvidenceBundle,
    risk_score: float,
    confidence: float,
    state_vector: OperationalStateVector,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if not evidence_bundle.policy_checks.classification_ok:
        reasons.append("Classification or control marking check failed.")
        return "reject", reasons
    if not evidence_bundle.policy_checks.safety_ok:
        reasons.append("Safety envelope is exceeded by fatigue and tempo risk.")
        return "reject", reasons

    status = "pass"
    if not evidence_bundle.policy_checks.evidence_threshold_ok:
        status = "escalate"
        reasons.append("Fewer than two observations support the recommendation.")
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


def _observation_kind_for_artifact(kind: str) -> TwinObservationKind:
    return {
        "audio": "voice_fact",
        "transcript": "voice_fact",
        "document_image": "ocr_fact",
        "ocr_text": "ocr_fact",
        "telemetry": "telemetry_fact",
        "weather": "weather_fact",
        "sleep_food_log": "sleep_food_fact",
        "photo": "photo_fact",
        "manual_note": "manual_note",
    }[kind]


def _artifact_confidence(kind: str) -> float:
    return {
        "weather": 0.96,
        "telemetry": 0.90,
        "sleep_food_log": 0.88,
        "ocr_text": 0.86,
        "document_image": 0.78,
        "audio": 0.82,
        "transcript": 0.88,
        "photo": 0.72,
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
