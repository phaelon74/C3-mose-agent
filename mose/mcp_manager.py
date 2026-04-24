"""MCP client manager: connect to servers, discover tools, route calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mose.observe import get_logger, log_event, log_duration

logger = get_logger("mcp")


class MCPServer:
    """A single connected MCP server."""

    def __init__(self, name: str, session: ClientSession, read, write) -> None:
        self.name = name
        self.session = session
        self._read = read
        self._write = write
        self.tools: list[dict[str, Any]] = []

    async def initialize(self) -> None:
        await self.session.initialize()
        await self.refresh_tools()

    async def refresh_tools(self) -> None:
        result = await self.session.list_tools()
        self.tools = []
        for tool in result.tools:
            self.tools.append({
                "name": f"{self.name}__{tool.name}",
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
                "_server": self.name,
                "_tool_name": tool.name,
            })
        log_event(logger, "tools_refreshed", server=self.name, tool_count=len(self.tools))

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Call a tool on this server. Returns ``(text, is_mcp_error)`` where ``is_mcp_error`` mirrors SDK ``isError``."""
        with log_duration(logger, "tool_call", server=self.name, tool=tool_name):
            result = await self.session.call_tool(tool_name, arguments)

        # Concatenate text content from result
        parts = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            else:
                parts.append(str(block))

        text = "\n".join(parts)
        if result.isError:
            log_event(logger, "tool_error", server=self.name, tool=tool_name, error=text[:200])
        return text, bool(result.isError)


class MCPManager:
    """Manages connections to multiple MCP servers."""

    def __init__(self) -> None:
        self.servers: dict[str, MCPServer] = {}
        self._contexts: list[Any] = []  # keep async context managers alive
        # Per-server config retained so we can reconnect after a stdio crash
        # (anyio.ClosedResourceError, BrokenResourceError, etc.).
        self._server_configs: dict[str, dict[str, Any]] = {}
        # Per-server context-manager slots so a reconnect only tears down the
        # affected server's (stdio_client, ClientSession) pair, not the whole
        # process.
        self._server_contexts: dict[str, list[Any]] = {}

    async def load_servers(self, config_path: Path) -> None:
        """Load and connect to MCP servers from config file."""
        if not config_path.exists():
            log_event(logger, "no_mcp_config", path=str(config_path))
            return

        try:
            with open(config_path, encoding="utf-8-sig") as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            log_event(
                logger,
                "mcp_config_invalid_json",
                path=str(config_path),
                error=str(e),
            )
            logger.error(
                "Invalid JSON in %s: %s. If the error is 'Extra data', the file likely contains "
                "more than one top-level object (e.g. a second copy of { \"servers\": { ... } }). "
                "Keep a single object, or use mcp_servers.example.json as a template.",
                config_path,
                e,
            )
            return

        for name, server_config in config.get("servers", {}).items():
            try:
                await self._connect_server(name, server_config)
            except Exception:
                logger.exception(f"Failed to connect MCP server: {name}")

    async def _connect_server(self, name: str, config: dict[str, Any]) -> None:
        transport = config.get("transport", "stdio")
        if transport != "stdio":
            log_event(logger, "unsupported_transport", server=name, transport=transport)
            return

        params = StdioServerParameters(
            command=config["command"],
            args=config.get("args", []),
            env=config.get("env"),
        )

        ctx = stdio_client(params)
        read, write = await ctx.__aenter__()
        self._contexts.append(ctx)

        session = ClientSession(read, write)
        await session.__aenter__()
        self._contexts.append(session)

        server = MCPServer(name, session, read, write)
        await server.initialize()
        self.servers[name] = server
        # Track config + per-server contexts so we can reconnect after a crash.
        self._server_configs[name] = config
        self._server_contexts[name] = [ctx, session]

        log_event(logger, "server_connected", server=name, tools=len(server.tools))

    async def _close_server(self, name: str) -> None:
        """Tear down a single server's contexts without touching the others."""
        ctxs = self._server_contexts.pop(name, [])
        for ctx in reversed(ctxs):
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:
                pass
            # Remove from the shared list too so close() doesn't double-exit.
            try:
                self._contexts.remove(ctx)
            except ValueError:
                pass
        self.servers.pop(name, None)

    async def _reconnect(self, name: str) -> bool:
        """Rebuild a dead MCP server. Returns True on success."""
        config = self._server_configs.get(name)
        if config is None:
            return False
        await self._close_server(name)
        try:
            await self._connect_server(name, config)
            log_event(logger, "server_reconnected", server=name)
            return True
        except Exception as e:  # noqa: BLE001
            log_event(logger, "server_reconnect_failed", server=name, error=str(e)[:200])
            logger.exception("Failed to reconnect MCP server: %s", name)
            return False

    def get_all_tools(self) -> list[dict[str, Any]]:
        """Get all tools from all servers in OpenAI function-calling format."""
        tools = []
        for server in self.servers.values():
            for tool in server.tools:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["input_schema"],
                    },
                })
        return tools

    def _resolve_tool(self, full_name: str) -> tuple[MCPServer, str] | None:
        """Resolve a namespaced tool name to (server, original_tool_name)."""
        for server in self.servers.values():
            for tool in server.tools:
                if tool["name"] == full_name:
                    return server, tool["_tool_name"]
        return None

    async def call_tool(self, full_name: str, arguments: str | dict) -> tuple[str, bool]:
        """Route a tool call to the correct MCP server.

        If the target server's stdio session has been torn down (e.g. a prior
        unhandled exception inside a FastMCP tool), automatically reconnect and
        retry the call once. Without this, a single sidecar crash would poison
        all subsequent calls in the parent process with ``ClosedResourceError``.

        Returns ``(text, is_mcp_error)``; ``is_mcp_error`` is True only when the
        MCP SDK marks the tool result as an error (e.g. schema validation).
        """
        resolved = self._resolve_tool(full_name)
        if resolved is None:
            return f"Error: Unknown tool '{full_name}'", False

        server, tool_name = resolved
        if isinstance(arguments, str):
            arguments = json.loads(arguments)

        try:
            return await server.call_tool(tool_name, arguments)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError) as e:
            log_event(
                logger,
                "mcp_session_dead",
                server=server.name,
                tool=tool_name,
                error=type(e).__name__,
            )
            reconnected = await self._reconnect(server.name)
            if not reconnected:
                return json.dumps({
                    "error": "mcp_server_unavailable",
                    "server": server.name,
                    "tool": tool_name,
                    "detail": f"MCP session closed ({type(e).__name__}) and reconnect failed",
                }), False
            # Re-resolve against the newly rebuilt server.
            resolved2 = self._resolve_tool(full_name)
            if resolved2 is None:
                return json.dumps({
                    "error": "mcp_tool_missing_after_reconnect",
                    "server": server.name,
                    "tool": tool_name,
                }), False
            server2, tool_name2 = resolved2
            try:
                return await server2.call_tool(tool_name2, arguments)
            except Exception as e2:  # noqa: BLE001
                return json.dumps({
                    "error": "mcp_retry_failed",
                    "server": server2.name,
                    "tool": tool_name2,
                    "detail": repr(e2)[:500],
                }), False

    async def close(self) -> None:
        """Shut down all MCP server connections."""
        for ctx in reversed(self._contexts):
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:
                pass
        self.servers.clear()
        self._contexts.clear()
        log_event(logger, "mcp_shutdown")
