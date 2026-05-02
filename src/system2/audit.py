from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REDACTED_FIELDS = {"protected_race", "protected_gender"}


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


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
        body = {
            "event_type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "previous_hash": previous_hash,
            "payload": redact_payload(payload),
        }
        body["record_hash"] = _canonical_hash(body)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str) + "\n")
        return str(body["record_hash"])


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
