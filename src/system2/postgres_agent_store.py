from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .models import AgentRun, AgentRunRequest, AgentRunStatus


AGENT_RUNS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS system2_agent_runs (
    run_id text PRIMARY KEY,
    status text NOT NULL,
    mission_id text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    payload jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_system2_agent_runs_status
    ON system2_agent_runs (status);

CREATE INDEX IF NOT EXISTS idx_system2_agent_runs_mission_id
    ON system2_agent_runs (mission_id);
"""


ConnectionFactory = Callable[[], Any]


class PostgresAgentRunRepository:
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
                cursor.execute(AGENT_RUNS_SCHEMA_SQL)
            connection.commit()

    def create(self, request: AgentRunRequest) -> AgentRun:
        now = datetime.now(UTC)
        run = AgentRun(
            run_id=str(uuid4()),
            status=AgentRunStatus.queued,
            request=request,
            steps=[],
            created_at=now,
            updated_at=now,
        )
        return self.save(run)

    def get(self, run_id: str) -> AgentRun | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload FROM system2_agent_runs WHERE run_id = %s", (run_id,))
                row = cursor.fetchone()
        if row is None:
            return None
        payload = row["payload"] if isinstance(row, dict) else row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return load_agent_run(payload)

    def save(self, run: AgentRun) -> AgentRun:
        stored = run.model_copy(update={"updated_at": datetime.now(UTC)})
        payload = dump_agent_run(stored)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO system2_agent_runs (
                        run_id, status, mission_id, created_at, updated_at, payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (run_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        mission_id = EXCLUDED.mission_id,
                        updated_at = EXCLUDED.updated_at,
                        payload = EXCLUDED.payload
                    """,
                    (
                        stored.run_id,
                        stored.status.value,
                        stored.request.score_request.mission_id,
                        stored.created_at,
                        stored.updated_at,
                        json.dumps(payload, sort_keys=True, default=str),
                    ),
                )
            connection.commit()
        return stored

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()

        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "Postgres agent storage requires the 'infra' optional dependencies. "
                "Install with: pip install -e '.[infra]'"
            ) from exc

        return psycopg.connect(self.database_url)


def dump_agent_run(run: AgentRun) -> dict[str, Any]:
    return run.model_dump(mode="json")


def load_agent_run(payload: dict[str, Any]) -> AgentRun:
    return AgentRun.model_validate(payload)
