"""Tests for the LLM client."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from luna.config import LLMConfig
from luna.llm import LLMClient, LLMResponse, ToolCall


class TestLLMResponse:
    def test_no_tool_calls(self):
        r = LLMResponse(content="Hello!")
        assert not r.has_tool_calls()
        assert r.content == "Hello!"

    def test_with_tool_calls(self):
        r = LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="1", name="test_tool", arguments='{"a": 1}')],
        )
        assert r.has_tool_calls()
        assert r.tool_calls[0].name == "test_tool"


class TestLLMClient:
    def test_client_initialization(self):
        config = LLMConfig(endpoint="http://localhost:8001/v1", model="test")
        client = LLMClient(config)
        assert client.config.model == "test"
        assert client.client.base_url.host == "localhost"


class TestTemperatureOverride:
    def _make_client(self, config_temp: float = 0.7) -> LLMClient:
        config = LLMConfig(endpoint="http://localhost:8001/v1", model="test", temperature=config_temp)
        return LLMClient(config)

    def _mock_openai_response(self):
        """Create a mock OpenAI completion response."""
        choice = MagicMock()
        choice.message.content = "response"
        choice.message.tool_calls = None
        usage = MagicMock()
        usage.prompt_tokens = 10
        usage.completion_tokens = 5
        raw = MagicMock()
        raw.choices = [choice]
        raw.usage = usage
        return raw

    @pytest.mark.asyncio
    async def test_default_temperature_from_config(self):
        client = self._make_client(config_temp=0.7)
        mock_response = self._mock_openai_response()
        client.client.chat.completions.create = AsyncMock(return_value=mock_response)

        await client.chat([{"role": "user", "content": "hi"}])

        call_kwargs = client.client.chat.completions.create.call_args[1]
        assert call_kwargs["temperature"] == 0.7

    @pytest.mark.asyncio
    async def test_temperature_override(self):
        client = self._make_client(config_temp=0.7)
        mock_response = self._mock_openai_response()
        client.client.chat.completions.create = AsyncMock(return_value=mock_response)

        await client.chat([{"role": "user", "content": "hi"}], temperature=0.2)

        call_kwargs = client.client.chat.completions.create.call_args[1]
        assert call_kwargs["temperature"] == 0.2

    @pytest.mark.asyncio
    async def test_temperature_none_uses_config(self):
        client = self._make_client(config_temp=0.9)
        mock_response = self._mock_openai_response()
        client.client.chat.completions.create = AsyncMock(return_value=mock_response)

        await client.chat([{"role": "user", "content": "hi"}], temperature=None)

        call_kwargs = client.client.chat.completions.create.call_args[1]
        assert call_kwargs["temperature"] == 0.9

    @pytest.mark.asyncio
    async def test_temperature_zero_is_valid(self):
        client = self._make_client(config_temp=0.7)
        mock_response = self._mock_openai_response()
        client.client.chat.completions.create = AsyncMock(return_value=mock_response)

        await client.chat([{"role": "user", "content": "hi"}], temperature=0.0)

        call_kwargs = client.client.chat.completions.create.call_args[1]
        assert call_kwargs["temperature"] == 0.0
