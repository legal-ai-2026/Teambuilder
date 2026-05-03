from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit


_BACKENDS = {
    "adaptation_repository": {"memory", "postgres"},
    "agent_repository": {"memory", "postgres"},
    "agent_state": {"memory", "redis"},
    "audit": {"file", "postgres"},
    "candidate_pool": {"local", "postgres"},
    "deployment_repository": {"memory", "postgres"},
    "operational_twin_repository": {"memory", "postgres"},
    "retrieval": {"local", "pgvector"},
    "graph": {"local", "falkordb"},
    "shared_data": {"memory", "postgres"},
}

_AGENTIC_PROVIDERS = {"auto", "deterministic", "openai"}


def _env_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_env_file(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    env_path = Path(path)
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    with env_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            values[key.strip()] = _strip_env_value(value.strip())
    return values


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _csv_values(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _optional_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _backend(value: str | None, *, default: str, kind: str) -> str:
    selected = (value or default).strip().lower()
    allowed = _BACKENDS[kind]
    if selected not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ValueError(f"{kind} backend must be one of: {allowed_values}")
    return selected


def _agentic_provider(value: str | None) -> str:
    selected = (value or "auto").strip().lower()
    if selected not in _AGENTIC_PROVIDERS:
        allowed_values = ", ".join(sorted(_AGENTIC_PROVIDERS))
        raise ValueError(f"agentic provider must be one of: {allowed_values}")
    return selected


def _non_negative_int(value: str | None, *, default: int, name: str) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _positive_float(value: str | None, *, default: float, name: str) -> float:
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def normalize_postgres_url(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("postgresql+psycopg://"):
        return "postgresql://" + value.removeprefix("postgresql+psycopg://")
    return value


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
    pgvector_url: str | None
    redis_url: str | None
    falkordb_url: str | None
    pgvector_enabled: bool
    audit_log_path: str
    adaptation_repository_backend: str
    audit_backend: str
    agent_repository_backend: str
    agent_state_backend: str
    candidate_pool_backend: str
    deployment_repository_backend: str
    operational_twin_repository_backend: str
    retrieval_backend: str
    graph_backend: str
    shared_data_backend: str
    api_key: str | None
    admin_api_key: str | None
    cors_allowed_origins: tuple[str, ...]
    agentic_provider: str
    agentic_max_retries: int
    agentic_timeout_seconds: float
    openai_api_key: str | None
    openai_model: str
    openai_base_url: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "InfraSettings":
        provided = dict(env or os.environ)
        source = dict(_load_env_file(provided.get("SYSTEM2_ENV_FILE")))
        source.update(provided)
        database_url = normalize_postgres_url(source.get("DATABASE_URL"))
        pgvector_url = normalize_postgres_url(source.get("PGVECTOR_CONNECTION_STRING") or database_url)
        pgvector_enabled = _env_bool(source.get("PGVECTOR_ENABLED"), default=source.get("PGVECTOR_CONNECTION_STRING") is not None)
        api_key = _optional_value(source.get("SYSTEM2_API_KEY"))
        admin_api_key = _optional_value(source.get("SYSTEM2_ADMIN_API_KEY")) or api_key
        openai_api_key = _optional_value(source.get("OPENAI_API_KEY"))
        return cls(
            database_url=database_url,
            pgvector_url=pgvector_url,
            redis_url=source.get("REDIS_URL"),
            falkordb_url=source.get("FALKORDB_URL"),
            pgvector_enabled=pgvector_enabled,
            audit_log_path=source.get("SYSTEM2_AUDIT_LOG", "/tmp/system2_audit.jsonl"),
            adaptation_repository_backend=_backend(
                source.get("ADAPTATION_REPOSITORY_BACKEND"),
                default="postgres" if database_url else "memory",
                kind="adaptation_repository",
            ),
            audit_backend=_backend(
                source.get("AUDIT_BACKEND"),
                default="postgres" if database_url else "file",
                kind="audit",
            ),
            agent_repository_backend=_backend(
                source.get("AGENT_REPOSITORY_BACKEND"),
                default="postgres" if database_url else "memory",
                kind="agent_repository",
            ),
            agent_state_backend=_backend(
                source.get("AGENT_STATE_BACKEND"),
                default="redis" if source.get("REDIS_URL") else "memory",
                kind="agent_state",
            ),
            candidate_pool_backend=_backend(
                source.get("CANDIDATE_POOL_BACKEND"),
                default="postgres" if database_url else "local",
                kind="candidate_pool",
            ),
            deployment_repository_backend=_backend(
                source.get("DEPLOYMENT_REPOSITORY_BACKEND"),
                default="postgres" if database_url else "memory",
                kind="deployment_repository",
            ),
            operational_twin_repository_backend=_backend(
                source.get("OPERATIONAL_TWIN_REPOSITORY_BACKEND"),
                default="postgres" if database_url else "memory",
                kind="operational_twin_repository",
            ),
            retrieval_backend=_backend(
                source.get("RETRIEVAL_BACKEND"),
                default="pgvector" if pgvector_url and pgvector_enabled else "local",
                kind="retrieval",
            ),
            graph_backend=_backend(
                source.get("GRAPH_BACKEND"),
                default="falkordb" if source.get("FALKORDB_URL") else "local",
                kind="graph",
            ),
            shared_data_backend=_backend(
                source.get("SHARED_DATA_BACKEND"),
                default="postgres" if database_url else "memory",
                kind="shared_data",
            ),
            api_key=api_key,
            admin_api_key=admin_api_key,
            cors_allowed_origins=_csv_values(source.get("SYSTEM2_CORS_ORIGINS")),
            agentic_provider=_agentic_provider(source.get("SYSTEM2_AGENTIC_PROVIDER")),
            agentic_max_retries=_non_negative_int(
                source.get("SYSTEM2_AGENTIC_MAX_RETRIES"),
                default=1,
                name="SYSTEM2_AGENTIC_MAX_RETRIES",
            ),
            agentic_timeout_seconds=_positive_float(
                source.get("SYSTEM2_AGENTIC_TIMEOUT_SECONDS"),
                default=45.0,
                name="SYSTEM2_AGENTIC_TIMEOUT_SECONDS",
            ),
            openai_api_key=openai_api_key,
            openai_model=source.get("OPENAI_MODEL", "gpt-5.4-mini"),
            openai_base_url=source.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )

    def status(self) -> dict[str, object]:
        return {
            "postgres": {
                "configured": self.database_url is not None,
                "url": redact_url(self.database_url),
                "pgvector_enabled": self.pgvector_enabled,
                "pgvector_url": redact_url(self.pgvector_url),
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
                "adaptation_repository": self.adaptation_repository_backend,
                "audit": self.audit_backend,
                "agent_repository": self.agent_repository_backend,
                "agent_state": self.agent_state_backend,
                "candidate_pool": self.candidate_pool_backend,
                "deployment_repository": self.deployment_repository_backend,
                "operational_twin_repository": self.operational_twin_repository_backend,
                "retrieval": self.retrieval_backend,
                "graph": self.graph_backend,
                "shared_data": self.shared_data_backend,
            },
            "security": {
                "api_key_required": self.api_key is not None,
                "admin_api_key_required": self.admin_api_key is not None,
                "cors_allowed_origins": list(self.cors_allowed_origins),
            },
            "agentic_runtime": {
                "provider": self.agentic_provider,
                "max_retries": self.agentic_max_retries,
                "timeout_seconds": self.agentic_timeout_seconds,
                "input_boundary": "processed_system1_data",
                "openai_configured": self.openai_api_key is not None,
                "openai_model": self.openai_model,
                "openai_base_url": redact_url(self.openai_base_url),
            },
        }
