from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit


def _env_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "InfraSettings":
        source = env or os.environ
        return cls(
            database_url=source.get("DATABASE_URL"),
            redis_url=source.get("REDIS_URL"),
            falkordb_url=source.get("FALKORDB_URL"),
            pgvector_enabled=_env_bool(source.get("PGVECTOR_ENABLED")),
            audit_log_path=source.get("SYSTEM2_AUDIT_LOG", "/tmp/system2_audit.jsonl"),
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
        }
