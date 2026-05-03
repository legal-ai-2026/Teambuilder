from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


class JsonAgentClient(Protocol):
    provider: str
    model: str

    def complete_json(self, *, stage: str, system: str, user: str) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class OpenAIJsonAgentClient:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 45.0
    provider: str = "openai"

    def complete_json(self, *, stage: str, system: str, user: str) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "text": {
                "format": _response_format_for_stage(stage),
            },
            "store": False,
        }
        request = urllib.request.Request(
            self._url("/responses"),
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={
                "authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = _safe_error_detail(exc)
            raise RuntimeError(f"OpenAI agent stage '{stage}' failed: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI agent stage '{stage}' failed: {exc.reason}") from exc

        content = _response_content(response_payload)
        try:
            parsed = json.loads(_strip_json_fence(content))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OpenAI agent stage '{stage}' returned non-JSON content") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"OpenAI agent stage '{stage}' returned a JSON value that is not an object")
        return parsed

    def _url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path


def _response_format_for_stage(stage: str) -> dict[str, Any]:
    schema = _schema_for_stage(stage)
    if schema is None:
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "name": f"system2_{stage}_response",
        "strict": True,
        "schema": schema,
    }


def _schema_for_stage(stage: str) -> dict[str, Any] | None:
    schemas: dict[str, dict[str, Any]] = {
        "perception": {
            "type": "object",
            "additionalProperties": False,
            "required": ["observations"],
            "properties": {
                "observations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "kind",
                            "content",
                            "source_artifact_ids",
                            "confidence",
                            "subject_ref",
                        ],
                        "properties": {
                            "kind": {"type": "string"},
                            "content": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["summary"],
                                "properties": {
                                    "summary": {"type": "string"},
                                },
                            },
                            "source_artifact_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "confidence": {"type": "number"},
                            "subject_ref": _subject_ref_schema(),
                        },
                    },
                },
            },
        },
        "state": {
            "type": "object",
            "additionalProperties": False,
            "required": ["state_vector", "uncertainty"],
            "properties": {
                "state_vector": _state_vector_schema(),
                "uncertainty": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["overall", "by_field"],
                    "properties": {
                        "overall": {"type": "number"},
                        "by_field": _state_by_field_schema(),
                    },
                },
            },
        },
        "scenario": {
            "type": "object",
            "additionalProperties": False,
            "required": ["scenario_options"],
            "properties": {
                "scenario_options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "title",
                            "narrative",
                            "predicted_effect",
                            "risk_score",
                            "confidence",
                        ],
                        "properties": {
                            "title": {"type": "string"},
                            "narrative": {"type": "string"},
                            "predicted_effect": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "target_state_change",
                                    "expected_learning_value",
                                    "expected_mission_benefit",
                                ],
                                "properties": {
                                    "target_state_change": {"type": "string"},
                                    "expected_learning_value": {
                                        "type": ["number", "null"],
                                    },
                                    "expected_mission_benefit": {
                                        "type": ["number", "null"],
                                    },
                                },
                            },
                            "risk_score": {"type": "number"},
                            "confidence": {"type": "number"},
                        },
                    },
                },
            },
        },
        "critic": {
            "type": "object",
            "additionalProperties": False,
            "required": ["reviews"],
            "properties": {
                "reviews": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "index",
                            "critic_status",
                            "critic_reasons",
                            "risk_score",
                            "confidence",
                        ],
                        "properties": {
                            "index": {"type": "number"},
                            "critic_status": {
                                "type": "string",
                                "enum": ["pass", "modify", "escalate", "reject"],
                            },
                            "critic_reasons": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "risk_score": {"type": "number"},
                            "confidence": {"type": "number"},
                        },
                    },
                },
            },
        },
    }
    return schemas.get(stage)


def _subject_ref_schema() -> dict[str, Any]:
    return {
        "type": ["object", "null"],
        "additionalProperties": False,
        "required": ["subject_type", "subject_id"],
        "properties": {
            "subject_type": {
                "type": "string",
                "enum": ["person", "team", "mission", "environment"],
            },
            "subject_id": {"type": "string"},
        },
    }


def _state_vector_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "fatigue_burden",
            "situational_clarity",
            "cohesion",
            "leader_decision_quality",
            "mission_tempo_risk",
            "training_challenge_gap",
        ],
        "properties": {
            "fatigue_burden": {"type": "number"},
            "situational_clarity": {"type": "number"},
            "cohesion": {"type": "number"},
            "leader_decision_quality": {"type": "number"},
            "mission_tempo_risk": {"type": "number"},
            "training_challenge_gap": {"type": "number"},
        },
    }


def _state_by_field_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "fatigue_burden",
            "situational_clarity",
            "cohesion",
            "leader_decision_quality",
            "mission_tempo_risk",
            "training_challenge_gap",
        ],
        "properties": {
            "fatigue_burden": {"type": "number"},
            "situational_clarity": {"type": "number"},
            "cohesion": {"type": "number"},
            "leader_decision_quality": {"type": "number"},
            "mission_tempo_risk": {"type": "number"},
            "training_challenge_gap": {"type": "number"},
        },
    }


def _response_content(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if not isinstance(output, list) or not output:
        return _choice_content(payload)

    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for content_item in content:
            if not isinstance(content_item, dict):
                continue
            if content_item.get("type") == "refusal":
                refusal = content_item.get("refusal")
                raise RuntimeError(
                    str(refusal) if isinstance(refusal, str) else "OpenAI response refused the request"
                )
            text = content_item.get("text")
            if content_item.get("type") == "output_text" and isinstance(text, str):
                parts.append(text)
    if parts:
        return "".join(parts)
    raise RuntimeError("OpenAI response content was empty or malformed")


def _choice_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("OpenAI response did not include choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise RuntimeError("OpenAI response choice was malformed")
    message = first.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("OpenAI response did not include a message")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return "".join(parts)
    raise RuntimeError("OpenAI response content was empty or malformed")


def _strip_json_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _safe_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except Exception:
        return f"HTTP {exc.code}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return str(error["message"])
    return f"HTTP {exc.code}"
