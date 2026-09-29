"""FastMCP server: ``portal_codemode_search`` + ``portal_codemode_execute`` (Deno sandbox)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

from mose.approval_timing import APPROVAL_REQUEST_TIMEOUT_SECONDS
from mose_portal.aggregator import PortalAggregator
from mose_portal.rpc import CodeModeRPC
from mose_portal.sandbox_runner import (
    resolve_rpc_host_for_sandbox,
    resolve_rpc_port,
    resolve_sandbox_container,
    run_deno_sandbox,
)
from mose_portal.search import search_tools

log = logging.getLogger("mose_portal.mcp_server")

_MAX_EXECUTE_OUTPUT = 256_000


@dataclass
class PortalLifespanState:
    aggregator: PortalAggregator
    use_embeddings: bool
    rpc: CodeModeRPC


def _resolve_config_path() -> Path:
    raw = os.environ.get("MCP_PORTAL_CONFIG", "mcp_servers.json")
    p = Path(raw)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


def _resolve_use_embeddings() -> bool:
    """Operator-controlled: ``MCP_PORTAL_USE_EMBEDDINGS=1`` enables semantic search."""
    raw = (os.environ.get("MCP_PORTAL_USE_EMBEDDINGS") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _resolve_skip_sandbox() -> bool:
    raw = (os.environ.get("MCP_PORTAL_SKIP_SANDBOX") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _resolve_rpc_bind() -> tuple[str, int]:
    host = (os.environ.get("MCP_PORTAL_RPC_BIND") or "0.0.0.0").strip() or "0.0.0.0"
    port_raw = (os.environ.get("MCP_PORTAL_RPC_PORT") or "9001").strip()
    try:
        port = int(port_raw)
    except ValueError:
        port = 9001
    return host, port


def _truncate_execute_text(s: str) -> str:
    if len(s) <= _MAX_EXECUTE_OUTPUT:
        return s
    return s[: _MAX_EXECUTE_OUTPUT - 24] + "\n...<truncated>...\n"


@asynccontextmanager
async def _portal_lifespan(_server: FastMCP) -> AsyncIterator[PortalLifespanState]:
    path = _resolve_config_path()
    use_emb = _resolve_use_embeddings()
    bind_host, bind_port = _resolve_rpc_bind()
    log.info(
        "portal_startup config=%s use_embeddings=%s rpc_bind=%s:%s",
        path,
        use_emb,
        bind_host,
        bind_port,
    )
    agg = PortalAggregator()
    await agg.load_servers(path)
    rpc = CodeModeRPC(agg)
    await rpc.start(host=bind_host, port=bind_port)
    try:
        yield PortalLifespanState(aggregator=agg, use_embeddings=use_emb, rpc=rpc)
    finally:
        await rpc.stop()
        await agg.close()
        log.info("portal_shutdown")


# --- Tool implementations (testable without FastMCP machinery) -------------------


async def search_impl(
    state: PortalLifespanState,
    query: str,
    top_k: int = 10,
) -> str:
    """Pure-Python implementation of ``portal_codemode_search``."""
    tools = state.aggregator.flatten_tools()
    hits = await search_tools(
        tools,
        query=query,
        top_k=top_k,
        use_embeddings=state.use_embeddings,
    )
    return json.dumps([h.to_json_dict() for h in hits], indent=2)


async def execute_impl(
    state: PortalLifespanState,
    code: str,
    timeout_seconds: int = 30,
    scheduled_approval_token: str | None = None,
) -> str:
    """Run TypeScript in the Deno sandbox (``docker exec``) with RPC to this portal."""
    t0 = time.monotonic()
    if _resolve_skip_sandbox():
        return json.dumps(
            {
                "stdout": "",
                "stderr": "",
                "return_value": None,
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "errors": [
                    {
                        "kind": "runtime",
                        "message": "MCP_PORTAL_SKIP_SANDBOX is set — sandbox execution disabled.",
                    }
                ],
            },
            indent=2,
        )

    rpc = state.rpc
    # Weekly additions execute one mutating call per invocation. Reserve a full
    # interactive approval window in addition to the requested execution time,
    # and pass the same budget to both the Docker runner and the RPC deadline.
    if (os.environ.get("MCP_PORTAL_AGENT_APPROVAL_URL") or "").strip():
        timeout_seconds += APPROVAL_REQUEST_TIMEOUT_SECONDS
    token, fut = rpc.register_session(
        scheduled_approval_token=scheduled_approval_token,
    )
    # Use the actual listening port so concurrent portal processes (each on
    # their own ephemeral port) route the sandbox to the right RPC server.
    actual_rpc_port = rpc.listen_port or resolve_rpc_port()
    run_task = asyncio.create_task(
        run_deno_sandbox(
            rpc,
            code,
            token,
            container=resolve_sandbox_container(),
            rpc_host=resolve_rpc_host_for_sandbox(),
            rpc_port=actual_rpc_port,
            timeout_seconds=timeout_seconds,
        ),
        name="mose_portal_deno_sandbox",
    )
    deadline = max(timeout_seconds + 25, 35)
    payload: dict[str, object]
    rc = 0
    out_b = b""
    err_b = b""
    try:
        done, _pending = await asyncio.wait(
            {fut, run_task},
            return_when=asyncio.FIRST_COMPLETED,
            timeout=deadline,
        )
        timed_out = not done
        if timed_out:
            rpc.abandon_session(token, f"Code Mode execution timed out after {timeout_seconds}s.")
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass
            rc, out_b, err_b = -1, b"", b"subprocess task cancelled after RPC timeout"
        elif run_task in done and not fut.done():
            try:
                rc_early, ob, eb = run_task.result()
            except Exception as e:  # noqa: BLE001
                rpc.abandon_session(token, repr(e)[:2000])
            else:
                msg = eb.decode("utf-8", errors="replace") or ob.decode("utf-8", errors="replace")
                rpc.abandon_session(
                    token,
                    f"sandbox exited before RPC done (code {rc_early}): {msg[:4000]}",
                )

        payload = await fut

        if not timed_out:
            try:
                if not run_task.done():
                    rc, out_b, err_b = await run_task
                else:
                    rc, out_b, err_b = run_task.result()
            except Exception as e:  # noqa: BLE001
                rc, out_b, err_b = -1, b"", repr(e).encode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        log.exception("portal_codemode_execute_failed")
        if not fut.done():
            rpc.abandon_session(token, repr(e)[:2000])
        payload = await fut
        try:
            if not run_task.done():
                rc, out_b, err_b = await asyncio.wait_for(run_task, timeout=30)
            else:
                rc, out_b, err_b = run_task.result()
        except Exception as e2:  # noqa: BLE001
            rc, out_b, err_b = -1, b"", repr(e2).encode("utf-8", errors="replace")

    extra_err = err_b.decode("utf-8", errors="replace").strip()
    extra_out = out_b.decode("utf-8", errors="replace").strip()
    if extra_err:
        sep = "\n" if payload.get("stderr") else ""
        payload["stderr"] = str(payload.get("stderr") or "") + sep + extra_err
    if rc not in (0, None) and not (payload.get("errors") or []):
        errs = list(payload.get("errors") or [])
        errs.append(
            {
                "kind": "runtime",
                "message": f"Deno/docker exited with code {rc}: {extra_err or extra_out}"[:8000],
            },
        )
        payload["errors"] = errs

    duration_ms = int((time.monotonic() - t0) * 1000)
    if not isinstance(payload, dict):
        payload = {
            "stdout": "",
            "stderr": "",
            "return_value": None,
            "errors": [{"kind": "runtime", "message": "internal error: bad execute payload"}],
        }
    payload.setdefault("stdout", "")
    payload.setdefault("stderr", "")
    payload.setdefault("return_value", None)
    payload.setdefault("errors", [])
    payload["stdout"] = _truncate_execute_text(str(payload.get("stdout") or ""))
    payload["stderr"] = _truncate_execute_text(str(payload.get("stderr") or ""))
    payload["duration_ms"] = duration_ms
    return json.dumps(payload, indent=2)


# --- FastMCP wiring --------------------------------------------------------------


def build_app() -> FastMCP:
    mcp = FastMCP("mose-mcp-portal", lifespan=_portal_lifespan)

    @mcp.tool()
    async def portal_codemode_search(ctx: Context, query: str, top_k: int = 10) -> str:
        """Search aggregated upstream MCP tools.

        Returns a JSON array of ``{name, ts_signature, description, example}`` hits.
        Each hit includes a copy-pasteable TypeScript snippet you can adapt and pass
        to ``portal_codemode_execute``.
        """
        # ``ctx`` annotated as bare ``Context`` is FastMCP's injection marker;
        # the SDK filters it from the public input schema so the LLM never sees it.
        state: PortalLifespanState = ctx.request_context.lifespan_context
        return await search_impl(state, query=query, top_k=top_k)

    @mcp.tool()
    async def portal_codemode_execute(
        ctx: Context,
        code: str,
        timeout_seconds: int = 30,
        scheduled_approval_token: str | None = None,
    ) -> str:
        """Run TypeScript in the Code Mode sandbox.

        Returns structured ``{stdout, stderr, return_value, duration_ms, errors[]}``.
        Mutating MCP tools require the agent HTTP approval bridge (``MCP_PORTAL_AGENT_APPROVAL_URL``).
        ``scheduled_approval_token`` is injected by the agent for approved scheduled tasks only — do not set manually.
        """
        state: PortalLifespanState = ctx.request_context.lifespan_context
        return await execute_impl(
            state,
            code,
            timeout_seconds,
            scheduled_approval_token=scheduled_approval_token,
        )

    return mcp
