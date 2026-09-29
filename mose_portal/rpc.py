"""WebSocket RPC for Code Mode sandbox ↔ portal (hello / call / done)."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp

from mose.approval_timing import APPROVAL_REQUEST_TIMEOUT_SECONDS
from mose.mcp_write_policy import classify_mcp_tool
from mose_portal.codegen import generate_mcp_dts, sanitize_server_ts

if TYPE_CHECKING:
    from websockets.asyncio.server import Server

    from mose_portal.aggregator import PortalAggregator

log = logging.getLogger("mose_portal.rpc")

MAX_WS_TEXT = 512_000
MAX_CALL_RESULT = 256_000
_APPROVAL_HTTP_TIMEOUT = float(APPROVAL_REQUEST_TIMEOUT_SECONDS)


def _approval_bridge_url() -> str:
    return (os.environ.get("MCP_PORTAL_AGENT_APPROVAL_URL") or "").strip()


async def _request_mutating_tool_approval(
    *,
    url: str,
    server: str,
    tool: str,
    arguments: dict[str, Any],
    scheduled_approval_token: str | None = None,
) -> bool:
    summary = json.dumps(arguments, default=str, sort_keys=True)
    if len(summary) > 500:
        summary = summary[:497] + "..."
    payload = {
        "server": server,
        "tool": tool,
        "arguments_summary": summary,
        "reason": "Code Mode: mutating MCP tool (approval required)",
    }
    if scheduled_approval_token:
        payload["scheduled_approval_token"] = scheduled_approval_token
    timeout = aiohttp.ClientTimeout(total=_APPROVAL_HTTP_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    log.warning("approval_bridge_http_status status=%s", resp.status)
                    return False
                data = await resp.json(content_type=None)
                if not isinstance(data, dict):
                    return False
                return bool(data.get("approved"))
    except Exception:
        log.exception("approval_bridge_request_failed url=%s", url[:80])
        return False


@dataclass
class _PendingSession:
    future: asyncio.Future[dict[str, Any]]
    created_at: float
    websocket: Any | None = None
    scheduled_approval_token: str | None = None


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 20] + "\n...<truncated>..."


def ts_keys_to_servers(servers: list[str]) -> dict[str, str]:
    """Map TS-safe server keys (``sanitize_server_ts``) back to real MCP server names."""
    return {sanitize_server_ts(name): name for name in servers}


class CodeModeRPC:
    """One-shot session tokens; sandbox connects with ``hello`` then ``call`` / ``done``."""

    def __init__(self, aggregator: PortalAggregator) -> None:
        self._agg = aggregator
        self._sessions: dict[str, _PendingSession] = {}
        self._repeat_guard: dict[str, dict[str, Any]] = {}
        self._ws_server: Server | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._bound_host: str = "0.0.0.0"
        self._bound_port: int = 9001

    @property
    def bound_port(self) -> int:
        return self._bound_port

    @property
    def listen_port(self) -> int:
        srv = self._ws_server
        if srv is not None:
            socks = getattr(srv, "sockets", None) or []
            if socks:
                return int(socks[0].getsockname()[1])
        return self._bound_port

    def register_session(
        self, *, scheduled_approval_token: str | None = None
    ) -> tuple[str, asyncio.Future[dict[str, Any]]]:
        token = secrets.token_urlsafe(24)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._sessions[token] = _PendingSession(
            future=fut,
            created_at=time.monotonic(),
            scheduled_approval_token=(scheduled_approval_token or "").strip() or None,
        )
        log.debug("codemode_session_registered token=%s", token[:8])
        return token, fut

    def finish_session(self, token: str, payload: dict[str, Any]) -> None:
        """Complete a session (from WS ``done`` or tests)."""
        self._repeat_guard.pop(token, None)
        sess = self._sessions.pop(token, None)
        if sess is None:
            log.warning("codemode_finish_unknown_token token=%s", token[:8])
            return
        if not sess.future.done():
            sess.future.set_result(payload)

    def abandon_session(self, token: str, message: str) -> None:
        """Complete session with a runtime error if still pending."""
        payload = {
            "stdout": "",
            "stderr": "",
            "return_value": None,
            "errors": [{"kind": "runtime", "message": message}],
        }
        self.finish_session(token, payload)

    def discard_pending(self, token: str) -> None:
        """Remove token without completing the future (caller handles the future)."""
        self._sessions.pop(token, None)

    async def _serve_loop(self, host: str, port: int, started: asyncio.Event, stop: asyncio.Event) -> None:
        from websockets.asyncio.server import serve

        async with serve(
            self._connection_handler,
            host,
            port,
            max_size=MAX_WS_TEXT + 1024,
        ) as server:
            self._ws_server = server
            started.set()
            await stop.wait()

    async def _try_bind(self, host: str, port: int) -> bool:
        """Spawn ``_serve_loop`` and wait until it's either listening or it fails."""
        started = asyncio.Event()
        stop = asyncio.Event()
        task = asyncio.create_task(
            self._serve_loop(host, port, started, stop),
            name="mose_portal_codemode_rpc",
        )
        # Race: server signals ``started``, OR the task ends (bind failure).
        wait_started: asyncio.Task[bool] = asyncio.create_task(started.wait())
        done, _pending = await asyncio.wait(
            {wait_started, task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if started.is_set():
            wait_started.cancel()
            self._serve_task = task
            self._stop_event = stop
            return True
        # Task ended before listen — re-raise its exception (or signal generic failure).
        wait_started.cancel()
        try:
            task.result()
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                return False
            raise
        return False

    async def start(self, host: str = "0.0.0.0", port: int = 9001) -> None:
        if self._serve_task is not None:
            return
        self._bound_host = host
        self._bound_port = port
        if await self._try_bind(host, port):
            log.info("codemode_rpc_listen host=%s port=%s", host, port)
            return
        # Port in use — fall back to an ephemeral port so concurrent portal
        # processes (e.g. diagnostics, second agent) don't deadlock.
        log.warning("codemode_rpc_port_in_use port=%s; using ephemeral port", port)
        if not await self._try_bind(host, 0):
            raise RuntimeError("Could not bind Code Mode RPC server (no free port)")
        actual = self.listen_port
        self._bound_port = actual
        log.info("codemode_rpc_listen host=%s port=%s (ephemeral)", host, actual)

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._serve_task is not None:
            try:
                await self._serve_task
            except asyncio.CancelledError:
                pass
            self._serve_task = None
        self._stop_event = None
        self._ws_server = None
        log.info("codemode_rpc_stopped")

    async def _connection_handler(self, websocket: Any) -> None:
        token_for_cleanup: str | None = None
        try:
            raw = await websocket.recv()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            msg = json.loads(raw)
            if not isinstance(msg, dict) or msg.get("type") != "hello":
                await websocket.close(code=4000, reason="expected hello")
                return
            token = str(msg.get("token") or "")
            if not token:
                await websocket.close(code=4000, reason="missing token")
                return
            token_for_cleanup = token
            sess = self._sessions.get(token)
            if sess is None:
                await websocket.send(json.dumps({"type": "error", "message": "unknown or expired session token"}))
                await websocket.close(code=4000, reason="bad token")
                return
            if sess.websocket is not None:
                await websocket.send(json.dumps({"type": "error", "message": "session already bound"}))
                await websocket.close(code=4000, reason="double hello")
                return
            sess.websocket = websocket

            tools = self._agg.flatten_tools()
            mcp_dts = generate_mcp_dts(tools)
            await websocket.send(
                json.dumps(
                    {
                        "type": "ready",
                        "mcp_dts": mcp_dts,
                        "note": "Runtime mcp proxy is built into runner.ts (Phase 2). Types are for reference.",
                    }
                )
            )

            while True:
                raw2 = await websocket.recv()
                if isinstance(raw2, bytes):
                    raw2 = raw2.decode("utf-8", errors="replace")
                data = json.loads(raw2)
                if not isinstance(data, dict):
                    continue
                mtype = data.get("type")
                if mtype == "call":
                    await self._handle_call(websocket, data, session_token=token)
                elif mtype == "done":
                    errs = data.get("errors")
                    payload = {
                        "stdout": str(data.get("stdout") or ""),
                        "stderr": str(data.get("stderr") or ""),
                        "return_value": data.get("return_value"),
                        "errors": errs if isinstance(errs, list) else [],
                    }
                    self.finish_session(token, payload)
                    token_for_cleanup = None
                    break
                else:
                    await websocket.send(json.dumps({"type": "error", "message": f"unknown message type: {mtype}"}))
        except json.JSONDecodeError as e:
            log.warning("codemode_ws_bad_json error=%s", e)
        except Exception:
            log.exception("codemode_ws_handler_error")
        finally:
            if token_for_cleanup:
                tok = token_for_cleanup
                sess = self._sessions.get(tok)
                if sess is not None and not sess.future.done():
                    self.abandon_session(
                        tok,
                        "WebSocket closed before done (sandbox crash or disconnect).",
                    )

    async def _handle_call(self, websocket: Any, data: dict[str, Any], *, session_token: str) -> None:
        call_id = str(data.get("id") or "")
        server_ts = str(data.get("server_ts") or data.get("server") or "").strip()
        tool = str(data.get("tool") or "").strip()
        args = data.get("arguments")
        if not call_id or not server_ts or not tool:
            await websocket.send(
                json.dumps({"type": "call_result", "id": call_id, "ok": False, "error": "missing id/server_ts/tool"})
            )
            return
        if not isinstance(args, dict):
            args = {}

        keymap = ts_keys_to_servers(list(self._agg.servers.keys()))
        real_server = keymap.get(server_ts)
        if real_server is None:
            await websocket.send(
                json.dumps(
                    {
                        "type": "call_result",
                        "id": call_id,
                        "ok": False,
                        "error": f"unknown server key {server_ts!r} (valid: {sorted(keymap.keys())})",
                    }
                )
            )
            return

        full_name = f"{real_server}__{tool}"
        sig = f"{full_name}:{json.dumps(args, sort_keys=True, default=str)}"
        guard = self._repeat_guard.setdefault(session_token, {"sig": "", "fail_streak": 0})
        if guard.get("sig") == sig and int(guard.get("fail_streak", 0) or 0) >= 2:
            await websocket.send(
                json.dumps(
                    {
                        "type": "call_result",
                        "id": call_id,
                        "ok": False,
                        "error": (
                            "Error: [MCP] The same tool with the same arguments failed twice in a row with "
                            "an MCP-level error. Do not call again with these arguments; fix parameters or "
                            "check upstream logs."
                        ),
                    }
                )
            )
            return

        if classify_mcp_tool(real_server, tool) != "read":
            bridge = _approval_bridge_url()
            if not bridge:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "call_result",
                            "id": call_id,
                            "ok": False,
                            "error": (
                                "Mutating MCP tool blocked: set MCP_PORTAL_AGENT_APPROVAL_URL to the agent "
                                "approval bridge (e.g. http://mose-agent:9100/approve) and enable "
                                "[portal].enabled on mose-agent."
                            ),
                        }
                    )
                )
                return
            sess = self._sessions.get(session_token)
            scheduled_approval_token = (
                sess.scheduled_approval_token if sess is not None else None
            )
            approved = await _request_mutating_tool_approval(
                url=bridge,
                server=real_server,
                tool=tool,
                arguments=args,
                scheduled_approval_token=scheduled_approval_token,
            )
            if not approved:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "call_result",
                            "id": call_id,
                            "ok": False,
                            "error": "Execution denied by operator (mutating MCP tool).",
                        }
                    )
                )
                return

        try:
            text, is_err = await self._agg.call_tool(full_name, args)
        except Exception as e:  # noqa: BLE001
            log.exception("codemode_call_tool_failed tool=%s", full_name)
            await websocket.send(
                json.dumps({"type": "call_result", "id": call_id, "ok": False, "error": repr(e)[:2000]})
            )
            return

        if is_err:
            if guard.get("sig") == sig:
                guard["fail_streak"] = int(guard.get("fail_streak", 0) or 0) + 1
            else:
                guard["sig"] = sig
                guard["fail_streak"] = 1
        else:
            guard["sig"] = ""
            guard["fail_streak"] = 0

        text = _truncate(text, MAX_CALL_RESULT)
        await websocket.send(
            json.dumps({"type": "call_result", "id": call_id, "ok": not is_err, "result": text, "is_error": is_err})
        )
