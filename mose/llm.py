"""Thin LLM client wrapping the OpenAI-compatible API (llama-server)."""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

import openai

from mose.config import LLMConfig
from mose.observe import get_logger, log_event, log_duration

logger = get_logger("llm")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string


@dataclass
class LLMResponse:
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: Any = None

    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


_TOOL_CALL_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL)
_FUNCTION_CALL_RE = re.compile(r"<function=\w+>.*?</function>", re.DOTALL)
_THINKING_TAG_RE = re.compile(r"</?thinking>")


def _clean_reasoning(text: str) -> str:
    """Strip leaked tool-call markup and thinking tags from reasoning text.

    Thinking models sometimes emit tool calls as plain text in reasoning_content
    instead of using structured tool calling. This removes that markup so we
    don't send raw XML to the user.
    """
    text = _TOOL_CALL_RE.sub("", text)
    text = _FUNCTION_CALL_RE.sub("", text)
    text = _THINKING_TAG_RE.sub("", text)
    return text.strip()


class LLMClient:
    """Single point of contact with the LLM. AI firewall insertion point."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        api_key = (config.api_key or os.environ.get("LLM_API_KEY") or "").strip()
        if not api_key:
            api_key = "not-needed"  # local vLLM / llama-server often need no key
        self.client = openai.AsyncOpenAI(
            base_url=config.endpoint,
            api_key=api_key,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Send a chat completion request. Returns structured response."""
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "max_completion_tokens": self.config.max_tokens,
        }
        if not self.config.omit_temperature:
            kwargs["temperature"] = (
                temperature if temperature is not None else self.config.temperature
            )
        if tools:
            kwargs["tools"] = tools

        tool_names = [t["function"]["name"] for t in (tools or [])]

        with log_duration(logger, "llm_call", model=self.config.model, tools_available=len(tool_names)):
            try:
                raw = await self.client.chat.completions.create(**kwargs)
            except Exception:
                logger.exception("LLM call failed")
                raise

        choice = raw.choices[0]
        usage = raw.usage
        msg = choice.message

        # Extract reasoning_content (thinking models like Qwen3.5)
        reasoning = getattr(msg, "reasoning_content", None)
        # Also check raw dict — some backends put it there instead
        if not reasoning and hasattr(msg, "model_extra"):
            reasoning = (msg.model_extra or {}).get("reasoning_content")

        content = msg.content
        has_tool_calls = bool(msg.tool_calls)
        finished_early = choice.finish_reason == "length"

        # Fall back to reasoning when the model produced thinking but no visible content
        # and didn't call any tools. This happens when the thinking model either:
        # - ran out of tokens during thinking (finish_reason="length")
        # - produced an empty response after thinking (finish_reason="stop")
        if not content and not has_tool_calls and reasoning:
            cleaned = _clean_reasoning(reasoning)
            if cleaned:
                content = cleaned
            else:
                # Reasoning was entirely tool-call markup with no useful text
                content = "(The model produced only internal reasoning with no response. Please try again.)"
            log_event(logger, "thinking_fallback", reasoning_len=len(reasoning),
                      finish_reason=choice.finish_reason)

        # Sanitize content — strip any leaked tool-call / thinking markup
        if content:
            content = _clean_reasoning(content) or content

        response = LLMResponse(
            content=content,
            reasoning_content=reasoning,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            raw=raw,
        )

        if msg.tool_calls:
            for tc in msg.tool_calls:
                response.tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=tc.function.arguments,
                    )
                )

        log_event(
            logger,
            "llm_response",
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            has_tool_calls=response.has_tool_calls(),
            tools_called=[tc.name for tc in response.tool_calls],
        )

        return response


# ---------------------------------------------------------------------------
# AWS Bedrock backend (hidden easter egg)
# ---------------------------------------------------------------------------


def _is_bedrock_endpoint(endpoint: str) -> bool:
    """Return True if the endpoint string activates the Bedrock backend."""
    return endpoint.lower().startswith("bedrock")


def _parse_bedrock_region(endpoint: str) -> str | None:
    """Extract AWS region from 'bedrock://us-east-1', or None for bare 'bedrock'."""
    if "://" in endpoint:
        region = endpoint.split("://", 1)[1].strip().rstrip("/")
        return region or None
    return None


def _openai_messages_to_bedrock(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert OpenAI-format messages to Bedrock Converse format.

    Returns (system_prompts, bedrock_messages).
    """
    system_prompts: list[dict[str, Any]] = []
    bedrock_messages: list[dict[str, Any]] = []

    for msg in messages:
        role = msg["role"]

        if role == "system":
            system_prompts.append({"text": msg["content"]})
            continue

        if role == "assistant":
            content_blocks: list[dict[str, Any]] = []
            if msg.get("content"):
                content_blocks.append({"text": msg["content"]})
            for tc in msg.get("tool_calls", []):
                fn = tc["function"]
                content_blocks.append({
                    "toolUse": {
                        "toolUseId": tc["id"],
                        "name": fn["name"],
                        "input": json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"],
                    }
                })
            if content_blocks:
                bedrock_messages.append({"role": "assistant", "content": content_blocks})
            continue

        if role == "tool":
            tool_result_block = {
                "toolResult": {
                    "toolUseId": msg["tool_call_id"],
                    "content": [{"text": msg["content"]}],
                }
            }
            # Batch consecutive tool results into a single user message
            if bedrock_messages and bedrock_messages[-1]["role"] == "user" and any(
                "toolResult" in b for b in bedrock_messages[-1]["content"]
            ):
                bedrock_messages[-1]["content"].append(tool_result_block)
            else:
                bedrock_messages.append({"role": "user", "content": [tool_result_block]})
            continue

        # user messages
        bedrock_messages.append({"role": "user", "content": [{"text": msg["content"]}]})

    return system_prompts, bedrock_messages


def _openai_tools_to_bedrock(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI function-calling tool definitions to Bedrock toolSpec format."""
    bedrock_tools: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool["function"]
        spec: dict[str, Any] = {
            "name": fn["name"],
            "description": fn.get("description", ""),
            "inputSchema": {"json": fn.get("parameters", {})},
        }
        bedrock_tools.append({"toolSpec": spec})
    return bedrock_tools


def _bedrock_response_to_llm_response(response: dict[str, Any]) -> LLMResponse:
    """Convert a Bedrock Converse API response to an LLMResponse."""
    output = response.get("output", {})
    message = output.get("message", {})
    content_blocks = message.get("content", [])

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for block in content_blocks:
        if "text" in block:
            text_parts.append(block["text"])
        elif "toolUse" in block:
            tu = block["toolUse"]
            tool_calls.append(ToolCall(
                id=tu["toolUseId"],
                name=tu["name"],
                arguments=json.dumps(tu["input"]),
            ))

    usage = response.get("usage", {})

    return LLMResponse(
        content="\n".join(text_parts) if text_parts else None,
        tool_calls=tool_calls,
        prompt_tokens=usage.get("inputTokens", 0),
        completion_tokens=usage.get("outputTokens", 0),
        raw=response,
    )


class BedrockClient:
    """LLM client using AWS Bedrock Converse API."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        try:
            import boto3
        except ImportError:
            raise ImportError(
                "boto3 is required for the Bedrock backend. "
                "Install it with: pip install mose-agent[cloud]"
            )
        region = _parse_bedrock_region(config.endpoint)
        kwargs: dict[str, Any] = {}
        if region:
            kwargs["region_name"] = region
        self._bedrock = boto3.client("bedrock-runtime", **kwargs)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Send a chat request via Bedrock Converse API."""
        system_prompts, bedrock_messages = _openai_messages_to_bedrock(messages)

        kwargs: dict[str, Any] = {
            "modelId": self.config.model,
            "messages": bedrock_messages,
        }
        if system_prompts:
            kwargs["system"] = system_prompts
        if tools:
            kwargs["toolConfig"] = {"tools": _openai_tools_to_bedrock(tools)}

        inference_cfg: dict[str, Any] = {"maxTokens": self.config.max_tokens}
        if not self.config.omit_temperature:
            inference_cfg["temperature"] = (
                temperature if temperature is not None else self.config.temperature
            )
        kwargs["inferenceConfig"] = inference_cfg

        tool_names = [t["function"]["name"] for t in (tools or [])]

        with log_duration(logger, "llm_call", model=self.config.model, tools_available=len(tool_names)):
            try:
                raw = await asyncio.to_thread(self._bedrock.converse, **kwargs)
            except Exception:
                logger.exception("Bedrock call failed")
                raise

        response = _bedrock_response_to_llm_response(raw)

        log_event(
            logger,
            "llm_response",
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            has_tool_calls=response.has_tool_calls(),
            tools_called=[tc.name for tc in response.tool_calls],
        )

        return response


def create_llm_client(config: LLMConfig) -> LLMClient | BedrockClient:
    """Factory: return the right LLM client based on the endpoint string."""
    if _is_bedrock_endpoint(config.endpoint):
        return BedrockClient(config)
    return LLMClient(config)
