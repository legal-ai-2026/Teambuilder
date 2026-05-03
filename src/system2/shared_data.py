from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from .audit import redact_payload
from .models import (
    AgentRun,
    CognitiveAdaptationRequest,
    CognitiveAdaptationResponse,
    ContextChunkInput,
    GraphFactInput,
    OperationalTwinOutcomeRequest,
    OperationalTwinOutcomeResponse,
    OperationalTwinRequest,
    OperationalTwinResponse,
    RoleRequirement,
    RosterRecommendation,
    ScenarioApprovalRequest,
    ScenarioApprovalResponse,
    ScenarioOptionDecisionRequest,
    ScenarioOptionDecisionResponse,
    ScoreRequest,
    Soldier,
    SourceReference,
)


SYSTEM2_APP_ID = "system2"

SHARED_DATA_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS entity_update_events (
    event_id text PRIMARY KEY,
    entity_type text NOT NULL,
    entity_id text NOT NULL,
    source_app text NOT NULL,
    source_record_id text NOT NULL,
    operation text NOT NULL,
    event_payload jsonb NOT NULL,
    previous_source_hash text,
    new_source_hash text,
    observed_at timestamptz,
    recorded_at timestamptz NOT NULL,
    actor_id text NOT NULL,
    reason text NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entity_update_events_entity
    ON entity_update_events (entity_type, entity_id);

CREATE INDEX IF NOT EXISTS idx_entity_update_events_source_app
    ON entity_update_events (source_app);

CREATE INDEX IF NOT EXISTS idx_entity_update_events_recorded_at
    ON entity_update_events (recorded_at);

CREATE TABLE IF NOT EXISTS decision_snapshots (
    snapshot_id text PRIMARY KEY,
    run_id text NOT NULL,
    mission_id text NOT NULL,
    request_hash text NOT NULL,
    input_source_hashes jsonb NOT NULL,
    output_hash text NOT NULL,
    fairness_hash text NOT NULL,
    created_at timestamptz NOT NULL,
    payload jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decision_snapshots_run_id
    ON decision_snapshots (run_id);

CREATE INDEX IF NOT EXISTS idx_decision_snapshots_mission_id
    ON decision_snapshots (mission_id);
"""


ConnectionFactory = Callable[[], Any]


class SharedDataSink(Protocol):
    def record_decision_snapshot(self, snapshot: dict[str, Any]) -> None:
        ...

    def append_update_event(self, event: dict[str, Any]) -> None:
        ...


@dataclass
class InMemorySharedDataSink:
    decision_snapshots: list[dict[str, Any]] = field(default_factory=list)
    update_events: list[dict[str, Any]] = field(default_factory=list)

    def record_decision_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.decision_snapshots.append(snapshot)

    def append_update_event(self, event: dict[str, Any]) -> None:
        self.update_events.append(event)


class PostgresSharedDataSink:
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
                cursor.execute(SHARED_DATA_SCHEMA_SQL)
            connection.commit()

    def record_decision_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO decision_snapshots (
                        snapshot_id, run_id, mission_id, request_hash,
                        input_source_hashes, output_hash, fairness_hash,
                        created_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (snapshot_id) DO NOTHING
                    """,
                    (
                        snapshot["snapshot_id"],
                        snapshot["run_id"],
                        snapshot["mission_id"],
                        snapshot["request_hash"],
                        json.dumps(snapshot["input_source_hashes"], sort_keys=True),
                        snapshot["output_hash"],
                        snapshot["fairness_hash"],
                        snapshot["created_at"],
                        json.dumps(snapshot, sort_keys=True, default=str),
                    ),
                )
            connection.commit()

    def append_update_event(self, event: dict[str, Any]) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO entity_update_events (
                        event_id, entity_type, entity_id, source_app,
                        source_record_id, operation, event_payload,
                        previous_source_hash, new_source_hash, observed_at,
                        recorded_at, actor_id, reason
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        event["event_id"],
                        event["entity_type"],
                        event["entity_id"],
                        event["source_app"],
                        event["source_record_id"],
                        event["operation"],
                        json.dumps(event["event_payload"], sort_keys=True, default=str),
                        event.get("previous_source_hash"),
                        event.get("new_source_hash"),
                        event.get("observed_at"),
                        event["recorded_at"],
                        event["actor_id"],
                        event["reason"],
                    ),
                )
            connection.commit()

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()

        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "Shared data persistence requires the 'infra' optional dependencies. "
                "Install with: pip install -e '.[infra]'"
            ) from exc

        return psycopg.connect(self.database_url)


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def score_request_source_refs(
    request: ScoreRequest,
    soldiers: Sequence[Soldier],
    roles: Sequence[RoleRequirement],
    *,
    candidate_pool_resolved: bool = False,
    additional_refs: Sequence[SourceReference] = (),
) -> list[SourceReference]:
    refs = [
        SourceReference(
            ref=f"postgres://missions_current/{request.mission_id}",
            role="mission",
            source_hash=canonical_hash({"mission_id": request.mission_id}),
        ),
        SourceReference(
            ref=f"falkordb://system2/Mission/{request.mission_id}",
            role="mission_graph",
            source_hash=canonical_hash({"graph": "system2", "mission_id": request.mission_id}),
        ),
    ]
    if request.candidate_pool_id:
        refs.append(
            SourceReference(
                ref=f"postgres://candidate_pools_current/{request.candidate_pool_id}",
                role="candidate_pool_requested",
                source_hash=canonical_hash(
                    {"candidate_pool_id": request.candidate_pool_id, "mission_id": request.mission_id}
                ),
            )
        )
    if request.candidates or candidate_pool_resolved:
        refs.extend(
            SourceReference(
                ref=f"postgres://soldiers_current/{soldier.soldier_id}",
                role="candidate_profile",
                source_hash=canonical_hash(redact_payload(soldier.model_dump(mode="json"))),
                metadata={"soldier_id": soldier.soldier_id},
            )
            for soldier in soldiers
        )
    else:
        refs.append(
            SourceReference(
                ref=f"synthetic://system2/generated-candidates/{request.mission_id}/{request.seed}",
                role="candidate_pool_local_fallback",
                source_hash=canonical_hash(
                    {
                        "mission_id": request.mission_id,
                        "candidate_count": request.candidate_count,
                        "seed": request.seed,
                    }
                ),
                metadata={"operational_source": False},
            )
        )

    refs.extend(
        SourceReference(
            ref=f"postgres://role_slots_current/{request.mission_id}/{role.slot_id}",
            role="role_slot",
            source_hash=canonical_hash(role.model_dump(mode="json")),
            metadata={"slot_id": role.slot_id, "role": role.role},
        )
        for role in roles
    )
    refs.extend(additional_refs)
    return dedupe_source_refs(refs)


def attach_source_refs(
    recommendation: RosterRecommendation,
    refs: Sequence[SourceReference],
) -> RosterRecommendation:
    merged_refs = dedupe_source_refs([*recommendation.trace.source_refs, *refs])
    trace = recommendation.trace.model_copy(
        update={
            "source_refs": merged_refs,
            "input_source_hashes": input_source_hashes(merged_refs),
        }
    )
    return recommendation.model_copy(update={"trace": trace})


def input_source_hashes(refs: Sequence[SourceReference]) -> dict[str, str]:
    return {
        ref.ref: ref.source_hash
        for ref in refs
        if ref.source_hash is not None
    }


def dedupe_source_refs(refs: Sequence[SourceReference]) -> list[SourceReference]:
    by_ref: dict[str, SourceReference] = {}
    for ref in refs:
        by_ref[ref.ref] = ref
    return list(by_ref.values())


def build_decision_snapshot(run: AgentRun) -> dict[str, Any]:
    if run.recommendation is None:
        raise ValueError("decision snapshots require a recommendation")
    recommendation_payload = run.recommendation.model_dump(mode="json")
    return {
        "snapshot_id": f"snapshot-{uuid4()}",
        "run_id": run.run_id,
        "mission_id": run.request.score_request.mission_id,
        "request_hash": canonical_hash(run.request.model_dump(mode="json")),
        "input_source_hashes": run.recommendation.trace.input_source_hashes,
        "output_hash": canonical_hash(recommendation_payload),
        "fairness_hash": canonical_hash(run.recommendation.fairness_audit.model_dump(mode="json")),
        "created_at": datetime.now(UTC),
        "payload": {
            "run_id": run.run_id,
            "status": run.status.value,
            "recommendation": recommendation_payload,
            "decision_quality": run.recommendation.decision_quality.model_dump(mode="json"),
            "utility_estimate": run.recommendation.utility_estimate.model_dump(mode="json"),
            "reliance_guidance": run.recommendation.reliance_guidance.model_dump(mode="json"),
            "source_refs": [ref.model_dump(mode="json") for ref in run.recommendation.trace.source_refs],
        },
    }


def build_direct_score_decision_snapshot(
    request: ScoreRequest,
    recommendation: RosterRecommendation,
) -> dict[str, Any]:
    run_id = f"direct-score-{uuid4()}"
    recommendation_payload = recommendation.model_dump(mode="json")
    return {
        "snapshot_id": f"snapshot-{uuid4()}",
        "run_id": run_id,
        "mission_id": request.mission_id,
        "request_hash": canonical_hash(request.model_dump(mode="json")),
        "input_source_hashes": recommendation.trace.input_source_hashes,
        "output_hash": canonical_hash(recommendation_payload),
        "fairness_hash": canonical_hash(recommendation.fairness_audit.model_dump(mode="json")),
        "created_at": datetime.now(UTC),
        "payload": {
            "run_id": run_id,
            "status": "returned",
            "request": request.model_dump(mode="json"),
            "recommendation": recommendation_payload,
            "decision_quality": recommendation.decision_quality.model_dump(mode="json"),
            "utility_estimate": recommendation.utility_estimate.model_dump(mode="json"),
            "reliance_guidance": recommendation.reliance_guidance.model_dump(mode="json"),
            "source_refs": [ref.model_dump(mode="json") for ref in recommendation.trace.source_refs],
        },
    }


def build_approval_update_event(run: AgentRun) -> dict[str, Any]:
    if run.approval is None or run.recommendation is None:
        raise ValueError("approval update events require approval and recommendation")

    selected = [
        {"slot_id": item.slot_id, "role": item.role, "soldier_id": item.soldier_id}
        for item in run.recommendation.roster
    ]
    previous_payload = {
        "run_id": run.run_id,
        "status": "awaiting_approval",
        "recommendation": run.recommendation.model_dump(mode="json"),
    }
    new_payload = {
        **previous_payload,
        "status": run.status.value,
        "approval": run.approval.model_dump(mode="json"),
    }
    operation = "approve" if run.approval.decision.value == "approved" else "reject"
    return {
        "event_id": f"update-{uuid4()}",
        "entity_type": "recommendation",
        "entity_id": run.run_id,
        "source_app": SYSTEM2_APP_ID,
        "source_record_id": run.run_id,
        "operation": operation,
        "event_payload": {
            "run_id": run.run_id,
            "mission_id": run.request.score_request.mission_id,
            "decision": run.approval.decision.value,
            "approver_id": run.approval.approver_id,
            "selected_assignments": selected,
            "selected_soldier_ids": [item["soldier_id"] for item in selected],
            "slot_ids": [item["slot_id"] for item in selected],
            "decision_quality": run.recommendation.decision_quality.model_dump(mode="json"),
            "utility_estimate": run.recommendation.utility_estimate.model_dump(mode="json"),
            "reliance_guidance": run.recommendation.reliance_guidance.model_dump(mode="json"),
            "source_refs": [ref.model_dump(mode="json") for ref in run.recommendation.trace.source_refs],
        },
        "previous_source_hash": canonical_hash(previous_payload),
        "new_source_hash": canonical_hash(new_payload),
        "observed_at": run.approval.decided_at,
        "recorded_at": datetime.now(UTC),
        "actor_id": run.approval.approver_id,
        "reason": run.approval.rationale,
    }


def build_kill_switch_update_event(
    *,
    disabled: bool,
    actor_id: str = "system2-admin",
    reason: str | None = None,
) -> dict[str, Any]:
    previous_payload = {"disabled": not disabled}
    new_payload = {"disabled": disabled}
    operation = "disable" if disabled else "enable"
    return {
        "event_id": f"update-{uuid4()}",
        "entity_type": "system_control",
        "entity_id": "system2.kill_switch",
        "source_app": SYSTEM2_APP_ID,
        "source_record_id": "system2.kill_switch",
        "operation": operation,
        "event_payload": {
            "control": "kill_switch",
            "disabled": disabled,
            "service": SYSTEM2_APP_ID,
        },
        "previous_source_hash": canonical_hash(previous_payload),
        "new_source_hash": canonical_hash(new_payload),
        "observed_at": datetime.now(UTC),
        "recorded_at": datetime.now(UTC),
        "actor_id": actor_id,
        "reason": reason or f"System 2 kill switch {operation}d.",
    }


def build_context_update_events(chunks: Sequence[ContextChunkInput]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for chunk in chunks:
        payload = {
            "chunk_id": chunk.chunk_id,
            "source": chunk.source,
            "title": chunk.title,
            "content_hash": canonical_hash(chunk.content),
            "metadata": chunk.metadata,
            "has_embedding": chunk.embedding is not None,
        }
        events.append(
            {
                "event_id": f"update-{uuid4()}",
                "entity_type": str(chunk.metadata.get("entity_type", "policy")),
                "entity_id": chunk.chunk_id,
                "source_app": SYSTEM2_APP_ID,
                "source_record_id": chunk.chunk_id,
                "operation": "observe",
                "event_payload": payload,
                "previous_source_hash": None,
                "new_source_hash": canonical_hash(payload),
                "observed_at": None,
                "recorded_at": datetime.now(UTC),
                "actor_id": str(chunk.metadata.get("actor_id", "system2-context-ingest")),
                "reason": str(chunk.metadata.get("reason", "Context chunk ingested for retrieval.")),
            }
        )
    return events


def build_graph_update_events(facts: Sequence[GraphFactInput]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for fact in facts:
        payload = {
            "subject": fact.subject,
            "predicate": fact.predicate,
            "object": fact.object,
            "metadata": fact.metadata,
        }
        fact_id = str(fact.metadata.get("fact_id") or _short_id("fact", payload))
        events.append(
            {
                "event_id": f"update-{uuid4()}",
                "entity_type": "graph_fact",
                "entity_id": fact_id,
                "source_app": SYSTEM2_APP_ID,
                "source_record_id": fact_id,
                "operation": "observe",
                "event_payload": payload,
                "previous_source_hash": None,
                "new_source_hash": canonical_hash(payload),
                "observed_at": None,
                "recorded_at": datetime.now(UTC),
                "actor_id": str(fact.metadata.get("actor_id", "system2-graph-ingest")),
                "reason": str(fact.metadata.get("reason", "Derived graph fact ingested for relationship lookup.")),
            }
        )
    return events


def build_cognitive_adaptation_update_event(
    request: CognitiveAdaptationRequest,
    response: CognitiveAdaptationResponse,
) -> dict[str, Any]:
    payload = {
        "adaptation_id": response.adaptation_id,
        "mission_id": response.mission_id,
        "team_id": response.team_id,
        "instructor_id": request.instructor_id,
        "primary_development_dimension": response.state.primary_development_dimension,
        "state_snapshot_id": response.state.snapshot_id,
        "recommendation_ids": [item.recommendation_id for item in response.recommendations],
        "blocked_recommendation_ids": [
            item.recommendation_id for item in response.blocked_recommendations
        ],
        "decision_quality": response.decision_quality.model_dump(mode="json"),
        "utility_estimate": response.utility_estimate.model_dump(mode="json"),
        "reliance_guidance": response.reliance_guidance.model_dump(mode="json"),
        "source_refs": [ref.model_dump(mode="json") for ref in response.trace.source_refs],
    }
    return {
        "event_id": f"update-{uuid4()}",
        "entity_type": "scenario_adaptation",
        "entity_id": response.adaptation_id,
        "source_app": SYSTEM2_APP_ID,
        "source_record_id": response.adaptation_id,
        "operation": "recommend",
        "event_payload": payload,
        "previous_source_hash": None,
        "new_source_hash": canonical_hash(response.model_dump(mode="json")),
        "observed_at": response.state.generated_at,
        "recorded_at": datetime.now(UTC),
        "actor_id": request.instructor_id,
        "reason": "Cognitive Mission Adaptation Engine generated instructor-approved scenario options.",
    }


def build_scenario_approval_update_event(
    adaptation: CognitiveAdaptationResponse,
    request: ScenarioApprovalRequest,
    approval: ScenarioApprovalResponse,
) -> dict[str, Any]:
    payload = {
        "adaptation_id": adaptation.adaptation_id,
        "mission_id": adaptation.mission_id,
        "team_id": adaptation.team_id,
        "recommendation_id": request.recommendation_id,
        "decision": request.decision.value,
        "approved_inject": (
            approval.approved_inject.model_dump(mode="json")
            if approval.approved_inject is not None
            else None
        ),
        "decision_quality": adaptation.decision_quality.model_dump(mode="json"),
        "utility_estimate": adaptation.utility_estimate.model_dump(mode="json"),
        "reliance_guidance": adaptation.reliance_guidance.model_dump(mode="json"),
        "source_refs": [ref.model_dump(mode="json") for ref in adaptation.trace.source_refs],
    }
    return {
        "event_id": f"update-{uuid4()}",
        "entity_type": "scenario_inject",
        "entity_id": request.recommendation_id,
        "source_app": SYSTEM2_APP_ID,
        "source_record_id": adaptation.adaptation_id,
        "operation": "approve" if request.decision.value == "approved" else "reject",
        "event_payload": payload,
        "previous_source_hash": canonical_hash(adaptation.model_dump(mode="json")),
        "new_source_hash": canonical_hash(approval.model_dump(mode="json")),
        "observed_at": approval.decided_at,
        "recorded_at": datetime.now(UTC),
        "actor_id": request.approver_id,
        "reason": request.rationale,
    }


def build_operational_twin_update_event(
    request: OperationalTwinRequest,
    response: OperationalTwinResponse,
) -> dict[str, Any]:
    payload = {
        "twin_run_id": response.twin_run_id,
        "mission_id": response.mission_id,
        "team_id": response.team_id,
        "mode": response.mode,
        "operator_id": request.operator_id,
        "artifact_ids": [item.artifact_id for item in response.artifacts],
        "observation_ids": [item.observation_id for item in response.observations],
        "state_estimate_id": response.state_estimate.state_estimate_id,
        "evidence_bundle_id": response.evidence_bundle.bundle_id,
        "scenario_option_ids": [
            item.scenario_option_id for item in response.scenario_options
        ],
        "policy_checks": response.evidence_bundle.policy_checks.model_dump(mode="json"),
        "decision_quality": response.decision_quality.model_dump(mode="json"),
        "utility_estimate": response.utility_estimate.model_dump(mode="json"),
        "reliance_guidance": response.reliance_guidance.model_dump(mode="json"),
    }
    return {
        "event_id": f"update-{uuid4()}",
        "entity_type": "operational_twin",
        "entity_id": response.twin_run_id,
        "source_app": SYSTEM2_APP_ID,
        "source_record_id": response.twin_run_id,
        "operation": "recommend",
        "event_payload": payload,
        "previous_source_hash": None,
        "new_source_hash": canonical_hash(response.model_dump(mode="json")),
        "observed_at": response.created_at_utc,
        "recorded_at": datetime.now(UTC),
        "actor_id": request.operator_id,
        "reason": "Operational twin generated draft options with evidence, state, and critic checks.",
    }


def build_operational_twin_decision_update_event(
    run: OperationalTwinResponse,
    request: ScenarioOptionDecisionRequest,
    response: ScenarioOptionDecisionResponse,
) -> dict[str, Any]:
    payload = {
        "twin_run_id": run.twin_run_id,
        "mission_id": run.mission_id,
        "team_id": run.team_id,
        "scenario_option_id": response.scenario_option_id,
        "decision": request.decision,
        "actor_id": request.actor_id,
        "decision_id": response.decision.decision_id,
        "lesson_id": (
            response.lesson_learned.lesson_id
            if response.lesson_learned is not None
            else None
        ),
        "evidence_bundle_id": response.decision.evidence_bundle_id,
        "decision_quality": run.decision_quality.model_dump(mode="json"),
        "utility_estimate": run.utility_estimate.model_dump(mode="json"),
        "reliance_guidance": run.reliance_guidance.model_dump(mode="json"),
    }
    return {
        "event_id": f"update-{uuid4()}",
        "entity_type": "operational_twin_option",
        "entity_id": response.scenario_option_id,
        "source_app": SYSTEM2_APP_ID,
        "source_record_id": run.twin_run_id,
        "operation": request.decision,
        "event_payload": payload,
        "previous_source_hash": None,
        "new_source_hash": canonical_hash(response.model_dump(mode="json")),
        "observed_at": response.decided_at_utc,
        "recorded_at": datetime.now(UTC),
        "actor_id": request.actor_id,
        "reason": request.comment,
    }


def build_operational_twin_outcome_update_event(
    run: OperationalTwinResponse,
    request: OperationalTwinOutcomeRequest,
    response: OperationalTwinOutcomeResponse,
) -> dict[str, Any]:
    payload = {
        "twin_run_id": run.twin_run_id,
        "mission_id": run.mission_id,
        "team_id": run.team_id,
        "selected_option_id": response.selected_option_id,
        "outcome_id": response.outcome.outcome_id,
        "lesson_id": response.lesson_learned.lesson_id,
        "instructor_rating": request.instructor_rating,
        "safety_incident": request.safety_incident,
        "targeted_state_improvement_estimate": request.targeted_state_improvement_estimate,
        "evidence_bundle_id": response.outcome.evidence_bundle_id,
        "decision_quality": run.decision_quality.model_dump(mode="json"),
        "utility_estimate": run.utility_estimate.model_dump(mode="json"),
        "reliance_guidance": run.reliance_guidance.model_dump(mode="json"),
    }
    return {
        "event_id": f"update-{uuid4()}",
        "entity_type": "operational_twin_outcome",
        "entity_id": response.outcome.outcome_id,
        "source_app": SYSTEM2_APP_ID,
        "source_record_id": run.twin_run_id,
        "operation": "observe_outcome",
        "event_payload": payload,
        "previous_source_hash": None,
        "new_source_hash": canonical_hash(response.model_dump(mode="json")),
        "observed_at": response.recorded_at_utc,
        "recorded_at": datetime.now(UTC),
        "actor_id": request.actor_id,
        "reason": request.aar_notes,
    }


def _short_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{canonical_hash(value).removeprefix('sha256:')[:16]}"
