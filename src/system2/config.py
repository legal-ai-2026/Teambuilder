from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit


_BACKENDS = {
    "agent_repository": {"memory", "postgres"},
    "agent_state": {"memory", "redis"},
    "audit": {"file", "postgres"},
    "retrieval": {"local", "pgvector"},
    "graph": {"local", "falkordb"},
}


def _env_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _backend(value: str | None, *, default: str, kind: str) -> str:
    selected = (value or default).strip().lower()
    allowed = _BACKENDS[kind]
    if selected not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ValueError(f"{kind} backend must be one of: {allowed_values}")
    return selected


def redact_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if not parsed.netloc:
        return value

    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    user = parsed.username
    auth = f"{user}:***@" if user else ""
    redacted_netloc = f"{auth}{host}{port}"
    return urlunsplit((parsed.scheme, redacted_netloc, parsed.path, parsed.query, parsed.fragment))


@dataclass(frozen=True)
class InfraSettings:
    database_url: str | None
    redis_url: str | None
    falkordb_url: str | None
    pgvector_enabled: bool
    audit_log_path: str
    audit_backend: str
    agent_repository_backend: str
    agent_state_backend: str
    retrieval_backend: str
    graph_backend: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "InfraSettings":
        source = env or os.environ
        pgvector_enabled = _env_bool(source.get("PGVECTOR_ENABLED"))
        return cls(
            database_url=source.get("DATABASE_URL"),
            redis_url=source.get("REDIS_URL"),
            falkordb_url=source.get("FALKORDB_URL"),
            pgvector_enabled=pgvector_enabled,
            audit_log_path=source.get("SYSTEM2_AUDIT_LOG", "/tmp/system2_audit.jsonl"),
            audit_backend=_backend(
                source.get("AUDIT_BACKEND"),
                default="postgres" if source.get("DATABASE_URL") else "file",
                kind="audit",
            ),
            agent_repository_backend=_backend(
                source.get("AGENT_REPOSITORY_BACKEND"),
                default="postgres" if source.get("DATABASE_URL") else "memory",
                kind="agent_repository",
            ),
            agent_state_backend=_backend(
                source.get("AGENT_STATE_BACKEND"),
                default="redis" if source.get("REDIS_URL") else "memory",
                kind="agent_state",
            ),
            retrieval_backend=_backend(
                source.get("RETRIEVAL_BACKEND"),
                default="pgvector" if source.get("DATABASE_URL") and pgvector_enabled else "local",
                kind="retrieval",
            ),
            graph_backend=_backend(
                source.get("GRAPH_BACKEND"),
                default="falkordb" if source.get("FALKORDB_URL") else "local",
                kind="graph",
            ),
        )

    def status(self) -> dict[str, object]:
        return {
            "postgres": {
                "configured": self.database_url is not None,
                "url": redact_url(self.database_url),
                "pgvector_enabled": self.pgvector_enabled,
            },
            "redis": {
                "configured": self.redis_url is not None,
                "url": redact_url(self.redis_url),
            },
            "falkordb": {
                "configured": self.falkordb_url is not None,
                "url": redact_url(self.falkordb_url),
            },
            "audit_log_path": self.audit_log_path,
            "backends": {
                "audit": self.audit_backend,
                "agent_repository": self.agent_repository_backend,
                "agent_state": self.agent_state_backend,
                "retrieval": self.retrieval_backend,
                "graph": self.graph_backend,
            },
        }
