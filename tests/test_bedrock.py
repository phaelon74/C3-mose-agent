"""Tests for the hidden Bedrock backend."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from mose.config import LLMConfig
from mose.llm import (
    BedrockClient,
    LLMClient,
    LLMResponse,
    ToolCall,
    _bedrock_response_to_llm_response,
    _is_bedrock_endpoint,
    _openai_messages_to_bedrock,
    _openai_tools_to_bedrock,
    _parse_bedrock_region,
    create_llm_client,
)


# ---------------------------------------------------------------------------
# _is_bedrock_endpoint
# ---------------------------------------------------------------------------


class TestIsBedrockEndpoint:
    def test_bare_bedrock(self):
        assert _is_bedrock_endpoint("bedrock") is True

    def test_bedrock_with_region(self):
        assert _is_bedrock_endpoint("bedrock://us-east-1") is True

    def test_case_insensitive(self):
        assert _is_bedrock_endpoint("BEDROCK") is True
        assert _is_bedrock_endpoint("Bedrock://eu-west-1") is True

    def test_normal_endpoint(self):
        assert _is_bedrock_endpoint("http://localhost:8001/v1") is False

    def test_bedrock_substring(self):
        assert _is_bedrock_endpoint("http://bedrock.example.com") is False


# ---------------------------------------------------------------------------
# _parse_bedrock_region
# ---------------------------------------------------------------------------


class TestParseBedrockRegion:
    def test_with_region(self):
        assert _parse_bedrock_region("bedrock://us-east-1") == "us-east-1"

    def test_with_region_trailing_slash(self):
        assert _parse_bedrock_region("bedrock://eu-west-1/") == "eu-west-1"

    def test_bare_bedrock(self):
        assert _parse_bedrock_region("bedrock") is None

    def test_empty_region(self):
        assert _parse_bedrock_region("bedrock://") is None


# ---------------------------------------------------------------------------
# _openai_messages_to_bedrock
# ---------------------------------------------------------------------------


class TestOpenAIMessagesToBedrock:
    def test_system_extraction(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        system, msgs = _openai_messages_to_bedrock(messages)
        assert system == [{"text": "You are helpful."}]
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_user_assistant_conversion(self):
        messages = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        _, msgs = _openai_messages_to_bedrock(messages)
        assert msgs[0] == {"role": "user", "content": [{"text": "Hi"}]}
        assert msgs[1] == {"role": "assistant", "content": [{"text": "Hello!"}]}

    def test_tool_calls_conversion(self):
        messages = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command": "ls"}',
                        },
                    }
                ],
            }
        ]
        _, msgs = _openai_messages_to_bedrock(messages)
        assert len(msgs) == 1
        block = msgs[0]["content"][0]
        assert "toolUse" in block
        assert block["toolUse"]["toolUseId"] == "call_1"
        assert block["toolUse"]["name"] == "bash"
        assert block["toolUse"]["input"] == {"command": "ls"}

    def test_assistant_with_content_and_tool_calls(self):
        messages = [
            {
                "role": "assistant",
                "content": "Let me check.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                    }
                ],
            }
        ]
        _, msgs = _openai_messages_to_bedrock(messages)
        assert len(msgs[0]["content"]) == 2
        assert msgs[0]["content"][0] == {"text": "Let me check."}
        assert "toolUse" in msgs[0]["content"][1]

    def test_tool_result_conversion(self):
        messages = [
            {"role": "tool", "tool_call_id": "call_1", "content": "file1.txt\nfile2.txt"},
        ]
        _, msgs = _openai_messages_to_bedrock(messages)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        block = msgs[0]["content"][0]
        assert block["toolResult"]["toolUseId"] == "call_1"
        assert block["toolResult"]["content"] == [{"text": "file1.txt\nfile2.txt"}]

    def test_consecutive_tool_results_batched(self):
        messages = [
            {"role": "tool", "tool_call_id": "call_1", "content": "result1"},
            {"role": "tool", "tool_call_id": "call_2", "content": "result2"},
        ]
        _, msgs = _openai_messages_to_bedrock(messages)
        # Should be batched into a single user message
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert len(msgs[0]["content"]) == 2
        assert msgs[0]["content"][0]["toolResult"]["toolUseId"] == "call_1"
        assert msgs[0]["content"][1]["toolResult"]["toolUseId"] == "call_2"

    def test_multiple_system_messages(self):
        messages = [
            {"role": "system", "content": "Part 1"},
            {"role": "system", "content": "Part 2"},
            {"role": "user", "content": "Go"},
        ]
        system, msgs = _openai_messages_to_bedrock(messages)
        assert len(system) == 2
        assert len(msgs) == 1


# ---------------------------------------------------------------------------
# _openai_tools_to_bedrock
# ---------------------------------------------------------------------------


class TestOpenAIToolsToBedrock:
    def test_basic_conversion(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "Run a shell command",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "Command to run"},
                        },
                        "required": ["command"],
                    },
                },
            }
        ]
        result = _openai_tools_to_bedrock(tools)
        assert len(result) == 1
        spec = result[0]["toolSpec"]
        assert spec["name"] == "bash"
        assert spec["description"] == "Run a shell command"
        assert spec["inputSchema"]["json"]["type"] == "object"

    def test_multiple_tools(self):
        tools = [
            {"type": "function", "function": {"name": "a", "description": "A", "parameters": {}}},
            {"type": "function", "function": {"name": "b", "description": "B", "parameters": {}}},
        ]
        result = _openai_tools_to_bedrock(tools)
        assert len(result) == 2
        assert result[0]["toolSpec"]["name"] == "a"
        assert result[1]["toolSpec"]["name"] == "b"

    def test_missing_description(self):
        tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
        result = _openai_tools_to_bedrock(tools)
        assert result[0]["toolSpec"]["description"] == ""


# ---------------------------------------------------------------------------
# _bedrock_response_to_llm_response
# ---------------------------------------------------------------------------


class TestBedrockResponseToLLMResponse:
    def test_text_response(self):
        raw = {
            "output": {"message": {"content": [{"text": "Hello!"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }
        resp = _bedrock_response_to_llm_response(raw)
        assert resp.content == "Hello!"
        assert resp.tool_calls == []
        assert resp.prompt_tokens == 10
        assert resp.completion_tokens == 5

    def test_tool_use_response(self):
        raw = {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "call_abc",
                                "name": "bash",
                                "input": {"command": "ls -la"},
                            }
                        }
                    ]
                }
            },
            "usage": {"inputTokens": 20, "outputTokens": 15},
        }
        resp = _bedrock_response_to_llm_response(raw)
        assert resp.content is None
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].id == "call_abc"
        assert resp.tool_calls[0].name == "bash"
        assert json.loads(resp.tool_calls[0].arguments) == {"command": "ls -la"}

    def test_mixed_response(self):
        raw = {
            "output": {
                "message": {
                    "content": [
                        {"text": "Let me check."},
                        {
                            "toolUse": {
                                "toolUseId": "call_1",
                                "name": "read_file",
                                "input": {"path": "/tmp/test"},
                            }
                        },
                    ]
                }
            },
            "usage": {"inputTokens": 30, "outputTokens": 25},
        }
        resp = _bedrock_response_to_llm_response(raw)
        assert resp.content == "Let me check."
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "read_file"

    def test_missing_usage(self):
        raw = {"output": {"message": {"content": [{"text": "ok"}]}}}
        resp = _bedrock_response_to_llm_response(raw)
        assert resp.prompt_tokens == 0
        assert resp.completion_tokens == 0

    def test_multiple_text_blocks(self):
        raw = {
            "output": {
                "message": {
                    "content": [{"text": "Line 1"}, {"text": "Line 2"}]
                }
            },
            "usage": {},
        }
        resp = _bedrock_response_to_llm_response(raw)
        assert resp.content == "Line 1\nLine 2"

    def test_raw_preserved(self):
        raw = {"output": {"message": {"content": []}}, "usage": {}}
        resp = _bedrock_response_to_llm_response(raw)
        assert resp.raw is raw


# ---------------------------------------------------------------------------
# create_llm_client factory
# ---------------------------------------------------------------------------


class TestCreateLLMClient:
    def test_returns_llm_client_for_normal_endpoint(self):
        config = LLMConfig(endpoint="http://localhost:8001/v1")
        client = create_llm_client(config)
        assert isinstance(client, LLMClient)

    @patch.dict(sys.modules, {"boto3": MagicMock()})
    def test_returns_bedrock_client_for_bedrock(self):
        config = LLMConfig(endpoint="bedrock://us-east-1", model="us.anthropic.claude-sonnet-4-20250514")
        client = create_llm_client(config)
        assert isinstance(client, BedrockClient)

    @patch.dict(sys.modules, {"boto3": MagicMock()})
    def test_returns_bedrock_client_for_bare_bedrock(self):
        config = LLMConfig(endpoint="bedrock", model="us.anthropic.claude-sonnet-4-20250514")
        client = create_llm_client(config)
        assert isinstance(client, BedrockClient)


# ---------------------------------------------------------------------------
# BedrockClient init
# ---------------------------------------------------------------------------


class TestBedrockClientInit:
    def test_missing_boto3_raises(self):
        # Temporarily hide boto3
        with patch.dict(sys.modules, {"boto3": None}):
            config = LLMConfig(endpoint="bedrock", model="test-model")
            with pytest.raises(ImportError, match="boto3 is required"):
                BedrockClient(config)

    @patch.dict(sys.modules, {"boto3": MagicMock()})
    def test_region_passed_to_boto3(self):
        mock_boto3 = sys.modules["boto3"]
        mock_boto3.client = MagicMock()
        config = LLMConfig(endpoint="bedrock://eu-west-1", model="test-model")
        BedrockClient(config)
        mock_boto3.client.assert_called_once_with("bedrock-runtime", region_name="eu-west-1")

    @patch.dict(sys.modules, {"boto3": MagicMock()})
    def test_no_region_for_bare_bedrock(self):
        mock_boto3 = sys.modules["boto3"]
        mock_boto3.client = MagicMock()
        config = LLMConfig(endpoint="bedrock", model="test-model")
        BedrockClient(config)
        mock_boto3.client.assert_called_once_with("bedrock-runtime")


# ---------------------------------------------------------------------------
# BedrockClient.chat (async)
# ---------------------------------------------------------------------------


class TestBedrockClientChat:
    @pytest.fixture
    def bedrock_client(self):
        with patch.dict(sys.modules, {"boto3": MagicMock()}):
            mock_boto3 = sys.modules["boto3"]
            mock_bedrock = MagicMock()
            mock_boto3.client.return_value = mock_bedrock
            config = LLMConfig(endpoint="bedrock://us-east-1", model="us.anthropic.claude-sonnet-4-20250514")
            client = BedrockClient(config)
            client._mock_bedrock = mock_bedrock
            return client

    async def test_basic_chat(self, bedrock_client):
        bedrock_client._mock_bedrock.converse.return_value = {
            "output": {"message": {"content": [{"text": "Hello!"}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }
        messages = [{"role": "user", "content": "Hi"}]
        resp = await bedrock_client.chat(messages)
        assert resp.content == "Hello!"
        assert resp.prompt_tokens == 10
        bedrock_client._mock_bedrock.converse.assert_called_once()

    async def test_chat_with_tools(self, bedrock_client):
        bedrock_client._mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": "call_1",
                                "name": "bash",
                                "input": {"command": "echo hi"},
                            }
                        }
                    ]
                }
            },
            "usage": {"inputTokens": 50, "outputTokens": 30},
        }
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "Run command",
                    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                },
            }
        ]
        resp = await bedrock_client.chat(
            [{"role": "user", "content": "run ls"}],
            tools=tools,
        )
        assert resp.has_tool_calls()
        assert resp.tool_calls[0].name == "bash"

        # Verify toolConfig was passed
        call_kwargs = bedrock_client._mock_bedrock.converse.call_args
        assert "toolConfig" in call_kwargs.kwargs

    async def test_chat_propagates_exception(self, bedrock_client):
        bedrock_client._mock_bedrock.converse.side_effect = RuntimeError("AWS error")
        with pytest.raises(RuntimeError, match="AWS error"):
            await bedrock_client.chat([{"role": "user", "content": "Hi"}])
