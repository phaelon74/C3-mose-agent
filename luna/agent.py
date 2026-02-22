"""Core agent loop: receive message, retrieve memory, call LLM, execute tools, respond."""

from __future__ import annotations

import json
from typing import Any

from luna.config import Config
from luna.llm import LLMClient
from luna.memory import MemoryManager
from luna.mcp_manager import MCPManager
from luna.observe import get_logger, log_event, log_duration

logger = get_logger("agent")

SYSTEM_PROMPT_TEMPLATE = """\
You are Luna, a helpful AI assistant. You have persistent memory and access to tools.

{memory_section}
{summary_section}

Current time: {current_time}

Be concise and helpful. Use tools when they would help answer the user's question."""


def _build_system_prompt(memories: list, summary: str | None, current_time: str) -> str:
    memory_section = ""
    if memories:
        mem_lines = []
        for m in memories:
            mem_lines.append(f"- [{m.memory_type}] {m.content}")
        memory_section = "## Relevant Memories\n" + "\n".join(mem_lines)

    summary_section = ""
    if summary:
        summary_section = f"## Previous Context\n{summary}"

    return SYSTEM_PROMPT_TEMPLATE.format(
        memory_section=memory_section,
        summary_section=summary_section,
        current_time=current_time,
    )


class Agent:
    """The orchestrator that ties LLM, memory, and MCP tools together."""

    def __init__(self, config: Config, llm: LLMClient, memory: MemoryManager, mcp: MCPManager) -> None:
        self.config = config
        self.llm = llm
        self.memory = memory
        self.mcp = mcp
        self.max_tool_rounds = 10  # safety limit on tool call loops

    async def process(self, message: str, session_id: str) -> str:
        """Process a user message and return the assistant's response."""
        with log_duration(logger, "agent_process", session_id=session_id):
            return await self._process_inner(message, session_id)

    async def _process_inner(self, message: str, session_id: str) -> str:
        # 1. Save user message
        self.memory.save_message(session_id, "user", message)

        # 2. Retrieve relevant memories
        memories = self.memory.search(message, top_k=self.config.memory.top_k)
        summary = self.memory.get_session_summary(session_id)

        # 3. Build prompt
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        system = _build_system_prompt(memories, summary, now)
        recent = self.memory.get_recent_messages(session_id, limit=20)

        # 4. Get available tools
        tools = self.mcp.get_all_tools()

        # 5. Build message list
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(recent)

        # 6. Call LLM
        response = await self.llm.chat(messages, tools=tools if tools else None)

        # 7. Tool call loop
        rounds = 0
        while response.has_tool_calls() and rounds < self.max_tool_rounds:
            rounds += 1

            # Append assistant message with tool calls
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": response.content or ""}
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in response.tool_calls
            ]
            messages.append(assistant_msg)

            # Execute each tool call
            for tc in response.tool_calls:
                log_event(logger, "tool_executing", tool=tc.name, session_id=session_id)
                try:
                    result = await self.mcp.call_tool(tc.name, tc.arguments)
                except Exception as e:
                    result = f"Tool error: {e}"
                    logger.exception(f"Tool call failed: {tc.name}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            # Call LLM again with tool results
            response = await self.llm.chat(messages, tools=tools if tools else None)

        if rounds >= self.max_tool_rounds:
            log_event(logger, "tool_loop_limit", session_id=session_id, rounds=rounds)

        # 8. Save assistant response
        content = response.content or "(no response)"
        self.memory.save_message(session_id, "assistant", content)

        # 9. Periodic maintenance
        if self.memory.should_summarize(session_id):
            try:
                await self.memory.summarize_and_extract(session_id, self.llm)
            except Exception:
                logger.exception("Background summarization failed")

        log_event(logger, "agent_response", session_id=session_id,
                  memory_hits=len(memories), tool_rounds=rounds)
        return content
