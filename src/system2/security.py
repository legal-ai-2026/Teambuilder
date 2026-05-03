from __future__ import annotations

import hmac

from fastapi import Header, HTTPException


class ApiKeyGuard:
    def __init__(self, *, api_key: str | None = None, admin_api_key: str | None = None) -> None:
        self.api_key = _normalize_key(api_key)
        self.admin_api_key = _normalize_key(admin_api_key) or self.api_key

    def require_api_key(
        self,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> None:
        if self.api_key is None:
            return
        if _matches_key(self.api_key, authorization=authorization, x_api_key=x_api_key):
            return
        raise HTTPException(status_code=401, detail="API key required")

    def require_admin_key(
        self,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> None:
        if self.admin_api_key is None:
            return
        if _matches_key(self.admin_api_key, authorization=authorization, x_api_key=x_api_key):
            return
        raise HTTPException(status_code=401, detail="admin API key required")


def _matches_key(
    expected: str,
    *,
    authorization: str | None,
    x_api_key: str | None,
) -> bool:
    candidates = [_normalize_key(x_api_key)]
    if isinstance(authorization, str):
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            candidates.append(_normalize_key(token))
    return any(
        candidate is not None and hmac.compare_digest(candidate, expected)
        for candidate in candidates
    )


def _normalize_key(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
