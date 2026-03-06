"""Core agent loop: receive message, retrieve memory, call LLM, execute tools, respond."""

from __future__ import annotations

import json
from typing import Any

from luna.config import Config
from luna.llm import LLMClient
from luna.memory import MemoryManager
from luna.mcp_manager import MCPManager
from luna.observe import get_logger, log_event, log_duration
from luna.tools import NATIVE_TOOLS, is_native_tool, call_native_tool

logger = get_logger("agent")

SYSTEM_PROMPT_TEMPLATE = """\
You are Luna, an AI assistant running on Fabio's homelab. You are knowledgeable, precise, and concise. \
You think step by step on complex problems but keep routine answers brief.

## Capabilities
You have persistent memory (facts survive across sessions), access to a bash shell, the local filesystem, \
and the web. You can read/write files, run commands, search the internet, and fetch web pages. \
You are expected to use these tools proactively — do not describe what you could do, just do it.

## Tool Usage
- **bash**: System commands, git, scripts, process management. Check exit codes — non-zero means failure.
- **read_file / write_file**: File I/O. Relative paths resolve to the workspace. Writes outside workspace are blocked.
- **list_directory**: Browse the filesystem before reading specific files.
- **web_search**: Search the web via DuckDuckGo when you need current information, documentation, or facts you're unsure about.
- **web_fetch**: Fetch and read a specific URL. Use after web_search to get details from a result.
- **delegate**: Hand off a self-contained subtask to a sub-agent with its own tool loop. \
Use for multi-step research, complex file operations, or anything that benefits from focused context.
- **list_available_tools / use_tool**: Discover and call additional MCP tools beyond the built-ins.

## Guidelines
- Act, don't ask. You have tools — use them. Install packages, run commands, create files, scan networks. \
Do it and report the results. Do not ask "would you like me to..." for safe, reversible operations.
- Never tell the user to run commands manually. You have bash. Run the command yourself, read the output, \
and iterate. The user should only need to intervene for physical actions (plugging in cables, rebooting hardware).
- When something fails, try a different approach. If a package install fails, try another method. \
If a scan finds nothing, try different parameters, a different tool, or debug why. Exhaust your options \
before asking the user for help.
- When there are multiple approaches, pick the best one and do it. Explain what you chose and why \
in your response — don't present a menu of options.
- Verify before destructive actions: check before deleting, overwriting, or modifying system config. \
But reading, installing, scanning, and creating are safe — just do them.
- Break complex tasks into steps. Use tools iteratively rather than guessing.
- When you don't know something, look it up (web_search, web_fetch, read docs) rather than guessing \
or asking the user.
- For file creation, use relative paths — they resolve to the workspace below.

## Approach
When given a task that requires multiple steps (e.g., "set up X", "discover devices", "install and test Y"):
1. Research first if needed (web_search, read docs)
2. Install dependencies in the workspace venv or with pip
3. Write and run code/scripts to accomplish the task
4. If something doesn't work, debug it — read errors, try alternatives, search for solutions
5. Report what you did and what the results were

## Memory
The "Relevant Memories" section below contains facts retrieved from your long-term memory based on \
the current conversation. These may include user preferences, past decisions, project details, or \
previously learned facts. Not all retrieved memories will be relevant — use judgment.

{memory_section}
{summary_section}

Current time: {current_time}
Workspace: {workspace}"""


def _build_system_prompt(memories: list, summary: str | None, current_time: str, workspace: str = "") -> str:
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
        workspace=workspace,
    )


# Patterns that indicate tool execution problems
_ERROR_PATTERNS: list[tuple[str, str]] = [
    ("connection refused", "The target service may be down."),
    ("permission denied", "Permission issue — may need sudo or different path."),
    ("no such file or directory", "File/path does not exist."),
    ("command not found", "Command is not installed or not in PATH."),
    ("timed out", "Operation timed out — consider a longer timeout or simpler approach."),
    ("name or service not known", "DNS resolution failed — check the hostname."),
    ("disk quota exceeded", "Out of disk space."),
    ("connection timed out", "Network timeout — host may be unreachable."),
]


def _verify_tool_result(tool_name: str, result: str) -> str:
    """Check a tool result for common problems and annotate if issues found.

    Returns the result string, possibly with a [NOTE] appended.
    Pure string matching — no LLM calls.
    """
    if not result or result.strip() == "(no output)":
        return result + "\n[NOTE: Tool returned empty/no output. Verify the command was correct.]"

    result_lower = result.lower()

    # Check for non-zero exit codes in bash output
    if "(exit code:" in result_lower and "(exit code: 0)" not in result_lower:
        for pattern, hint in _ERROR_PATTERNS:
            if pattern in result_lower:
                return result + f"\n[NOTE: {hint}]"
        return result + "\n[NOTE: Command exited with non-zero status. Check the output for errors.]"

    # Check for error patterns even without exit codes (web_fetch, MCP tools, etc.)
    for pattern, hint in _ERROR_PATTERNS:
        if pattern in result_lower:
            return result + f"\n[NOTE: {hint}]"

    return result


class Agent:
    """The orchestrator that ties LLM, memory, and MCP tools together."""

    def __init__(self, config: Config, llm: LLMClient, memory: MemoryManager, mcp: MCPManager) -> None:
        self.config = config
        self.llm = llm
        self.memory = memory
        self.mcp = mcp
        self.max_tool_rounds = 25  # safety limit on tool call loops

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
        system = _build_system_prompt(memories, summary, now, self.config.agent.workspace)
        recent = self.memory.get_recent_messages(session_id, limit=20)

        # 4. Get available tools (native only; MCP tools accessed via meta-tools)
        tools = NATIVE_TOOLS

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
                    if is_native_tool(tc.name):
                        result = await call_native_tool(
                            tc.name, tc.arguments,
                            context=message, llm=self.llm,
                        )
                    else:
                        result = await self.mcp.call_tool(tc.name, tc.arguments)
                except Exception as e:
                    result = f"Tool error: {e}"
                    logger.exception(f"Tool call failed: {tc.name}")

                result = _verify_tool_result(tc.name, result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            # Call LLM again with tool results
            response = await self.llm.chat(messages, tools=tools if tools else None)

        if rounds >= self.max_tool_rounds:
            log_event(logger, "tool_loop_limit", session_id=session_id, rounds=rounds)
            # Ask the LLM to wrap up without tools
            messages.append({
                "role": "user",
                "content": "You have reached the tool call limit. Please respond to the user with what you have so far. Do not call any more tools.",
            })
            response = await self.llm.chat(messages)

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
