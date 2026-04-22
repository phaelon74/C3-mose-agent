"""Shared synchronous httpx client for *arr API v3."""

from __future__ import annotations

import json
from typing import Any

import httpx


def normalize_base_url(url: str) -> str:
    u = url.strip().rstrip("/")
    if u.endswith("/api/v3"):
        u = u[: -len("/api/v3")]
    return u.rstrip("/")


class ArrClient:
    """Minimal client: GET/POST/DELETE under ``{base}/api/v3`` with ``X-Api-Key``."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0) -> None:
        self.base = normalize_base_url(base_url)
        self._client = httpx.Client(
            timeout=timeout,
            headers={"X-Api-Key": api_key},
        )

    def close(self) -> None:
        self._client.close()

    def _clean_params(self, params: dict[str, Any] | None) -> dict[str, Any]:
        if not params:
            return {}
        out: dict[str, Any] = {}
        for k, v in params.items():
            if v is None:
                continue
            out[k] = v
        return out

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base}/api/v3{path}"
        r = self._client.get(url, params=self._clean_params(params))
        r.raise_for_status()
        if not r.content:
            return None
        return r.json()

    def get_text(self, path: str, params: dict[str, Any] | None = None) -> str:
        url = f"{self.base}/api/v3{path}"
        r = self._client.get(url, params=self._clean_params(params))
        r.raise_for_status()
        return r.text

    def delete_json(self, path: str) -> Any:
        url = f"{self.base}/api/v3{path}"
        r = self._client.delete(url)
        r.raise_for_status()
        if not r.content:
            return None
        return r.json()

    def post_json(self, path: str, body: Any | None = None) -> Any:
        url = f"{self.base}/api/v3{path}"
        r = self._client.post(url, json=body)
        r.raise_for_status()
        if not r.content:
            return None
        return r.json()

    def post_empty(self, path: str) -> Any:
        url = f"{self.base}/api/v3{path}"
        r = self._client.post(url)
        r.raise_for_status()
        if not r.content:
            return None
        return r.json()


def truncate_output(text: str, max_lines: int = 200, max_chars: int = 20000) -> str:
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[:max_lines]) + f"\n\n... truncated to {max_lines} lines"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... truncated to {max_chars} characters"
    return text


def json_response(data: Any, max_chars: int = 20000) -> str:
    s = json.dumps(data, indent=2, default=str)
    if len(s) > max_chars:
        return s[:max_chars] + f"\n\n... truncated ({len(s)} chars total)"
    return s
