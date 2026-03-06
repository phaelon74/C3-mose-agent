"""Thin LLM client wrapping the OpenAI-compatible API (llama-server)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import openai

from luna.config import LLMConfig
from luna.observe import get_logger, log_event, log_duration

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
        self.client = openai.AsyncOpenAI(
            base_url=config.endpoint,
            api_key="not-needed",  # llama-server doesn't require a key
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
            "temperature": temperature if temperature is not None else self.config.temperature,
        }
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
