"""Tests for the agent loop."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from luna.config import Config
from luna.llm import LLMClient, LLMResponse
from luna.agent import Agent, _build_system_prompt
from luna.memory import MemoryResult, MemoryManager


class TestSystemPrompt:
    def test_empty_memories(self):
        prompt = _build_system_prompt([], None, "2026-01-01T00:00:00Z")
        assert "Luna" in prompt
        assert "2026-01-01" in prompt

    def test_with_memories(self):
        memories = [
            MemoryResult(id=1, content="User likes Python", memory_type="fact",
                         importance=7.0, score=0.9, created_at=0),
        ]
        prompt = _build_system_prompt(memories, None, "2026-01-01T00:00:00Z")
        assert "User likes Python" in prompt
        assert "[fact]" in prompt

    def test_with_summary(self):
        prompt = _build_system_prompt([], "They discussed AI.", "2026-01-01T00:00:00Z")
        assert "They discussed AI." in prompt


class TestAgent:
    @pytest.fixture
    def agent(self, tmp_path):
        from luna.config import MemoryConfig
        from luna.observe import setup_logging

        setup_logging(str(tmp_path / "logs"), "DEBUG")

        config = Config()
        config.memory.db_path = str(tmp_path / "test.db")

        llm = MagicMock(spec=LLMClient)
        llm.chat = AsyncMock(return_value=LLMResponse(content="Hello!"))

        memory = MemoryManager(MemoryConfig(
            db_path=str(tmp_path / "test.db"),
            embedding_model="nomic-ai/nomic-embed-text-v1.5",
            embedding_dimensions=384,
        ))
        # Patch search to avoid loading the embedding model in tests
        memory.search = MagicMock(return_value=[])

        mcp = MagicMock()
        mcp.get_all_tools.return_value = []

        return Agent(config, llm, memory, mcp)

    async def test_basic_response(self, agent):
        result = await agent.process("Hello", "test-session")
        assert result == "Hello!"
        agent.llm.chat.assert_called_once()

    async def test_saves_messages(self, agent):
        await agent.process("Hello", "test-session")
        messages = agent.memory.get_recent_messages("test-session")
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
