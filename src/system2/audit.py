from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


REDACTED_FIELDS = {"protected_race", "protected_gender"}

POSTGRES_AUDIT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS system2_audit_log (
    sequence_id bigserial PRIMARY KEY,
    event_type text NOT NULL,
    timestamp timestamptz NOT NULL,
    previous_hash text NOT NULL,
    record_hash text NOT NULL UNIQUE,
    payload jsonb NOT NULL,
    record jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_system2_audit_log_event_type
    ON system2_audit_log (event_type);

CREATE INDEX IF NOT EXISTS idx_system2_audit_log_timestamp
    ON system2_audit_log (timestamp);
"""


class AuditSink(Protocol):
    def append(self, event_type: str, payload: dict[str, Any]) -> str:
        ...


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_audit_record(event_type: str, payload: dict[str, Any], previous_hash: str) -> dict[str, Any]:
    body = {
        "event_type": event_type,
        "timestamp": datetime.now(UTC).isoformat(),
        "previous_hash": previous_hash,
        "payload": redact_payload(payload),
    }
    body["record_hash"] = _canonical_hash(body)
    return body


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key in REDACTED_FIELDS:
                continue
            if key in {"unit_id", "mos"}:
                redacted[key] = hashlib.sha256(str(item).encode()).hexdigest()[:12]
            else:
                redacted[key] = redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value


class AuditLog:
    def __init__(self, path: str | Path | None = None) -> None:
        default_path = os.environ.get("SYSTEM2_AUDIT_LOG", "/tmp/system2_audit.jsonl")
        self.path = Path(path or default_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _previous_hash(self) -> str:
        if not self.path.exists():
            return "0" * 64
        last = ""
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last = line
        if not last:
            return "0" * 64
        return str(json.loads(last)["record_hash"])

    def append(self, event_type: str, payload: dict[str, Any]) -> str:
        previous_hash = self._previous_hash()
        body = build_audit_record(event_type, payload, previous_hash)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str) + "\n")
        return str(body["record_hash"])


class PostgresAuditLog:
    def __init__(self, database_url: str, *, connection_factory: Any | None = None, auto_migrate: bool = True) -> None:
        self.database_url = database_url
        self._connection_factory = connection_factory
        if auto_migrate:
            self.migrate()

    def migrate(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(POSTGRES_AUDIT_SCHEMA_SQL)
            connection.commit()

    def append(self, event_type: str, payload: dict[str, Any]) -> str:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT record_hash FROM system2_audit_log ORDER BY sequence_id DESC LIMIT 1")
                row = cursor.fetchone()
                previous_hash = _row_value(row, "record_hash") if row is not None else "0" * 64
                body = build_audit_record(event_type, payload, previous_hash)
                cursor.execute(
                    """
                    INSERT INTO system2_audit_log (
                        event_type, timestamp, previous_hash, record_hash, payload, record
                    )
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
                    """,
                    (
                        body["event_type"],
                        body["timestamp"],
                        body["previous_hash"],
                        body["record_hash"],
                        json.dumps(body["payload"], sort_keys=True),
                        json.dumps(body, sort_keys=True, default=str),
                    ),
                )
            connection.commit()
        return str(body["record_hash"])

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()

        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "Postgres audit logging requires the 'infra' optional dependencies. "
                "Install with: pip install -e '.[infra]'"
            ) from exc

        return psycopg.connect(self.database_url)


def validate_hash_chain(path: str | Path) -> bool:
    audit_path = Path(path)
    previous = "0" * 64
    if not audit_path.exists():
        return True
    with audit_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            expected = record["record_hash"]
            if record["previous_hash"] != previous:
                return False
            candidate = dict(record)
            candidate.pop("record_hash")
            if _canonical_hash(candidate) != expected:
                return False
            previous = expected
    return True


def validate_audit_records(records: list[dict[str, Any]]) -> bool:
    previous = "0" * 64
    for record in records:
        expected = record["record_hash"]
        if record["previous_hash"] != previous:
            return False
        candidate = dict(record)
        candidate.pop("record_hash")
        if _canonical_hash(candidate) != expected:
            return False
        previous = expected
    return True


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row[key]
    return row[0]
