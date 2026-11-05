"""Exception-safe tool wrapper so a failing handler cannot kill the stdio session."""

from __future__ import annotations

import functools
import json
import traceback
from typing import Any

import httpx


def safe_tool(fn: Any) -> Any:
    """Wrap an MCP tool handler so unhandled exceptions return JSON instead of raising."""

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


def safe_tool_decorator(mcp_tool_factory: Any) -> Any:
    """Compose ``FastMCP.tool()`` with :func:`safe_tool`."""

    def _decorator(*d_args: Any, **d_kwargs: Any) -> Any:
        inner = mcp_tool_factory(*d_args, **d_kwargs)

        def _apply(fn: Any) -> Any:
            return inner(safe_tool(fn))

        return _apply

    return _decorator
