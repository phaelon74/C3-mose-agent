"""Tests for inlined MCP tools in the main agent LLM tool list and dispatch."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mose.agent import Agent
from mose.config import Config, MemoryConfig
from mose.llm import LLMClient, LLMResponse, ToolCall
from mose.memory import MemoryManager
from mose.tools import execute_mcp_tool, init_approval, init_tool_registry


def _tool_entry(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "test",
            "parameters": {"type": "object", "properties": {}},
        },
    }


@pytest.fixture
def inline_agent(tmp_path):
    from mose.observe import setup_logging

    setup_logging(str(tmp_path / "logs"), "DEBUG")
    config = Config()
    config.memory.db_path = str(tmp_path / "test.db")
    config.agent.skills_path = str(tmp_path / "noskills")

    llm = MagicMock(spec=LLMClient)
    llm.chat = AsyncMock(return_value=LLMResponse(content="ok"))

    memory = MemoryManager(
        MemoryConfig(
            db_path=str(tmp_path / "test.db"),
            embedding_model="nomic-ai/nomic-embed-text-v1.5",
            embedding_dimensions=384,
        )
    )
    memory.search = MagicMock(return_value=[])

    mcp = MagicMock()
    mcp.servers = {"srv": MagicMock()}
    mcp.get_all_tools.return_value = [_tool_entry("srv__ping")]

    return Agent(config, llm, memory, mcp)


def test_build_llm_tools_merges_and_strips_meta(inline_agent):
    names = {t["function"]["name"] for t in inline_agent._build_llm_tools("sess-1")}
    assert "srv__ping" in names
    assert "bash" in names
    assert "use_tool" not in names
    assert "list_available_tools" not in names


def test_build_llm_tools_inline_off_keeps_meta(tmp_path):
    from mose.observe import setup_logging

    setup_logging(str(tmp_path / "logs"), "DEBUG")
    config = Config()
    config.memory.db_path = str(tmp_path / "test.db")
    config.agent.skills_path = str(tmp_path / "noskills")
    config.agent.inline_mcp_tools = False

    llm = MagicMock(spec=LLMClient)
    memory = MemoryManager(
        MemoryConfig(
            db_path=str(tmp_path / "test2.db"),
            embedding_model="nomic-ai/nomic-embed-text-v1.5",
            embedding_dimensions=384,
        )
    )
    memory.search = MagicMock(return_value=[])
    mcp = MagicMock()
    mcp.servers = {"srv": MagicMock()}
    mcp.get_all_tools.return_value = [_tool_entry("srv__ping")]
    agent = Agent(config, llm, memory, mcp)

    names = {t["function"]["name"] for t in agent._build_llm_tools("s")}
    assert "use_tool" in names
    assert "srv__ping" not in names


def test_build_llm_tools_server_allowlist(tmp_path):
    from mose.observe import setup_logging

    setup_logging(str(tmp_path / "logs"), "DEBUG")
    config = Config()
    config.memory.db_path = str(tmp_path / "test3.db")
    config.agent.skills_path = str(tmp_path / "noskills")
    config.agent.inline_mcp_servers = ["srv"]

    llm = MagicMock(spec=LLMClient)
    memory = MemoryManager(
        MemoryConfig(
            db_path=str(tmp_path / "test3.db"),
            embedding_model="nomic-ai/nomic-embed-text-v1.5",
            embedding_dimensions=384,
        )
    )
    memory.search = MagicMock(return_value=[])
    mcp = MagicMock()
    mcp.servers = {"a": MagicMock(), "b": MagicMock()}
    mcp.get_all_tools.return_value = [
        _tool_entry("paper_db__x"),
        _tool_entry("srv__ping"),
    ]
    agent = Agent(config, llm, memory, mcp)

    names = {t["function"]["name"] for t in agent._build_llm_tools("s")}
    assert "srv__ping" in names
    assert "paper_db__x" not in names


@pytest.mark.asyncio
async def test_process_routes_inlined_mcp_through_execute_mcp_tool(inline_agent):
    tool_response = LLMResponse(
        content=None,
        tool_calls=[ToolCall(id="t1", name="srv__ping", arguments='{"x": 1}')],
    )
    final = LLMResponse(content="Done.")
    inline_agent.llm.chat = AsyncMock(side_effect=[tool_response, final])

    with patch("mose.agent.execute_mcp_tool", new_callable=AsyncMock) as exec_mcp:
        exec_mcp.return_value = "pong"
        result = await inline_agent.process("hi", "sess-inline")

    assert result == "Done."
    exec_mcp.assert_awaited_once_with("srv__ping", {"x": 1})


@pytest.mark.asyncio
async def test_execute_mcp_tool_denies_write_without_callback():
    mcp = MagicMock()
    mcp.call_tool = AsyncMock(return_value="should not run")
    init_tool_registry(mcp)
    init_approval(None)
    try:
        out = await execute_mcp_tool("plex-ops-admin__library_scan", {})
        assert "denied" in out.lower()
        mcp.call_tool.assert_not_called()
    finally:
        init_tool_registry(None)
        init_approval(None)
