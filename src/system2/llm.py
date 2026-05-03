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
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        request = urllib.request.Request(
            self._url("/chat/completions"),
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

        content = _choice_content(response_payload)
        try:
            parsed = json.loads(_strip_json_fence(content))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OpenAI agent stage '{stage}' returned non-JSON content") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"OpenAI agent stage '{stage}' returned a JSON value that is not an object")
        return parsed

    def _url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path


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
