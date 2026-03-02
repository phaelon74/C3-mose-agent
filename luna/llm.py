"""Thin LLM client wrapping the OpenAI-compatible API (llama-server)."""

from __future__ import annotations

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
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: Any = None

    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


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
            "max_tokens": self.config.max_tokens,
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

        response = LLMResponse(
            content=choice.message.content,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            raw=raw,
        )

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
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
