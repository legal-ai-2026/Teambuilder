from __future__ import annotations

from typing import Any, Protocol

from .models import AgentRunStatus


class AgentStateStore(Protocol):
    def set_status(self, run_id: str, status: AgentRunStatus, *, ttl_seconds: int = 3600) -> None:
        ...

    def get_status(self, run_id: str) -> AgentRunStatus | None:
        ...

    def acquire_lock(self, run_id: str, *, ttl_seconds: int = 300) -> bool:
        ...

    def release_lock(self, run_id: str) -> None:
        ...


class InMemoryAgentStateStore:
    def __init__(self) -> None:
        self._statuses: dict[str, AgentRunStatus] = {}
        self._locks: set[str] = set()

    def set_status(self, run_id: str, status: AgentRunStatus, *, ttl_seconds: int = 3600) -> None:
        self._statuses[run_id] = status

    def get_status(self, run_id: str) -> AgentRunStatus | None:
        return self._statuses.get(run_id)

    def acquire_lock(self, run_id: str, *, ttl_seconds: int = 300) -> bool:
        if run_id in self._locks:
            return False
        self._locks.add(run_id)
        return True

    def release_lock(self, run_id: str) -> None:
        self._locks.discard(run_id)


class RedisAgentStateStore:
    def __init__(self, redis_url: str, *, client: Any | None = None, key_prefix: str = "system2") -> None:
        self.redis_url = redis_url
        self._client = client
        self.key_prefix = key_prefix.rstrip(":")

    def set_status(self, run_id: str, status: AgentRunStatus, *, ttl_seconds: int = 3600) -> None:
        self._redis().set(self._status_key(run_id), status.value, ex=ttl_seconds)

    def get_status(self, run_id: str) -> AgentRunStatus | None:
        raw = self._redis().get(self._status_key(run_id))
        if raw is None:
            return None
        value = raw.decode() if isinstance(raw, bytes) else str(raw)
        return AgentRunStatus(value)

    def acquire_lock(self, run_id: str, *, ttl_seconds: int = 300) -> bool:
        return bool(self._redis().set(self._lock_key(run_id), "1", nx=True, ex=ttl_seconds))

    def release_lock(self, run_id: str) -> None:
        self._redis().delete(self._lock_key(run_id))

    def _redis(self) -> Any:
        if self._client is not None:
            return self._client

        try:
            import redis
        except ImportError as exc:
            raise RuntimeError(
                "Redis agent state requires the 'infra' optional dependencies. "
                "Install with: pip install -e '.[infra]'"
            ) from exc

        self._client = redis.Redis.from_url(self.redis_url, decode_responses=False)
        return self._client

    def _status_key(self, run_id: str) -> str:
        return f"{self.key_prefix}:agent-run:{run_id}:status"

    def _lock_key(self, run_id: str) -> str:
        return f"{self.key_prefix}:agent-run:{run_id}:lock"
