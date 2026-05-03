from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import RoleRequirement, ScoreRequest, Soldier, SourceReference
from .shared_data import canonical_hash, dedupe_source_refs


CANDIDATE_POOL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS candidate_pools_current (
    pool_id text PRIMARY KEY,
    mission_id text NOT NULL,
    candidate_ids jsonb NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_candidate_pools_current_mission
    ON candidate_pools_current (mission_id);

CREATE TABLE IF NOT EXISTS soldiers_current (
    soldier_id text PRIMARY KEY,
    unit_id text,
    mos text,
    profile_json jsonb NOT NULL,
    protected_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS role_slots_current (
    mission_id text NOT NULL,
    slot_id text NOT NULL,
    role text NOT NULL,
    required_mos text,
    min_acft integer NOT NULL DEFAULT 450,
    source_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (mission_id, slot_id)
);

CREATE INDEX IF NOT EXISTS idx_role_slots_current_mission
    ON role_slots_current (mission_id);

CREATE TABLE IF NOT EXISTS training_observations_current (
    soldier_id text PRIMARY KEY,
    observation_json jsonb NOT NULL,
    source_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS deployment_outcomes_current (
    soldier_id text NOT NULL,
    mission_id text NOT NULL,
    outcome_json jsonb NOT NULL,
    source_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (soldier_id, mission_id)
);

CREATE INDEX IF NOT EXISTS idx_deployment_outcomes_current_soldier
    ON deployment_outcomes_current (soldier_id);
"""


ConnectionFactory = Callable[[], Any]


@dataclass(frozen=True)
class ResolvedCandidatePool:
    candidate_pool_id: str
    mission_id: str
    candidates: list[Soldier]
    roles: list[RoleRequirement]
    source_refs: list[SourceReference] = field(default_factory=list)
    training_projections: dict[str, dict[str, Any]] = field(default_factory=dict)
    deployment_outcome_projections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


class CandidatePoolResolver(Protocol):
    requires_resolution: bool

    def resolve(self, request: ScoreRequest) -> ResolvedCandidatePool | None:
        ...


@dataclass
class InMemoryCandidatePoolResolver:
    pools: dict[tuple[str, str], ResolvedCandidatePool] = field(default_factory=dict)
    requires_resolution: bool = False

    def add_pool(
        self,
        candidate_pool_id: str,
        mission_id: str,
        candidates: Sequence[Soldier],
        roles: Sequence[RoleRequirement],
        source_refs: Sequence[SourceReference] = (),
        training_projections: Mapping[str, Mapping[str, Any]] | None = None,
        deployment_outcome_projections: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    ) -> None:
        self.pools[(candidate_pool_id, mission_id)] = ResolvedCandidatePool(
            candidate_pool_id=candidate_pool_id,
            mission_id=mission_id,
            candidates=list(candidates),
            roles=list(roles),
            source_refs=list(source_refs),
            training_projections={
                str(soldier_id): dict(projection)
                for soldier_id, projection in (training_projections or {}).items()
            },
            deployment_outcome_projections={
                str(soldier_id): [dict(projection) for projection in projections]
                for soldier_id, projections in (deployment_outcome_projections or {}).items()
            },
        )

    def resolve(self, request: ScoreRequest) -> ResolvedCandidatePool | None:
        if request.candidate_pool_id is None:
            return None
        pool = self.pools.get((request.candidate_pool_id, request.mission_id))
        if pool is None:
            return None
        return _enrich_resolved_pool(pool, request.mission_id)


class PostgresCandidatePoolResolver:
    requires_resolution = True

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
                cursor.execute(CANDIDATE_POOL_SCHEMA_SQL)
            connection.commit()

    def resolve(self, request: ScoreRequest) -> ResolvedCandidatePool | None:
        if request.candidate_pool_id is None:
            return None

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT candidate_ids, payload, source_hash
                    FROM candidate_pools_current
                    WHERE pool_id = %s AND mission_id = %s
                    """,
                    (request.candidate_pool_id, request.mission_id),
                )
                pool_row = cursor.fetchone()
                if pool_row is None:
                    return None

                candidate_ids = _json_value(pool_row[0])
                if not isinstance(candidate_ids, list) or not candidate_ids:
                    raise ValueError("candidate pool has no candidate_ids")
                candidate_id_values = [str(candidate_id) for candidate_id in candidate_ids]

                cursor.execute(
                    """
                    SELECT soldier_id, unit_id, mos, profile_json, protected_json, source_hash
                    FROM soldiers_current
                    WHERE soldier_id = ANY(%s)
                    """,
                    (candidate_id_values,),
                )
                soldier_rows = cursor.fetchall()

                cursor.execute(
                    """
                    SELECT slot_id, role, required_mos, min_acft, source_hash
                    FROM role_slots_current
                    WHERE mission_id = %s
                    ORDER BY slot_id ASC
                    """,
                    (request.mission_id,),
                )
                role_rows = cursor.fetchall()

                cursor.execute(
                    """
                    SELECT soldier_id, observation_json, source_hash
                    FROM training_observations_current
                    WHERE soldier_id = ANY(%s)
                    """,
                    (candidate_id_values,),
                )
                training_rows = cursor.fetchall()

                cursor.execute(
                    """
                    SELECT soldier_id, mission_id, outcome_json, source_hash
                    FROM deployment_outcomes_current
                    WHERE soldier_id = ANY(%s)
                    ORDER BY soldier_id ASC, updated_at ASC
                    """,
                    (candidate_id_values,),
                )
                outcome_rows = cursor.fetchall()

        soldiers_by_id = {
            str(row[0]): _soldier_from_row(row)
            for row in soldier_rows
        }
        missing_ids = [
            soldier_id
            for soldier_id in candidate_id_values
            if soldier_id not in soldiers_by_id
        ]
        if missing_ids:
            raise ValueError(
                "candidate pool references missing soldiers: "
                + ", ".join(missing_ids[:10])
            )
        roles = [_role_from_row(row) for row in role_rows]
        if not roles:
            raise ValueError(f"mission {request.mission_id!r} has no role slots")

        soldiers = [soldiers_by_id[soldier_id] for soldier_id in candidate_id_values]
        training_projections = {
            str(row[0]): _projection_from_json(row[1], label=f"training projection for soldier {row[0]}")
            for row in training_rows
        }
        deployment_outcome_projections: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in outcome_rows:
            deployment_outcome_projections[str(row[0])].append(
                _projection_from_json(row[2], label=f"deployment outcome for soldier {row[0]}")
            )
        source_refs = [
            SourceReference(
                ref=f"postgres://candidate_pools_current/{request.candidate_pool_id}",
                role="candidate_pool_resolved",
                source_hash=str(pool_row[2]),
                metadata={
                    "candidate_pool_id": request.candidate_pool_id,
                    "mission_id": request.mission_id,
                    "candidate_count": len(soldiers),
                },
            ),
            *[
                SourceReference(
                    ref=f"postgres://soldiers_current/{row[0]}",
                    role="candidate_profile",
                    source_hash=str(row[5]),
                    metadata={"soldier_id": str(row[0])},
                )
                for row in soldier_rows
            ],
            *[
                SourceReference(
                    ref=f"postgres://role_slots_current/{request.mission_id}/{row[0]}",
                    role="role_slot",
                    source_hash=str(row[4]),
                    metadata={"slot_id": str(row[0]), "role": str(row[1])},
                )
                for row in role_rows
            ],
            *[
                SourceReference(
                    ref=f"postgres://training_observations_current/{row[0]}",
                    role="training_projection",
                    source_hash=str(row[2]),
                    metadata={"soldier_id": str(row[0])},
                )
                for row in training_rows
            ],
            *[
                SourceReference(
                    ref=f"postgres://deployment_outcomes_current/{row[1]}/{row[0]}",
                    role="deployment_outcome_projection",
                    source_hash=str(row[3]),
                    metadata={"soldier_id": str(row[0]), "mission_id": str(row[1])},
                )
                for row in outcome_rows
            ],
        ]
        return _enrich_resolved_pool(ResolvedCandidatePool(
            candidate_pool_id=request.candidate_pool_id,
            mission_id=request.mission_id,
            candidates=soldiers,
            roles=roles,
            source_refs=dedupe_source_refs(source_refs),
            training_projections=training_projections,
            deployment_outcome_projections=dict(deployment_outcome_projections),
        ), request.mission_id)

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()

        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "Candidate pool resolution requires the 'infra' optional dependencies. "
                "Install with: pip install -e '.[infra]'"
            ) from exc

        return psycopg.connect(self.database_url)


def build_local_candidate_pool_source_refs(
    candidate_pool_id: str,
    mission_id: str,
    candidates: Sequence[Soldier],
    roles: Sequence[RoleRequirement],
) -> list[SourceReference]:
    refs = [
        SourceReference(
            ref=f"postgres://candidate_pools_current/{candidate_pool_id}",
            role="candidate_pool_resolved",
            source_hash=canonical_hash(
                {
                    "candidate_pool_id": candidate_pool_id,
                    "mission_id": mission_id,
                    "candidate_ids": [soldier.soldier_id for soldier in candidates],
                    "slot_ids": [role.slot_id for role in roles],
                }
            ),
            metadata={
                "candidate_pool_id": candidate_pool_id,
                "mission_id": mission_id,
                "candidate_count": len(candidates),
                "operational_source": True,
            },
        )
    ]
    refs.extend(
        SourceReference(
            ref=f"postgres://soldiers_current/{soldier.soldier_id}",
            role="candidate_profile",
            source_hash=canonical_hash(soldier.model_dump(mode="json")),
            metadata={"soldier_id": soldier.soldier_id},
        )
        for soldier in candidates
    )
    refs.extend(
        SourceReference(
            ref=f"postgres://role_slots_current/{mission_id}/{role.slot_id}",
            role="role_slot",
            source_hash=canonical_hash(role.model_dump(mode="json")),
            metadata={"slot_id": role.slot_id, "role": role.role},
        )
        for role in roles
    )
    return dedupe_source_refs(refs)


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _projection_from_json(value: Any, *, label: str) -> dict[str, Any]:
    projection = _json_value(value)
    if not isinstance(projection, dict):
        raise ValueError(f"{label} has invalid JSON projection")
    return projection


def _soldier_from_row(row: Any) -> Soldier:
    profile = _json_value(row[3])
    protected = _json_value(row[4])
    if not isinstance(profile, dict):
        raise ValueError(f"soldier {row[0]} has invalid profile_json")
    if protected is None:
        protected = {}
    if not isinstance(protected, dict):
        raise ValueError(f"soldier {row[0]} has invalid protected_json")
    payload = {
        **profile,
        **protected,
        "soldier_id": profile.get("soldier_id", row[0]),
        "unit_id": profile.get("unit_id", row[1]),
        "mos": profile.get("mos", row[2]),
    }
    return Soldier.model_validate(payload)


def _role_from_row(row: Any) -> RoleRequirement:
    return RoleRequirement(
        slot_id=str(row[0]),
        role=str(row[1]),
        required_mos=None if row[2] is None else str(row[2]),
        min_acft=int(row[3]),
    )


_FLOAT_FIELD_BOUNDS = {
    "self_efficacy_score": (1.0, 5.0),
    "peer_rating_z": (-3.0, 3.0),
    "home_unit_ranger_density": (0.0, 1.0),
    "operational_readiness": (0.0, 1.0),
    "medical_risk": (0.0, 1.0),
    "landing_asymmetry_score": (0.0, 1.0),
    "change_of_direction_index": (0.0, 1.0),
    "fatigue_index": (0.0, 1.0),
    "sandbox_score": (0.0, 1.0),
}

_INT_FIELD_BOUNDS = {
    "acft_score": (300, 600),
    "two_mile_run_sec": (600, 1500),
    "prior_missions": (0, None),
}


def _enrich_resolved_pool(pool: ResolvedCandidatePool, mission_id: str) -> ResolvedCandidatePool:
    candidate_ids = {soldier.soldier_id for soldier in pool.candidates}
    training_projections = {
        soldier_id: projection
        for soldier_id, projection in pool.training_projections.items()
        if soldier_id in candidate_ids
    }
    outcome_projections = {
        soldier_id: projections
        for soldier_id, projections in pool.deployment_outcome_projections.items()
        if soldier_id in candidate_ids
    }
    enriched_candidates = [
        _enrich_soldier(
            soldier,
            training_projection=training_projections.get(soldier.soldier_id),
            deployment_outcome_projections=outcome_projections.get(soldier.soldier_id, []),
        )
        for soldier in pool.candidates
    ]
    projection_refs = [
        *_local_training_source_refs(training_projections),
        *_local_deployment_outcome_source_refs(mission_id, outcome_projections),
    ]
    existing_refs = {ref.ref for ref in pool.source_refs}
    return ResolvedCandidatePool(
        candidate_pool_id=pool.candidate_pool_id,
        mission_id=pool.mission_id,
        candidates=enriched_candidates,
        roles=pool.roles,
        source_refs=dedupe_source_refs(
            [
                *pool.source_refs,
                *[ref for ref in projection_refs if ref.ref not in existing_refs],
            ]
        ),
        training_projections=training_projections,
        deployment_outcome_projections=outcome_projections,
    )


def _enrich_soldier(
    soldier: Soldier,
    *,
    training_projection: Mapping[str, Any] | None = None,
    deployment_outcome_projections: Sequence[Mapping[str, Any]] = (),
) -> Soldier:
    payload = soldier.model_dump(mode="json")
    for projection in [training_projection, *deployment_outcome_projections]:
        if isinstance(projection, Mapping):
            _apply_projection(payload, projection)
    return Soldier.model_validate(payload)


def _apply_projection(payload: dict[str, Any], projection: Mapping[str, Any]) -> None:
    views = [projection]
    for nested_key in ("metrics", "readiness", "performance"):
        nested = projection.get(nested_key)
        if isinstance(nested, Mapping):
            views.append(nested)

    for view in views:
        for field_name, (lower, upper) in _FLOAT_FIELD_BOUNDS.items():
            if field_name in view:
                value = _bounded_float(view[field_name], lower=lower, upper=upper)
                if value is not None:
                    payload[field_name] = value
        for field_name, (lower, upper) in _INT_FIELD_BOUNDS.items():
            if field_name in view:
                value = _bounded_int(view[field_name], lower=lower, upper=upper)
                if value is not None:
                    payload[field_name] = value

    for field_name in ("competencies", "milestones"):
        merged = _merge_rating_map(payload.get(field_name), projection.get(field_name))
        if merged is not None:
            payload[field_name] = merged


def _merge_rating_map(
    current: Any,
    projected: Any,
) -> dict[str, int] | None:
    if not isinstance(projected, Mapping):
        return None
    merged = dict(current) if isinstance(current, Mapping) else {}
    for key, raw_value in projected.items():
        value = _bounded_int(raw_value, lower=1, upper=5)
        if value is not None:
            merged[str(key)] = value
    return merged


def _bounded_float(value: Any, *, lower: float, upper: float) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return float(max(lower, min(upper, parsed)))


def _bounded_int(value: Any, *, lower: int, upper: int | None) -> int | None:
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    if upper is not None:
        parsed = min(upper, parsed)
    return max(lower, parsed)


def _local_training_source_refs(
    training_projections: Mapping[str, Mapping[str, Any]]
) -> list[SourceReference]:
    return [
        SourceReference(
            ref=f"postgres://training_observations_current/{soldier_id}",
            role="training_projection",
            source_hash=canonical_hash(projection),
            metadata={"soldier_id": soldier_id, "operational_source": True},
        )
        for soldier_id, projection in training_projections.items()
    ]


def _local_deployment_outcome_source_refs(
    mission_id: str,
    outcome_projections: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[SourceReference]:
    refs: list[SourceReference] = []
    for soldier_id, projections in outcome_projections.items():
        for projection in projections:
            outcome_mission_id = str(projection.get("mission_id", mission_id))
            refs.append(
                SourceReference(
                    ref=f"postgres://deployment_outcomes_current/{outcome_mission_id}/{soldier_id}",
                    role="deployment_outcome_projection",
                    source_hash=canonical_hash(projection),
                    metadata={
                        "soldier_id": soldier_id,
                        "mission_id": outcome_mission_id,
                        "operational_source": True,
                    },
                )
            )
    return refs
