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

    def post_json_documented_error(self, path: str, body: Any | None = None) -> str:
        """POST JSON and return ``json_response`` output or a structured HTTP error string."""
        url = f"{self.base}/api/v3{path}"
        try:
            r = self._client.post(url, json=body)
        except httpx.HTTPError as e:
            return json.dumps({"error": "transport_error", "detail": repr(e), "path": path})
        if r.is_success:
            if not r.content:
                return json.dumps({"http_status": r.status_code, "body": None})
            try:
                return json_response(r.json())
            except Exception:
                return json.dumps({"http_status": r.status_code, "body": r.text[:20000]})
        text = (r.text or "")[:8000]
        err: dict[str, Any] = {
            "error": "http_error",
            "http_status": r.status_code,
            "body": text,
        }
        return json.dumps(err, indent=2)


def safe_tool_decorator(mcp_tool_factory: Any) -> Any:
    """Compose ``FastMCP.tool()`` with :func:`safe_tool` so every tool is exception-safe.

    Usage inside a ``build_*_app`` function::

        tool = safe_tool_decorator(mcp.tool)

        @tool()
        def my_tool() -> str: ...

    Equivalent to ``@mcp.tool()`` then ``safe_tool`` wrapping.
    """

    def _decorator(*d_args: Any, **d_kwargs: Any) -> Any:
        inner = mcp_tool_factory(*d_args, **d_kwargs)

        def _apply(fn: Any) -> Any:
            return inner(safe_tool(fn))

        return _apply

    return _decorator


def safe_tool(fn: Any) -> Any:
    """Wrap an MCP tool handler so unhandled exceptions return JSON instead of killing stdio.

    Without this, a single ``httpx.HTTPStatusError`` or ``httpx.ConnectError`` inside a
    FastMCP ``@mcp.tool()`` bubbles up through the stdio event loop and tears down the
    client session (``anyio.ClosedResourceError`` on every subsequent call in the parent).
    This decorator converts any exception into a JSON error string the agent can read.
    """
    import functools
    import traceback

    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            body = ""
            try:
                body = (e.response.text or "")[:4000]
            except Exception:
                pass
            return json.dumps({
                "error": "http_error",
                "http_status": e.response.status_code,
                "tool": fn.__name__,
                "body": body,
            })
        except httpx.HTTPError as e:
            return json.dumps({
                "error": "transport_error",
                "tool": fn.__name__,
                "detail": repr(e),
            })
        except Exception as e:  # noqa: BLE001
            return json.dumps({
                "error": "tool_unhandled_exception",
                "tool": fn.__name__,
                "type": type(e).__name__,
                "detail": str(e)[:1000],
                "trace": traceback.format_exc(limit=4)[-2000:],
            })

    return _wrapped


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
