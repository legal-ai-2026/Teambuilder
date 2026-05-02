from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from .models import AgentRun, AgentRunRequest, AgentRunStatus


class AgentRunRepository(Protocol):
    def create(self, request: AgentRunRequest) -> AgentRun:
        ...

    def get(self, run_id: str) -> AgentRun | None:
        ...

    def save(self, run: AgentRun) -> AgentRun:
        ...


class InMemoryAgentRunRepository:
    def __init__(self) -> None:
        self._runs: dict[str, AgentRun] = {}

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
        self._runs[run.run_id] = run
        return run

    def get(self, run_id: str) -> AgentRun | None:
        return self._runs.get(run_id)

    def save(self, run: AgentRun) -> AgentRun:
        stored = run.model_copy(update={"updated_at": datetime.now(UTC)})
        self._runs[stored.run_id] = stored
        return stored
