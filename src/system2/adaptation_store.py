from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from .models import CognitiveAdaptationResponse


ADAPTATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS system2_adaptations (
    adaptation_id text PRIMARY KEY,
    mission_id text NOT NULL,
    team_id text NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    payload jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_system2_adaptations_mission_id
    ON system2_adaptations (mission_id);

CREATE INDEX IF NOT EXISTS idx_system2_adaptations_status
    ON system2_adaptations (status);
"""


ConnectionFactory = Callable[[], Any]


class AdaptationRepository(Protocol):
    def save(self, adaptation: CognitiveAdaptationResponse) -> CognitiveAdaptationResponse:
        ...

    def get(self, adaptation_id: str) -> CognitiveAdaptationResponse | None:
        ...

    def list_by_mission(self, mission_id: str, *, limit: int = 50) -> list[CognitiveAdaptationResponse]:
        ...


class InMemoryAdaptationRepository:
    def __init__(self) -> None:
        self._adaptations: dict[str, CognitiveAdaptationResponse] = {}

    def save(self, adaptation: CognitiveAdaptationResponse) -> CognitiveAdaptationResponse:
        self._adaptations[adaptation.adaptation_id] = adaptation
        return adaptation

    def get(self, adaptation_id: str) -> CognitiveAdaptationResponse | None:
        return self._adaptations.get(adaptation_id)

    def list_by_mission(self, mission_id: str, *, limit: int = 50) -> list[CognitiveAdaptationResponse]:
        matches = [
            adaptation
            for adaptation in self._adaptations.values()
            if adaptation.mission_id == mission_id
        ]
        matches.sort(key=lambda adaptation: adaptation.state.generated_at, reverse=True)
        return matches[:limit]


class PostgresAdaptationRepository:
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
                cursor.execute(ADAPTATION_SCHEMA_SQL)
            connection.commit()

    def save(self, adaptation: CognitiveAdaptationResponse) -> CognitiveAdaptationResponse:
        payload = dump_adaptation(adaptation)
        now = datetime.now(UTC)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO system2_adaptations (
                        adaptation_id, mission_id, team_id, status,
                        created_at, updated_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (adaptation_id) DO UPDATE SET
                        mission_id = EXCLUDED.mission_id,
                        team_id = EXCLUDED.team_id,
                        status = EXCLUDED.status,
                        updated_at = EXCLUDED.updated_at,
                        payload = EXCLUDED.payload
                    """,
                    (
                        adaptation.adaptation_id,
                        adaptation.mission_id,
                        adaptation.team_id,
                        adaptation.status,
                        adaptation.state.generated_at,
                        now,
                        payload,
                    ),
                )
            connection.commit()
        return adaptation

    def get(self, adaptation_id: str) -> CognitiveAdaptationResponse | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload FROM system2_adaptations WHERE adaptation_id = %s",
                    (adaptation_id,),
                )
                row = cursor.fetchone()
        if row is None:
            return None
        payload = row["payload"] if isinstance(row, dict) else row[0]
        return load_adaptation(payload)

    def list_by_mission(self, mission_id: str, *, limit: int = 50) -> list[CognitiveAdaptationResponse]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payload
                    FROM system2_adaptations
                    WHERE mission_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (mission_id, limit),
                )
                rows = cursor.fetchall()
        return [
            load_adaptation(row["payload"] if isinstance(row, dict) else row[0])
            for row in rows
        ]

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()

        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "Postgres adaptation storage requires the 'infra' optional dependencies. "
                "Install with: pip install -e '.[infra]'"
            ) from exc

        return psycopg.connect(self.database_url)


def dump_adaptation(adaptation: CognitiveAdaptationResponse) -> dict[str, Any]:
    return adaptation.model_dump(mode="json")


def load_adaptation(payload: dict[str, Any]) -> CognitiveAdaptationResponse:
    return CognitiveAdaptationResponse.model_validate(payload)
