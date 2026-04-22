"""MCP registry file loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from mose.mcp_manager import MCPManager


@pytest.mark.asyncio
async def test_load_servers_returns_empty_on_duplicate_json_objects(tmp_path: Path) -> None:
    """json.load rejects 'Extra data' — startup must not crash (log + skip MCP)."""
    cfg = tmp_path / "mcp_servers.json"
    cfg.write_text('{"servers": {}}\n{"servers": {}}\n', encoding="utf-8")

    mcp = MCPManager()
    await mcp.load_servers(cfg)
    assert mcp.servers == {}
