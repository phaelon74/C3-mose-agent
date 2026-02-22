"""Tests for the LLM client."""

from __future__ import annotations

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
