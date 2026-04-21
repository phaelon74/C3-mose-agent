"""Core agent loop: receive message, retrieve memory, call LLM, execute tools, respond."""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any, Callable

from mose.config import Config, LearningConfig
from mose.learning import SkillLearner
from mose.llm import LLMClient
from mose.memory import MemoryManager
from mose.mcp_manager import MCPManager
from mose.observe import get_logger, log_event, log_duration
from mose.tools import (
    NATIVE_TOOLS,
    call_native_tool,
    execute_mcp_tool,
    is_native_tool,
    verify_tool_result,
)

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


def _coerce_tool_arguments(raw: Any) -> dict[str, Any]:
    """Normalize LLM tool-call arguments to a dict (OpenAI wire format uses JSON string)."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return {}
    return {}


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
- **load_skill**: Load full text of one domain skill by name when using condensed skill index (level_0).
- **web_search**: Search the web via DuckDuckGo when you need current information, documentation, or facts you're unsure about.
- **web_fetch**: Fetch and read a specific URL. Use after web_search to get details from a result.
- **delegate**: Hand off a self-contained subtask to a sub-agent with its own tool loop. \
Use for multi-step research, complex file operations, or anything that benefits from focused context.
- **code_task**: Delegate a coding task to a sub-agent that writes code, runs it, checks results, \
and iterates on failures. Use for scripts, scrapers, automation, or any task requiring write-run-fix cycles. \
Prefer this over delegate for coding work.
- **Integrated backends (MCP)**: Tools named ``server__tool`` (for example ``plex-ops-admin__sessions_get_active``) \
call Plex, Sonarr, Radarr, and other MCP backends. **Prefer these** for those systems. Do not use ``bash``/``curl`` \
to reach those services — credentials and policy are not available in the shell.
- **list_available_tools / use_tool**: When these appear in your tool list (MCP inlining disabled in config), \
use them to discover or call MCP tools by name.

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


def _skill_blurb(text: str, limit: int = 240) -> str:
    """First heading or paragraph for level_0 index."""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()[:limit]
        if s and not s.startswith("---"):
            return s[:limit]
    return ""


def _load_skills(skills_dir: Path, mode: str = "full") -> str:
    """Load skills: full concatenation, or level_0 (overview + one-line index + load_skill for detail)."""
    if not skills_dir.exists() or not skills_dir.is_dir():
        return ""
    if mode == "level_0":
        overview_path = skills_dir / "_overview.md"
        overview = ""
        if overview_path.is_file():
            try:
                overview = overview_path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                logger.warning("Failed to load _overview: %s", e)
        index_lines: list[str] = []
        files = sorted(
            skills_dir.glob("*.md"),
            key=lambda p: (p.name == "_overview.md", p.name),
        )
        for f in files:
            if f.name == "_overview.md":
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                logger.warning("Failed to read skill file %s: %s", f, e)
                continue
            blurb = _skill_blurb(text)
            index_lines.append(f"- **{f.stem}** — {blurb or '(see load_skill)'}")
        index_block = "\n".join(index_lines) if index_lines else ""
        parts = [overview]
        if index_block:
            parts.append("### Skill index (use `load_skill` with the basename for full content)\n\n" + index_block)
        return "\n\n".join(p for p in parts if p.strip())

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
    learning: LearningConfig | None = None,
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
        mode = "full"
        if learning and getattr(learning, "skill_loading_mode", "full") == "level_0":
            mode = "level_0"
        content = _load_skills(Path(skills_path), mode=mode)
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
        self._skill_learner = SkillLearner(
            config.learning,
            Path(config.agent.skills_path),
            log_dir=Path(config.learning.review_log_dir),
            proposal_timeout_seconds=int(config.signal.proposal_timeout_seconds),
            build_grace_window_seconds=int(
                getattr(config.learning, "build_grace_window_seconds", 900)
            ),
        )
        self._review_task: asyncio.Task[Any] | None = None
        # Log tool_list_over_cap at most once per session_id.
        self._tool_list_cap_logged_sessions: set[str] = set()

    def _build_llm_tools(self, session_id: str) -> list[dict[str, Any]]:
        """Native tools plus inlined MCP tools (local list; never mutates ``NATIVE_TOOLS``)."""
        cfg = self.config.agent
        llm_tools: list[dict[str, Any]] = list(NATIVE_TOOLS)
        mcp_added = 0
        if cfg.inline_mcp_tools and self.mcp.servers:
            mcp_tools = self.mcp.get_all_tools()
            allowed = [s.strip() for s in cfg.inline_mcp_servers if str(s).strip()]
            if allowed:
                allowed_set = frozenset(allowed)
                filtered: list[dict[str, Any]] = []
                for entry in mcp_tools:
                    name = str(entry.get("function", {}).get("name", ""))
                    if "__" not in name:
                        continue
                    server = name.split("__", 1)[0].strip()
                    if server in allowed_set:
                        filtered.append(entry)
                mcp_tools = filtered
            llm_tools.extend(mcp_tools)
            mcp_added = len(mcp_tools)
        if mcp_added > 0:
            meta = frozenset({"list_available_tools", "use_tool"})
            llm_tools = [t for t in llm_tools if t.get("function", {}).get("name") not in meta]
        cap = cfg.inline_mcp_tools_soft_cap
        if len(llm_tools) > cap and session_id not in self._tool_list_cap_logged_sessions:
            self._tool_list_cap_logged_sessions.add(session_id)
            log_event(
                logger,
                "tool_list_over_cap",
                count=len(llm_tools),
                cap=cap,
                session_id=session_id,
            )
        return llm_tools

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
            learning=self.config.learning,
        )
        recent = self.memory.get_recent_messages(
            session_id, limit=self.config.agent.recent_messages_limit
        )

        # 4. Tools for the LLM: native builtins plus inlined MCP (see ``_build_llm_tools``).
        tools = self._build_llm_tools(session_id)

        # 5. Build message list
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(recent)

        max_input_tokens = self.config.llm.context_window - self.config.llm.max_tokens

        # 6. Call LLM
        messages = _truncate_messages_to_fit(messages, max_input_tokens)
        response = await self.llm.chat(messages, tools=tools if tools else None)

        # 7. Tool call loop
        rounds = 0
        total_native_tool_calls = 0
        had_tool_error = False
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
                # Skill-learning heuristic: only *native* builtins count (not inlined MCP).
                if is_native_tool(tc.name):
                    total_native_tool_calls += 1

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
                        parsed = _coerce_tool_arguments(tc.arguments)
                        result = await execute_mcp_tool(tc.name, parsed)
                except Exception as e:
                    result = f"Tool error: {e}"
                    logger.exception(f"Tool call failed: {tc.name}")

                result = verify_tool_result(tc.name, result)
                if (
                    "Error" in result
                    or result.startswith("Tool error")
                    or result.startswith("Error:")
                    or result.startswith("Blocked:")
                ):
                    had_tool_error = True
                if tc.name == "load_skill" and is_native_tool(tc.name):
                    try:
                        args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
                        sk = str(args.get("name", "")).strip()
                        if sk:
                            outcome = "failure" if "Error" in result else "success"
                            self.memory.record_skill_usage(sk, session_id, outcome)
                    except (json.JSONDecodeError, TypeError):
                        pass
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

        # 11. Optional skill learning — propose-first, human-in-the-loop.
        # The agent NEVER builds a skill on its own; it writes a proposal,
        # persists a durable row in pending_approvals, and notifies the admin.
        # The admin's reply (possibly after a restart) drives the actual build
        # via handle_skill_decision.
        if self.config.learning.enabled:
            try:
                await self._skill_learner.maybe_propose_skill(
                    session_id,
                    message,
                    content,
                    total_native_tool_calls,
                    had_tool_error,
                    self.llm,
                    memory=self.memory,
                    recipient=self.config.signal.admin_group_id,
                )
            except Exception:
                logger.exception("Skill proposal failed")

        log_event(logger, "agent_response", session_id=session_id,
                  memory_hits=len(memories), tool_rounds=rounds)
        return content

    # --------------------------------------------------- skill approval state

    async def sweep_pending_approvals(self, *, reminder: bool = True) -> tuple[int, int]:
        """Expire timed-out skill proposals and optionally re-ping admins.

        Intended for periodic sweeps during normal operation. For
        restart-recovery use :meth:`recover_pending_approvals` instead so
        the admin sees a single consolidated message covering everything
        that was outstanding.
        """
        try:
            expired, reminded = await self._skill_learner.sweep_expired_approvals(
                self.memory, reminder=reminder,
            )
        except Exception:
            logger.exception("pending approvals sweep failed")
            return 0, 0
        log_event(logger, "pending_approvals_swept", expired=expired, reminded=reminded)
        return expired, reminded

    async def recover_pending_approvals(self) -> tuple[int, int, int]:
        """Restart-recovery entrypoint.

        Presents the admin with ALL outstanding skill approvals that were
        waiting when the agent came back up:

        - **still-pending** items require a decision,
        - **expired-while-down** items are mentioned for awareness only,
        - **approved-but-unbuilt** items schedule a grace-window build that
          the admin can cancel with ``stop <slug>`` / ``cancel <slug>``.

        Returns ``(still_pending_count, expired_while_down_count,
        approved_unbuilt_count)``.
        """
        try:
            still_pending, expired, orphans = await self._skill_learner.run_startup_recovery(
                self.memory, llm=self.llm,
            )
        except Exception:
            logger.exception("pending approvals recovery failed")
            return 0, 0, 0
        log_event(
            logger,
            "pending_approvals_recovered",
            still_pending=len(still_pending),
            expired_while_down=len(expired),
            approved_unbuilt=len(orphans),
        )
        return len(still_pending), len(expired), len(orphans)

    async def cancel_approved_build(self, slug: str) -> bool:
        """Abort an approved-but-unbuilt skill during its grace window."""
        return self._skill_learner.cancel_approved_build(slug, self.memory)

    # ---------------------------------------------------------- skill review

    async def run_skill_review(self, *, notify: bool = True) -> Path | None:
        """One-shot skill review. Safe to call manually or from a systemd timer."""
        log_event(logger, "skill_review_started", notify=notify)
        report = await self._skill_learner.review_skills(self.memory, self.llm, notify=notify)
        log_event(logger, "skill_review_finished", report=str(report) if report else None)
        return report

    def start_skill_review_loop(self) -> None:
        """Spawn a background task that periodically runs ``run_skill_review``."""
        if not self.config.learning.enabled:
            return
        if self._review_task is not None and not self._review_task.done():
            return

        interval = max(1, int(self.config.learning.review_interval_hours)) * 3600
        startup_delay = max(0, int(self.config.learning.review_startup_delay_seconds))

        async def _loop() -> None:
            try:
                if startup_delay > 0:
                    await asyncio.sleep(startup_delay)
                while True:
                    try:
                        await self.run_skill_review(notify=True)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Periodic skill review failed")
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                log_event(logger, "skill_review_loop_cancelled")
                raise

        self._review_task = asyncio.create_task(_loop(), name="skill-review-loop")
        log_event(logger, "skill_review_loop_started", interval_hours=self.config.learning.review_interval_hours)

    async def stop_skill_review_loop(self) -> None:
        if self._review_task is None:
            return
        self._review_task.cancel()
        try:
            await self._review_task
        except (asyncio.CancelledError, Exception):
            pass
        self._review_task = None
