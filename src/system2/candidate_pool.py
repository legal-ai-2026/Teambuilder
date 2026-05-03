from __future__ import annotations

import json
from collections.abc import Callable, Sequence
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
"""


ConnectionFactory = Callable[[], Any]


@dataclass(frozen=True)
class ResolvedCandidatePool:
    candidate_pool_id: str
    mission_id: str
    candidates: list[Soldier]
    roles: list[RoleRequirement]
    source_refs: list[SourceReference] = field(default_factory=list)


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
    ) -> None:
        self.pools[(candidate_pool_id, mission_id)] = ResolvedCandidatePool(
            candidate_pool_id=candidate_pool_id,
            mission_id=mission_id,
            candidates=list(candidates),
            roles=list(roles),
            source_refs=list(source_refs),
        )

    def resolve(self, request: ScoreRequest) -> ResolvedCandidatePool | None:
        if request.candidate_pool_id is None:
            return None
        return self.pools.get((request.candidate_pool_id, request.mission_id))


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
        ]
        return ResolvedCandidatePool(
            candidate_pool_id=request.candidate_pool_id,
            mission_id=request.mission_id,
            candidates=soldiers,
            roles=roles,
            source_refs=dedupe_source_refs(source_refs),
        )

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
