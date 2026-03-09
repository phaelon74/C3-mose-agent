"""Core agent loop: receive message, retrieve memory, call LLM, execute tools, respond."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, Callable

from mose.config import Config
from mose.llm import LLMClient
from mose.memory import MemoryManager
from mose.mcp_manager import MCPManager
from mose.observe import get_logger, log_event, log_duration
from mose.tools import NATIVE_TOOLS, is_native_tool, call_native_tool, verify_tool_result

logger = get_logger("agent")

CHARS_PER_TOKEN = 4.0  # heuristic for English/mixed content


def _estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate from message content."""
    total = 0
    for m in messages:
        content = m.get("content") or ""
        total += len(str(content))
        for tc in m.get("tool_calls", []):
            fn = tc.get("function")
            args = fn.get("arguments", "") if isinstance(fn, dict) else str(tc)
            total += len(str(args))
    return int(total / CHARS_PER_TOKEN)


def _get_message_blocks(messages: list[dict]) -> list[tuple[int, int]]:
    """Return (start, end) indices for each logical block. Preserves assistant+tool pairs."""
    blocks: list[tuple[int, int]] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        role = m.get("role")
        if role in ("system", "user"):
            blocks.append((i, i + 1))
            i += 1
        elif role == "assistant":
            tool_calls = m.get("tool_calls", [])
            if not tool_calls:
                blocks.append((i, i + 1))
                i += 1
            else:
                blocks.append((i, i + 1 + len(tool_calls)))
                i += 1 + len(tool_calls)
        elif role == "tool":
            blocks.append((i, i + 1))
            i += 1
        else:
            blocks.append((i, i + 1))
            i += 1
    return blocks


def _truncate_messages_to_fit(messages: list[dict], max_input_tokens: int) -> list[dict]:
    """Keep system + most recent message blocks that fit within max_input_tokens."""
    if not messages:
        return messages
    blocks = _get_message_blocks(messages)
    system_block = blocks[0] if blocks and messages[blocks[0][0]].get("role") == "system" else None
    rest_blocks = blocks[1:] if system_block else blocks
    if not rest_blocks:
        return messages
    system_msgs = messages[system_block[0] : system_block[1]] if system_block else []
    system_tokens = _estimate_tokens(system_msgs)
    budget = max_input_tokens - system_tokens
    if budget <= 0:
        return system_msgs
    # Keep tail of blocks that fit
    kept: list[dict] = []
    for start, end in reversed(rest_blocks):
        block_msgs = messages[start:end]
        block_tokens = _estimate_tokens(block_msgs)
        if _estimate_tokens(kept) + block_tokens <= budget:
            kept = block_msgs + kept
        else:
            break
    return system_msgs + kept

SYSTEM_PROMPT_TEMPLATE = """\
You are Mose, an AI assistant running in Cloud3's Infrastructure. You are knowledgeable, precise, and concise. \
You think step by step on complex problems but keep routine answers brief.

## Capabilities
You have persistent memory (facts survive across sessions), access to a bash shell, the local filesystem, \
and the web. You can read/write files, run commands, search the internet, and fetch web pages. \
You are expected to use these tools proactively — do not describe what you could do, just do it.

## Tool Usage
- **bash**: Read-only system commands (status, logs, queries). Use for anything that does not modify state.
- **sre_execute**: State-changing commands (restart, update, config changes). Requires human approval before running.
- **read_file / write_file**: File I/O. Relative paths resolve to the workspace. Writes outside workspace are blocked.
- **list_directory**: Browse the filesystem before reading specific files.
- **web_search**: Search the web via DuckDuckGo when you need current information, documentation, or facts you're unsure about.
- **web_fetch**: Fetch and read a specific URL. Use after web_search to get details from a result.
- **delegate**: Hand off a self-contained subtask to a sub-agent with its own tool loop. \
Use for multi-step research, complex file operations, or anything that benefits from focused context.
- **code_task**: Delegate a coding task to a sub-agent that writes code, runs it, checks results, \
and iterates on failures. Use for scripts, scrapers, automation, or any task requiring write-run-fix cycles. \
Prefer this over delegate for coding work.
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

{skills_section}

## Memory
The "Relevant Memories" section below contains facts retrieved from your long-term memory based on \
the current conversation. These may include user preferences, past decisions, project details, or \
previously learned facts. Not all retrieved memories will be relevant — use judgment.

{memory_section}
{summary_section}

Current time: {current_time}
Workspace: {workspace}"""


def _load_skills(skills_dir: Path) -> str:
    """Load and concatenate all .md files from the skills directory. Returns empty string if dir missing or empty."""
    if not skills_dir.exists() or not skills_dir.is_dir():
        return ""
    files = sorted(skills_dir.glob("*.md"), key=lambda p: (p.name != "_overview.md", p.name))
    if not files:
        return ""
    parts: list[str] = []
    for f in files:
        try:
            parts.append(f.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            logger.warning("Failed to load skill file %s: %s", f, e)
    return "\n\n---\n\n".join(parts)


def _build_system_prompt(
    memories: list,
    summary: str | None,
    current_time: str,
    workspace: str = "",
    skills_path: str = "",
) -> str:
    memory_section = ""
    if memories:
        mem_lines = []
        for m in memories:
            mem_lines.append(f"- [{m.memory_type}] {m.content}")
        memory_section = "## Relevant Memories\n" + "\n".join(mem_lines)

    summary_section = ""
    if summary:
        summary_section = f"## Previous Context\n{summary}"

    skills_section = ""
    if skills_path:
        content = _load_skills(Path(skills_path))
        if content:
            skills_section = f"\n\n## Cloud3 SRE Environment\n\n{content}\n\n"

    return SYSTEM_PROMPT_TEMPLATE.format(
        memory_section=memory_section,
        summary_section=summary_section,
        skills_section=skills_section,
        current_time=current_time,
        workspace=workspace,
    )


class Agent:
    """The orchestrator that ties LLM, memory, and MCP tools together."""

    def __init__(
        self,
        config: Config,
        llm: LLMClient,
        memory: MemoryManager,
        mcp: MCPManager,
        tool_callback: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.memory = memory
        self.mcp = mcp
        self.max_tool_rounds = 25  # safety limit on tool call loops
        self.tool_callback = tool_callback

    async def process(
        self,
        message: str,
        session_id: str,
        status_callback: Callable[[str, str], Any] | None = None,
    ) -> str:
        """Process a user message and return the assistant's response."""
        with log_duration(logger, "agent_process", session_id=session_id):
            return await self._process_inner(message, session_id, status_callback)

    async def _process_inner(self, message: str, session_id: str,
                              status_callback: Callable[[str, str], Any] | None = None) -> str:
        # 1. Save user message
        self.memory.save_message(session_id, "user", message)

        # 2. Retrieve relevant memories
        memories = self.memory.search(message, top_k=self.config.memory.top_k)
        summary = self.memory.get_session_summary(session_id)

        # 3. Build prompt
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        system = _build_system_prompt(
            memories, summary, now,
            self.config.agent.workspace,
            self.config.agent.skills_path,
        )
        recent = self.memory.get_recent_messages(
            session_id, limit=self.config.agent.recent_messages_limit
        )

        # 4. Get available tools (native only; MCP tools accessed via meta-tools)
        tools = NATIVE_TOOLS

        # 5. Build message list
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(recent)

        max_input_tokens = self.config.llm.context_window - self.config.llm.max_tokens

        # 6. Call LLM
        messages = _truncate_messages_to_fit(messages, max_input_tokens)
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

                if status_callback is not None:
                    try:
                        ret = status_callback(tc.name, tc.arguments)
                        if inspect.isawaitable(ret):
                            await ret
                    except Exception:
                        logger.debug("Status callback failed", exc_info=True)

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

                result = verify_tool_result(tc.name, result)
                if self.tool_callback is not None:
                    self.tool_callback(tc.name, tc.arguments, result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            # Call LLM again with tool results
            messages = _truncate_messages_to_fit(messages, max_input_tokens)
            response = await self.llm.chat(messages, tools=tools if tools else None)

        if rounds >= self.max_tool_rounds:
            log_event(logger, "tool_loop_limit", session_id=session_id, rounds=rounds)
            # Ask the LLM to wrap up without tools
            messages.append({
                "role": "user",
                "content": "You have reached the tool call limit. Please respond to the user with what you have so far. Do not call any more tools.",
            })
            messages = _truncate_messages_to_fit(messages, max_input_tokens)
            response = await self.llm.chat(messages)

        # 8. Thinking retry — model produced only reasoning after tool use
        content = response.content
        if not content and rounds > 0 and response.reasoning_content:
            log_event(logger, "thinking_retry", session_id=session_id, rounds=rounds,
                      reasoning_len=len(response.reasoning_content))
            messages.append({
                "role": "user",
                "content": (
                    "You used tools and got results, but your last response was empty. "
                    "Please summarize what you found and answer the user's question."
                ),
            })
            messages = _truncate_messages_to_fit(messages, max_input_tokens)
            response = await self.llm.chat(messages)  # no tools — forces text
            content = response.content

        content = content or "(no response)"

        # 9. Save assistant response
        self.memory.save_message(session_id, "assistant", content)

        # 10. Periodic maintenance
        if self.memory.should_summarize(session_id):
            try:
                await self.memory.summarize_and_extract(session_id, self.llm)
            except Exception:
                logger.exception("Background summarization failed")

        log_event(logger, "agent_response", session_id=session_id,
                  memory_hits=len(memories), tool_rounds=rounds)
        return content
