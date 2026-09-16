"""Native built-in tools: bash, file I/O, web fetch, web search."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Callable

from mose.bash_policy import (
    backend_redirect_message,
    bash_rejection_message,
    is_backend_target,
    is_bash_allowlisted,
    is_dangerous_command,
)
from mose.mcp_write_policy import classify_mcp_tool
from mose.observe import get_logger, log_event
from mose.context_compress import (
    compress_messages_if_needed,
    compress_text_if_needed,
    default_tool_result_token_budget,
    max_input_tokens,
)
from mose.tool_output import LLMExtractor, process_large_output

logger = get_logger("tools")

BASH_DEFAULT_TIMEOUT = 30
BASH_MAX_TIMEOUT = 120
LIST_DIR_MAX_ENTRIES = 500

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
    (
        "already exists",
        "Item may already be in Radarr/Sonarr. Re-check the library by tmdbId/tvdbId "
        "(radarr_get_movie / sonarr_get_series) before concluding it is missing.",
    ),
    (
        "movie already",
        "Movie may already be in Radarr. Use radarr_get_movie({ tmdbId }) to confirm "
        "library membership; Missing status means no file yet, not absent from library.",
    ),
]


def verify_tool_result(tool_name: str, result: str) -> str:
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

    # Plex-stack-automation returns Radarr/Sonarr HTTP errors as JSON, not log lines.
    if (
        '"success": false' in result_lower
        and "400" in result_lower
        and "radarr" in result_lower
    ):
        return result + (
            "\n[NOTE: Radarr returned 400 — the movie may already be in your library. "
            "Use radarr_get_movie({ tmdbId }) to confirm before concluding it is missing.]"
        )

    return result

# Workspace — set by init_workspace() at startup
_workspace: Path | None = None
_allow_read_outside: bool = True

# MCP manager — set by init_tool_registry() at startup
_mcp_manager: "MCPManager | None" = None

# Approval callback for sre_execute — set by init_approval() at startup
_approval_callback: "Callable[[str, str, str], Any] | None" = None

# Skills root (for load_skill) — set by init_skills() at startup
_skills_dir: Path | None = None

# Trackers — set by init_tracker_tool_context() at startup
_tracker_memory: "Any | None" = None
_tracker_config: "Any | None" = None
_get_tracker_scheduler: Callable[[], Any] | None = None
_tracker_propose_callback: Callable[..., Any] | None = None

# Scheduled tasks — set by init_scheduled_task_tool_context() at startup
_scheduled_task_memory: Any | None = None
_scheduled_task_config: Any | None = None
_get_task_scheduler: Callable[[], Any | None] | None = None

# Playbooks — set by init_playbook_tool_context() at startup
_playbook_memory: Any | None = None
_playbook_config: Any | None = None

# Skill proposals — set by init_skill_tool_context() at startup
_skill_learner: Any | None = None
_skill_memory: Any | None = None
_skill_config: Any | None = None

_scheduled_exec_ctx: ContextVar[dict[str, Any] | None] = ContextVar(
    "scheduled_exec_ctx", default=None
)
# Code Mode mutating calls hit the HTTP approval bridge in a different asyncio task,
# so ContextVar alone is not enough — map agent-injected tokens to allowlists.
_scheduled_approval_sessions: dict[str, frozenset[str]] = {}


def init_tool_registry(mcp: "MCPManager", config: Any | None = None) -> None:
    """Register the MCP manager so meta-tools can discover and call MCP tools."""
    global _mcp_manager
    _mcp_manager = mcp
    if config is not None:
        from mose.context_compress import init_context_compress

        init_context_compress(config)


def init_approval(callback: Callable[[str, str, str], Any] | None) -> None:
    """Register the approval callback for sre_execute. Callback receives (command, reason, target_system) and returns bool (or awaitable bool)."""
    global _approval_callback
    _approval_callback = callback


def format_mcp_mutate_approval_command(full_name: str, arguments: dict[str, Any]) -> tuple[str, str, str]:
    """Build ``(command, reason, target_system)`` for mutating MCP tool approval (shared with Code Mode bridge)."""
    arg_str = json.dumps(arguments, default=str)
    if len(arg_str) > 500:
        arg_str = arg_str[:497] + "..."
    server = full_name.split("__", 1)[0].strip()
    command = f"{full_name}({arg_str})"
    reason = "MCP tool not on read allowlist (default-deny for protected servers)"
    return command, reason, f"mcp:{server}"


async def invoke_approval_callback(command: str, reason: str, target_system: str) -> bool:
    """Run the registered approval callback; False if none or denied."""
    if _approval_callback is None:
        return False
    result = _approval_callback(command, reason, target_system)
    if asyncio.iscoroutine(result):
        return bool(await result)
    return bool(result)


def init_skills_dir(skills_path: str) -> None:
    """Set the directory used by load_skill (repo skills/ folder)."""
    global _skills_dir
    _skills_dir = Path(skills_path).resolve()
    log_event(logger, "skills_dir_initialized", path=str(_skills_dir))


def init_tracker_tool_context(
    *,
    memory: Any,
    config: Any,
    get_scheduler: Callable[[], Any | None],
) -> None:
    global _tracker_memory, _tracker_config, _get_tracker_scheduler
    _tracker_memory = memory
    _tracker_config = config
    _get_tracker_scheduler = get_scheduler


def init_tracker_propose_callback(callback: Callable[..., Any] | None) -> None:
    global _tracker_propose_callback
    _tracker_propose_callback = callback


def init_scheduled_task_tool_context(
    *,
    memory: Any,
    config: Any,
    get_scheduler: Callable[[], Any | None],
) -> None:
    global _scheduled_task_memory, _scheduled_task_config, _get_task_scheduler
    _scheduled_task_memory = memory
    _scheduled_task_config = config
    _get_task_scheduler = get_scheduler


def init_playbook_tool_context(
    *,
    memory: Any,
    config: Any,
) -> None:
    global _playbook_memory, _playbook_config
    _playbook_memory = memory
    _playbook_config = config


def init_skill_tool_context(
    *,
    learner: Any,
    memory: Any,
    config: Any,
) -> None:
    global _skill_learner, _skill_memory, _skill_config
    _skill_learner = learner
    _skill_memory = memory
    _skill_config = config


def _open_scheduled_task_memory() -> Any | None:
    """Open memory DB when the global manager is unset (e.g. approval bridge process)."""
    try:
        from mose.config import load_config
        from mose.memory import MemoryManager

        cfg = _scheduled_task_config or load_config()
        mem_cfg = getattr(cfg, "memory", None) or load_config().memory
        return MemoryManager(mem_cfg)
    except Exception:
        return None


def _with_scheduled_task_memory(fn: Callable[[Any], None]) -> None:
    mem = _scheduled_task_memory
    owned = False
    if mem is None:
        mem = _open_scheduled_task_memory()
        owned = mem is not None
    if mem is None:
        return
    try:
        fn(mem)
    finally:
        if owned:
            mem.close()


def _persist_scheduled_approval_session(
    token: str, slug: str, allowed_tools: frozenset[str]
) -> None:
    _scheduled_approval_sessions[token] = allowed_tools

    def _save(mem: Any) -> None:
        mem.save_scheduled_approval_session(
            token,
            task_slug=slug,
            allowed_tools=sorted(allowed_tools),
        )

    _with_scheduled_task_memory(_save)


def _clear_scheduled_approval_session(token: str) -> None:
    _scheduled_approval_sessions.pop(token, None)

    def _delete(mem: Any) -> None:
        mem.delete_scheduled_approval_session(token)

    _with_scheduled_task_memory(_delete)


def _lookup_scheduled_approval_tools(token: str) -> frozenset[str] | None:
    if token in _scheduled_approval_sessions:
        return _scheduled_approval_sessions[token]

    found: frozenset[str] | None = None

    def _lookup(mem: Any) -> None:
        nonlocal found
        found = mem.get_scheduled_approval_tools(token)

    _with_scheduled_task_memory(_lookup)
    return found


def enter_scheduled_execution(slug: str, allowed_tools: frozenset[str]) -> Token:
    approval_token = secrets.token_urlsafe(16)
    _persist_scheduled_approval_session(approval_token, slug, allowed_tools)
    return _scheduled_exec_ctx.set(
        {"slug": slug, "allowed_tools": allowed_tools, "approval_token": approval_token}
    )


def exit_scheduled_execution(token: Token) -> None:
    ctx = _scheduled_exec_ctx.get()
    if ctx:
        at = str(ctx.get("approval_token") or "").strip()
        if at:
            _clear_scheduled_approval_session(at)
    _scheduled_exec_ctx.reset(token)


def get_scheduled_execution_slug() -> str | None:
    ctx = _scheduled_exec_ctx.get()
    if not ctx:
        return None
    return str(ctx.get("slug") or "") or None


def get_scheduled_approval_token() -> str | None:
    ctx = _scheduled_exec_ctx.get()
    if not ctx:
        return None
    token = str(ctx.get("approval_token") or "").strip()
    return token or None


def _scheduled_tool_block_reason(tool_name: str) -> str | None:
    ctx = _scheduled_exec_ctx.get()
    if ctx is None:
        return None
    allowed = ctx.get("allowed_tools") or frozenset()
    if tool_name not in allowed:
        log_event(
            logger,
            "scheduled_task_tool_blocked",
            slug=ctx.get("slug"),
            tool=tool_name,
        )
        return (
            f"Blocked: tool '{tool_name}' is not in the approved scheduled task allowlist."
        )
    return None


def scheduled_execution_bypasses_approval(tool_name: str) -> bool:
    ctx = _scheduled_exec_ctx.get()
    if ctx is None:
        return False
    allowed = ctx.get("allowed_tools") or frozenset()
    return tool_name in allowed


def scheduled_approval_bypasses(tool_name: str, approval_token: str | None) -> bool:
    """True when a mutating tool is pre-approved for an active scheduled task run."""
    token = (approval_token or "").strip()
    if token:
        allowed = _lookup_scheduled_approval_tools(token)
        if allowed and tool_name in allowed:
            return True
    return scheduled_execution_bypasses_approval(tool_name)


def init_terminal(cfg: "TerminalConfig", workspace: str) -> None:
    """Configure shell execution backend (local bash or docker exec)."""
    from mose.terminal import init_terminal as _init_terminal_backend

    _init_terminal_backend(cfg, workspace)


def init_workspace(workspace: str, allow_read_outside: bool = True) -> None:
    """Configure the workspace sandbox for file tools."""
    global _workspace, _allow_read_outside
    _workspace = Path(workspace).resolve()
    _workspace.mkdir(parents=True, exist_ok=True)
    _allow_read_outside = allow_read_outside
    log_event(logger, "workspace_initialized", workspace=str(_workspace))


def _resolve_path(path_str: str) -> Path:
    """Resolve a path: relative paths go to workspace, absolute paths stay as-is."""
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        base = _workspace or Path(__file__).resolve().parent.parent
        path = base / path
    return path.resolve()


def _check_write_allowed(path: Path) -> str | None:
    """Return an error message if writing to this path is not allowed."""
    if _workspace is None:
        return None  # no sandbox configured
    resolved = path.resolve()
    try:
        resolved.relative_to(_workspace)
        return None  # inside workspace
    except ValueError:
        return f"Blocked: writes are confined to workspace ({_workspace}). Path {resolved} is outside."

# --- Tool schemas (OpenAI function-calling format) ---

NATIVE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a read-only shell command on THIS host (status, logs, docker ps/logs, ls, cat, grep, find, etc.). "
                "DOES NOT support curl/wget — use ``web_fetch`` for external URLs and ``mcp-portal__portal_codemode_execute`` "
                "for backend systems (Plex / Sonarr / Radarr / NZBGet / paper_db). For state-changing local commands, use ``sre_execute``."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30, max 120).",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory for the command.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's contents. Supports offset/limit for large files. Relative paths resolve to the workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start from (0-based). Default: 0.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of lines to read. Default: all.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates parent directories if needed. Relative paths resolve to the workspace directory. Writes outside the workspace are not allowed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["write", "append"],
                        "description": "Write mode: 'write' (default, overwrite) or 'append'.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and directories at a path. Relative paths resolve to the workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path. Default: current directory.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "List recursively. Default: false.",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Max recursion depth. Default: 3.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "Load the full text of a domain skill from the skills directory by basename (e.g. 'docker'). "
                "Use when skill_loading_mode is level_0 and you need details not in the system prompt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill basename without .md (e.g. 'docker', 'proxmox').",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a web page and extract content as markdown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "What to look for in the page (guides extraction).",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using DuckDuckGo and return results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results (default 10).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_paper",
            "description": (
                "Summarize an arXiv paper using a two-step extract-then-summarize pipeline. "
                "Fetches the paper, extracts verbatim facts from the abstract, then generates "
                "a summary constrained to only those facts. Prevents hallucination."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "arxiv_id": {
                        "type": "string",
                        "description": "The arXiv paper ID (e.g., '2601.10825').",
                    },
                    "style": {
                        "type": "string",
                        "enum": ["technical", "linkedin"],
                        "description": "Summary style: 'technical' (default) or 'linkedin'.",
                    },
                },
                "required": ["arxiv_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate",
            "description": (
                "Delegate a self-contained subtask to a sub-agent with its own tool loop. "
                "The sub-agent can use bash, read_file, write_file, list_directory, web_search, "
                "and web_fetch. Use this for complex tasks that require multiple tool calls "
                "(e.g., 'research X and summarize', 'find and fix the bug in Y'). "
                "Returns the sub-agent's final answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Clear description of the subtask to perform.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional background context to help the sub-agent.",
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "code_task",
            "description": (
                "Delegate a coding task to a specialized sub-agent that writes, runs, and iterates "
                "on code. Use for scripts, scrapers, data processing, automation — anything needing "
                "code execution with a write-run-fix loop. Prefer this over delegate for coding work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Clear description of the coding task.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Background context or constraints.",
                    },
                    "working_dir": {
                        "type": "string",
                        "description": "Subdirectory within workspace (auto-generated if omitted).",
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tracker_propose",
            "description": (
                "Propose a new scheduled data tracker (human must approve). "
                "Use collector_kind 'codemode' with TypeScript for Plex/Sonarr/Radarr/NZBGet via "
                "mcp-portal (same as Code Mode). Collector must console.log JSON: "
                '{\"metrics\":{...},\"snapshot\":{...}}. '
                "Does not start collection until approved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Unique kebab-case id (e.g. plex-streams)."},
                    "description": {"type": "string", "description": "What this tracker measures."},
                    "collector_kind": {
                        "type": "string",
                        "enum": ["codemode", "bash"],
                        "description": "Prefer codemode for backend systems.",
                    },
                    "collector_codemode": {
                        "type": "string",
                        "description": "TypeScript body for portal_codemode_execute when kind is codemode.",
                    },
                    "schedule_seconds": {
                        "type": "integer",
                        "description": "Interval between samples (e.g. 5 for 5 seconds).",
                    },
                    "aggregations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Metric keys to rollup daily (default: all numeric metrics).",
                    },
                    "alert_rules": {
                        "type": "array",
                        "description": (
                            "Rules as objects, e.g. "
                            "{\"id\":\"rec\",\"type\":\"new_daily_high\",\"metric\":\"streams_daily_max\","
                            "\"lookback_days\":30} or threshold_above / threshold_below / delta_pct "
                            "(delta_pct compares the last two samples — at 5s polling that is a 5-second window)."
                        ),
                    },
                    "recipients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "e.g. signal:admin",
                    },
                    "created_by_session": {
                        "type": "string",
                        "description": "Optional session id for audit.",
                    },
                },
                "required": ["slug", "description", "collector_kind", "collector_codemode", "schedule_seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tracker_delete_propose",
            "description": "Propose deleting an existing tracker (requires human approval).",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_slug": {"type": "string", "description": "Tracker slug to remove."},
                },
                "required": ["target_slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tracker_list",
            "description": "List configured trackers as JSON.",
            "parameters": {
                "type": "object",
                "properties": {
                    "enabled_only": {"type": "boolean", "description": "If true, only enabled trackers."},
                    "include_collector": {
                        "type": "boolean",
                        "description": "If true, include collector_kind and collector_ref for debugging.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tracker_update",
            "description": (
                "Update an existing tracker in place (operator tool — no approval). "
                "Use to fix collector_codemode or pause/resume. Prefer after probing API shape."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Tracker slug to update."},
                    "collector_codemode": {
                        "type": "string",
                        "description": "New TypeScript body when collector_kind is codemode.",
                    },
                    "enabled": {"type": "boolean", "description": "Enable or disable the tracker."},
                    "schedule_seconds": {
                        "type": "integer",
                        "description": "New collection interval in seconds.",
                    },
                    "reset_failures": {
                        "type": "boolean",
                        "description": "If true, clear consecutive_failures and last_status.",
                    },
                },
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tracker_query",
            "description": "Query recent samples and/or rollups for a tracker slug.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "since": {"type": "number", "description": "Unix epoch (optional)."},
                    "until": {"type": "number", "description": "Unix epoch (optional)."},
                    "metric": {"type": "string", "description": "Filter rollups by metric name."},
                    "since_bucket": {"type": "string", "description": "YYYY-MM-DD inclusive."},
                    "until_bucket": {"type": "string", "description": "YYYY-MM-DD inclusive."},
                    "limit": {"type": "integer", "description": "Max samples (default 50)."},
                },
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tracker_stats",
            "description": (
                "Aggregate min/max/avg/count for tracker metrics over a time window. "
                "Prefer this over tracker_query for peaks when polling at 5s. "
                "Returns max_sample_id/max_ts for fetching the peak snapshot via tracker_query."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "since": {"type": "number", "description": "Unix epoch start (optional)."},
                    "until": {"type": "number", "description": "Unix epoch end (optional)."},
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Metric keys (default: tracker aggregations).",
                    },
                },
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tracker_pause",
            "description": "Disable a tracker (stops scheduled collection).",
            "parameters": {
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tracker_resume",
            "description": "Re-enable a paused tracker.",
            "parameters": {
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tracker_run_now",
            "description": "Run one collection tick immediately for a tracker.",
            "parameters": {
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scheduled_task_propose",
            "description": (
                "Propose a calendar-scheduled agent task (human must approve). "
                "Use when the user asks to run something daily/weekly/monthly/yearly at a set time. "
                "You MUST supply execution_plan with procedure, allowed_tools, and codemode_scripts "
                "listing every tool the task will use. Times use the configured scheduler timezone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Unique kebab-case id."},
                    "description": {"type": "string", "description": "What this task does."},
                    "recurrence": {
                        "type": "object",
                        "description": (
                            "Schedule: frequency daily|weekly|monthly|yearly, hour (0-23), minute (0-59); "
                            "weekly needs day_of_week (0=Mon); monthly/yearly need day_of_month (1-28); "
                            "yearly needs month (1-12)."
                        ),
                    },
                    "user_prompt": {
                        "type": "string",
                        "description": "Prompt sent to the agent when the task fires.",
                    },
                    "system_addendum": {
                        "type": "string",
                        "description": "Extra system instructions for the scheduled run.",
                    },
                    "execution_plan": {
                        "type": "object",
                        "description": (
                            "Required: procedure (str), allowed_tools (list of tool names), "
                            "optional codemode_scripts [{purpose, code}], max_tool_rounds."
                        ),
                    },
                    "recipients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "signal:admin, signal:engagement, or log_only",
                    },
                    "created_by_session": {"type": "string"},
                },
                "required": [
                    "slug",
                    "description",
                    "recurrence",
                    "user_prompt",
                    "execution_plan",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scheduled_task_update_propose",
            "description": (
                "Propose updating an existing scheduled task in place (human must approve). "
                "Use when changing procedure, codemode scripts, schedule, prompt, or recipients "
                "without deleting and recreating the task. Provide only fields to change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_slug": {
                        "type": "string",
                        "description": "Existing scheduled task slug to update.",
                    },
                    "description": {"type": "string", "description": "New task description."},
                    "recurrence": {
                        "type": "object",
                        "description": "New schedule (same shape as scheduled_task_propose).",
                    },
                    "user_prompt": {
                        "type": "string",
                        "description": "New prompt sent when the task fires.",
                    },
                    "system_addendum": {
                        "type": "string",
                        "description": "New extra system instructions for scheduled runs.",
                    },
                    "execution_plan": {
                        "type": "object",
                        "description": (
                            "Replacement execution plan: procedure, allowed_tools, "
                            "optional codemode_scripts, max_tool_rounds."
                        ),
                    },
                    "recipients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "signal:admin, signal:engagement, or log_only",
                    },
                },
                "required": ["target_slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scheduled_task_delete_propose",
            "description": "Propose deleting a scheduled task (requires human approval).",
            "parameters": {
                "type": "object",
                "properties": {"target_slug": {"type": "string"}},
                "required": ["target_slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scheduled_task_list",
            "description": "List scheduled tasks with next run times.",
            "parameters": {
                "type": "object",
                "properties": {
                    "enabled_only": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scheduled_task_pause",
            "description": "Pause a scheduled task (no approval).",
            "parameters": {
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scheduled_task_resume",
            "description": "Resume a paused scheduled task and recompute next run.",
            "parameters": {
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scheduled_task_run_now",
            "description": "Run a scheduled task immediately (enforces approved tool allowlist).",
            "parameters": {
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "playbook_propose",
            "description": (
                "Propose a reusable on-demand playbook (human must approve). "
                "No schedule — invoke later with playbook_run_propose. "
                "Requires execution_plan with procedure and allowed_tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Unique kebab-case id."},
                    "description": {"type": "string", "description": "What this playbook does."},
                    "user_prompt_template": {
                        "type": "string",
                        "description": "Guidance for filling user_prompt on each invocation.",
                    },
                    "system_addendum": {"type": "string"},
                    "execution_plan": {
                        "type": "object",
                        "description": "procedure (str), allowed_tools (list), optional codemode_scripts.",
                    },
                    "created_by_session": {"type": "string"},
                },
                "required": ["slug", "description", "execution_plan"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "playbook_update_propose",
            "description": "Propose changes to an existing playbook (admin approval required).",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_slug": {"type": "string"},
                    "description": {"type": "string"},
                    "user_prompt_template": {"type": "string"},
                    "system_addendum": {"type": "string"},
                    "execution_plan": {"type": "object"},
                },
                "required": ["target_slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "playbook_delete_propose",
            "description": "Propose deletion of a playbook (admin approval required).",
            "parameters": {
                "type": "object",
                "properties": {"target_slug": {"type": "string"}},
                "required": ["target_slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "playbook_list",
            "description": "List active playbooks (not pending proposals).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "playbook_run_propose",
            "description": (
                "After read-only preflight, propose one bundled admin approval for a playbook run. "
                "On approve, mutating tools in allowed_tools run without per-action prompts. "
                "Requires preflight_summary with findings before delete/mutate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "playbook_slug": {"type": "string"},
                    "invocation_params": {
                        "type": "object",
                        "description": "e.g. series, season, episodes",
                    },
                    "preflight_summary": {
                        "type": "string",
                        "description": "Read-only findings: ids, languages, hasFile, etc.",
                    },
                    "user_prompt": {
                        "type": "string",
                        "description": "Fully resolved instructions for the authorized run.",
                    },
                    "run_slug": {"type": "string", "description": "Optional; default run-{playbook}-{id}."},
                    "reply_session_id": {
                        "type": "string",
                        "description": "Session to attribute results (current chat session).",
                    },
                },
                "required": [
                    "playbook_slug",
                    "invocation_params",
                    "preflight_summary",
                    "user_prompt",
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_propose",
            "description": (
                "Stage a NEW production skill for admin approval. Writes pending JSON only — "
                "does not install skills/{slug}.md. Never use write_file for production skills "
                "(workspace copies are not live). Pass draft_markdown with the full Markdown "
                "body you would have written. Admin replies approve <slug> on Signal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Kebab-case skill slug (filename without .md).",
                    },
                    "title": {"type": "string", "description": "Short title."},
                    "description": {
                        "type": "string",
                        "description": "One-line summary for the approval prompt.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Why this is reusable and when to use it.",
                    },
                    "draft_markdown": {
                        "type": "string",
                        "description": (
                            "Full skill Markdown body (YAML frontmatter optional). "
                            "Staged in the proposal; used as-is on approve instead of an LLM rewrite."
                        ),
                    },
                },
                "required": ["slug", "description", "rationale"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_propose_update",
            "description": (
                "Stage an UPDATE to an EXISTING production skill for admin approval. "
                "Use this when the slug already exists under skills/ — skill_propose will refuse. "
                "Requires draft_markdown (the full revised file). Pending slug is skill-upd-<slug>. "
                "Never use write_file as the install step."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_slug": {
                        "type": "string",
                        "description": "Existing skill slug to revise (kebab-case).",
                    },
                    "title": {"type": "string", "description": "Short title (optional)."},
                    "description": {
                        "type": "string",
                        "description": "One-line summary of the change.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "What was wrong and what this revision fixes.",
                    },
                    "draft_markdown": {
                        "type": "string",
                        "description": "Full revised skill Markdown. Required. Used as-is on approve.",
                    },
                },
                "required": ["target_slug", "description", "rationale", "draft_markdown"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pending_approvals_list",
            "description": (
                "List proposals awaiting human admin approval (skill, tracker, scheduled task, playbook). "
                "Read-only — you cannot approve or reject. "
                "Use when asked about pending proposals; do not infer from tracker_list or scheduled_task_list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": (
                            "Optional filter: skill_proposal, skill_update, tracker_proposal, tracker_deletion, "
                            "scheduled_task_proposal, scheduled_task_update, scheduled_task_deletion, "
                            "playbook_proposal, playbook_update, playbook_deletion, playbook_run_proposal."
                        ),
                    },
                    "include_payload": {
                        "type": "boolean",
                        "description": (
                            "If true, include rationale and tool_trace_count for skill proposals."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_proposal_get",
            "description": (
                "Read full details of a pending skill proposal by slug. "
                "Read-only — approval requires the admin to reply on Signal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Skill proposal slug (kebab-case)."},
                },
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sre_execute",
            "description": (
                "Execute a command that modifies system state (restart, update, delete, etc.). "
                "REQUIRES human approval before execution. Use bash for read-only operations. "
                "Use this tool for anything that changes state: restarts, config changes, "
                "package installs, data modifications."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command to execute.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this command needs to run.",
                    },
                    "target_system": {
                        "type": "string",
                        "description": "Which Cloud3 system this affects.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30, max 120).",
                    },
                },
                "required": ["command", "reason", "target_system"],
            },
        },
    },
]

_NATIVE_TOOL_NAMES: set[str] = {t["function"]["name"] for t in NATIVE_TOOLS}


def is_native_tool(name: str) -> bool:
    """Check if a tool name is a native built-in tool."""
    return name in _NATIVE_TOOL_NAMES


async def call_native_tool(
    name: str,
    arguments: str | dict,
    context: str = "",
    llm: LLMExtractor | None = None,
    root: Path | None = None,
    session_id: str = "",
) -> str:
    """Dispatch a native tool call and return the result string."""
    if isinstance(arguments, str):
        arguments = json.loads(arguments) if arguments else {}

    block = _scheduled_tool_block_reason(name)
    if block:
        return block

    handler = _TOOL_REGISTRY.get(name)
    if handler is None:
        return f"Error: Unknown native tool '{name}'"

    log_event(logger, "native_tool_call", tool=name)
    try:
        return await handler(
            arguments,
            context=context,
            llm=llm,
            root=root,
            session_id=session_id,
        )
    except Exception as e:
        logger.exception(f"Native tool error: {name}")
        return f"Error executing {name}: {e}"


# --- Tool implementations ---


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated, {len(text)} total chars)"


async def _run_shell(
    command: str,
    timeout: int,
    cwd: str | None,
    *,
    require_allowlist: bool,
    llm: LLMExtractor | None = None,
    query_context: str = "",
) -> str:
    """Run via terminal backend; bash requires allowlist, sre_execute allows any non-dangerous command.

    Backend systems (Plex / Sonarr / Radarr / paper_db / MCP sidecars) are always rejected here
    so the LLM is forced through the Code Mode portal where credentials and approval policy live.
    """
    if is_backend_target(command):
        log_event(logger, "shell_backend_redirect", command=command[:200])
        return backend_redirect_message(command)
    if require_allowlist and not is_bash_allowlisted(command):
        return bash_rejection_message(command)
    if not require_allowlist and is_dangerous_command(command):
        return f"Blocked: command matches a dangerous pattern and cannot run even with approval: {command!r}"

    from mose.terminal import get_backend

    timeout = min(timeout, BASH_MAX_TIMEOUT)
    cwd_use = cwd or (_workspace and str(_workspace))
    try:
        res = await get_backend().run(command, timeout, cwd_use)
    except Exception as e:
        logger.exception("shell run failed")
        return f"Error: {e}"

    output = ""
    if res.stdout:
        output += res.stdout
    if res.stderr:
        output += ("\n--- stderr ---\n" if output else "") + res.stderr
    if res.exit_code != 0:
        output += f"\n(exit code: {res.exit_code})"
    if not output:
        return "(no output)"
    return await compress_text_if_needed(
        output,
        llm=llm,
        query_context=query_context or command,
        max_output_tokens=default_tool_result_token_budget(),
        source="bash",
    )


async def _tool_bash(args: dict, **kwargs) -> str:
    command = args.get("command", "")
    if not command:
        return "Error: 'command' is required"

    timeout = min(args.get("timeout", BASH_DEFAULT_TIMEOUT), BASH_MAX_TIMEOUT)
    cwd = args.get("cwd") or (_workspace and str(_workspace))

    return await _run_shell(
        command,
        timeout,
        cwd,
        require_allowlist=True,
        llm=kwargs.get("llm"),
        query_context=kwargs.get("context", ""),
    )


async def _tool_sre_execute(args: dict, **kwargs) -> str:
    """Execute a state-changing command after human approval."""
    command = args.get("command", "")
    reason = args.get("reason", "")
    target_system = args.get("target_system", "")
    if not command:
        return "Error: 'command' is required"
    if not reason:
        return "Error: 'reason' is required"
    if not target_system:
        return "Error: 'target_system' is required"

    if is_dangerous_command(command):
        return f"Blocked: dangerous pattern in command: {command!r}"

    if not scheduled_execution_bypasses_approval("sre_execute"):
        if _approval_callback is None:
            log_event(logger, "sre_execute_denied", reason="no_approval_callback", target_system=target_system)
            return "Execution denied: no approval callback configured. Run with CLI or Discord to enable approval."

        result = _approval_callback(command, reason, target_system)
        if asyncio.iscoroutine(result):
            approved = await result
        else:
            approved = bool(result)

        if not approved:
            log_event(logger, "sre_execute_denied", reason="operator_denied", target_system=target_system)
            return "Execution denied by operator."
    else:
        log_event(logger, "sre_execute_scheduled_bypass", target_system=target_system)

    log_event(logger, "sre_execute_approved", target_system=target_system)
    timeout = min(args.get("timeout", BASH_DEFAULT_TIMEOUT), BASH_MAX_TIMEOUT)
    cwd = _workspace and str(_workspace)

    return await _run_shell(
        command,
        timeout,
        cwd,
        require_allowlist=False,
        llm=kwargs.get("llm"),
        query_context=kwargs.get("context", "") or reason,
    )


async def _tool_read_file(args: dict, context: str = "", llm=None, root=None, **kwargs) -> str:
    path_str = args.get("path", "")
    if not path_str:
        return "Error: 'path' is required"

    path = _resolve_path(path_str)

    if not path.exists():
        return f"Error: File not found: {path}"
    if not path.is_file():
        return f"Error: Not a file: {path}"

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        return f"Error: Permission denied: {path}"

    offset = args.get("offset", 0)
    limit = args.get("limit")

    if offset or limit:
        lines = content.splitlines(keepends=True)
        end = offset + limit if limit else len(lines)
        content = "".join(lines[offset:end])
        # If we sliced, return directly (user was specific)
        return content

    return await process_large_output(
        content, context or path_str, f"read_file_{path.name}", llm, root=root
    )


async def _tool_write_file(args: dict, **kwargs) -> str:
    path_str = args.get("path", "")
    content = args.get("content", "")
    mode = args.get("mode", "write")

    if not path_str:
        return "Error: 'path' is required"

    # LLM sometimes passes a dict/list instead of a string
    if not isinstance(content, str):
        content = json.dumps(content, indent=2)

    path = _resolve_path(path_str)

    blocked = _check_write_allowed(path)
    if blocked:
        return blocked

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with open(path, "a", encoding="utf-8") as f:
                f.write(content)
        else:
            path.write_text(content, encoding="utf-8")
    except PermissionError:
        return f"Error: Permission denied: {path}"
    except OSError as e:
        return f"Error writing file: {e}"

    return f"Wrote {len(content)} chars to {path}"


async def _tool_list_directory(args: dict, **kwargs) -> str:
    path_str = args.get("path", ".")
    recursive = args.get("recursive", False)
    max_depth = args.get("max_depth", 3)

    path = _resolve_path(path_str)

    if not path.exists():
        return f"Error: Path not found: {path}"
    if not path.is_dir():
        return f"Error: Not a directory: {path}"

    entries: list[str] = []
    count = 0

    def _walk(p: Path, depth: int) -> None:
        nonlocal count
        if count >= LIST_DIR_MAX_ENTRIES:
            return
        try:
            items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            entries.append(f"  {'  ' * depth}(permission denied)")
            return
        for item in items:
            if count >= LIST_DIR_MAX_ENTRIES:
                entries.append(f"... (capped at {LIST_DIR_MAX_ENTRIES} entries)")
                return
            prefix = "  " * depth
            suffix = "/" if item.is_dir() else ""
            entries.append(f"{prefix}{item.name}{suffix}")
            count += 1
            if recursive and item.is_dir() and depth < max_depth:
                _walk(item, depth + 1)

    _walk(path, 0)
    return "\n".join(entries) if entries else "(empty directory)"


async def _tool_load_skill(args: dict, **kwargs) -> str:
    """Load one skill file by basename (level_0 mode on-demand)."""
    name = (args.get("name") or "").strip()
    if not name:
        return "Error: 'name' is required"
    if not re.match(r"^[\w\-]+$", name):
        return "Error: name must be a simple basename (letters, numbers, underscore, hyphen)"
    if _skills_dir is None:
        return "Error: skills directory not configured"
    path = _skills_dir / f"{name}.md"
    if not path.is_file():
        return f"Error: skill not found: {name}.md"
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"Error reading skill: {e}"


async def _tool_web_fetch(args: dict, context: str = "", llm=None, root=None, **kwargs) -> str:
    url = args.get("url", "")
    prompt = args.get("prompt", "")
    if not url:
        return "Error: 'url' is required"

    try:
        import httpx
    except ImportError:
        # Fallback to urllib
        import urllib.request
        import urllib.error

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mose-Agent/0.1"})
            resp = await asyncio.to_thread(
                lambda: urllib.request.urlopen(req, timeout=30).read().decode("utf-8", errors="replace")
            )
            html = resp
        except urllib.error.URLError as e:
            return f"Error fetching URL: {e}"
        except Exception as e:
            return f"Error fetching URL: {e}"
    else:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                resp = await client.get(url, headers={"User-Agent": "Mose-Agent/0.1"})
                resp.raise_for_status()
                html = resp.text
        except Exception as e:
            return f"Error fetching URL: {e}"

    # Convert HTML to markdown
    try:
        import html2text
        converter = html2text.HTML2Text()
        converter.ignore_links = False
        converter.ignore_images = True
        converter.body_width = 0
        content = converter.handle(html)
    except ImportError:
        # Crude fallback: strip tags
        content = re.sub(r"<[^>]+>", "", html)
        content = re.sub(r"\s+", " ", content).strip()

    extraction_context = prompt or context or url
    source = f"web_fetch_{url.split('//')[1].split('/')[0] if '//' in url else 'unknown'}"
    return await process_large_output(content, extraction_context, source, llm, root=root)


async def _tool_web_search(args: dict, context: str = "", llm=None, root=None, **kwargs) -> str:
    query = args.get("query", "")
    max_results = args.get("max_results", 10)
    if not query:
        return "Error: 'query' is required"

    try:
        from ddgs import DDGS
    except ImportError:
        return "Error: duckduckgo-search package not installed. Run: pip install duckduckgo-search"

    try:
        results = await asyncio.to_thread(
            lambda: list(DDGS().text(query, max_results=max_results))
        )
    except Exception as e:
        return f"Error searching: {e}"

    if not results:
        return "No results found."

    output_parts: list[str] = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        href = r.get("href", "")
        body = r.get("body", "")
        output_parts.append(f"{i}. **{title}**\n   {href}\n   {body}")

    output = "\n\n".join(output_parts)
    return await process_large_output(output, context or query, f"web_search_{query[:30]}", llm, root=root)


async def execute_mcp_tool(full_name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    """Run an MCP tool by ``server__tool`` name with read/write policy (Code Mode + direct MCP).

    Returns ``(text, is_mcp_error)`` where ``is_mcp_error`` is True when the MCP SDK marks the
    tool result as an error (e.g. FastMCP input validation), not for normal JSON error bodies.
    """
    if _mcp_manager is None:
        return "Error: MCP not configured — no external tools available.", False

    if not isinstance(arguments, dict):
        arguments = {}

    full_name = str(full_name).strip()
    if "__" not in full_name:
        return (
            "Error: MCP tool name must use server__tool format "
            "(e.g. plex-ops-admin__library_list).",
            False,
        )
    server, bare_tool = full_name.split("__", 1)
    server = server.strip()
    bare_tool = bare_tool.strip()
    if not server or not bare_tool:
        return "Error: invalid MCP tool name (empty server or tool segment).", False

    block = _scheduled_tool_block_reason(full_name)
    if block:
        return block, False

    if full_name == "mcp-portal__portal_codemode_execute":
        ctx = _scheduled_exec_ctx.get()
        at = str((ctx or {}).get("approval_token") or "").strip()
        if at:
            arguments = {**arguments, "scheduled_approval_token": at}

    policy = classify_mcp_tool(server, bare_tool)
    if policy != "read":
        if not scheduled_execution_bypasses_approval(full_name):
            if _approval_callback is None:
                log_event(
                    logger,
                    "use_tool_denied",
                    reason="no_approval_callback",
                    tool=full_name,
                )
                return (
                    "Execution denied: no approval callback configured. "
                    "Mutating MCP tools require human approval — configure Signal "
                    "(SIGNAL_ADMIN_GROUP_ID) or use CLI / Discord with approval enabled.",
                    False,
                )
            command, reason, target_system = format_mcp_mutate_approval_command(
                full_name, arguments
            )
            approved = await invoke_approval_callback(command, reason, target_system)
            if not approved:
                log_event(
                    logger,
                    "use_tool_denied",
                    reason="operator_denied",
                    tool=full_name,
                )
                return "Execution denied by operator.", False
            log_event(logger, "use_tool_approved", tool=full_name, target_system=target_system)
        else:
            log_event(logger, "use_tool_scheduled_bypass", tool=full_name)

    return await _mcp_manager.call_tool(full_name, arguments)


# --- Summarize paper (extract-then-summarize) ---

_EXTRACT_PROMPT = """\
You are a precise fact extractor. Given the abstract of a research paper, extract ONLY facts that are \
explicitly stated. Do NOT infer, interpret, or add anything.

Extract these categories:
1. **Method/Model name** — exact name as written
2. **Authors** — if mentioned in the abstract (often not)
3. **Key claims** — quote exact numbers, percentages, and comparisons verbatim
4. **Benchmarks/Datasets** — exact names as written
5. **Domains** — what field or application area

For each fact, quote the relevant text from the abstract.
If a category has no information in the abstract, write "NOT MENTIONED".

Paper title: {title}
Authors: {authors}
Abstract:
{abstract}"""

_SUMMARIZE_PROMPT = """\
You are a precise summarizer. Write a {style} summary of this paper using ONLY the extracted facts below. \
Do NOT add any information, benchmarks, numbers, or claims that are not in the extracts. \
If something is marked "NOT MENTIONED", do not guess or fill it in.

{style_instruction}

Paper title: {title}

Extracted facts:
{extracts}"""

_STYLE_INSTRUCTIONS = {
    "technical": "Write a concise technical summary (3-5 sentences). Focus on the method, key results, and significance.",
    "linkedin": "Write an engaging LinkedIn-style post (3-4 short paragraphs). Use accessible language but stay accurate to the extracts.",
}


async def _tool_summarize_paper(args: dict, context: str = "", llm=None, root=None, **kwargs) -> str:
    arxiv_id = args.get("arxiv_id", "")
    style = args.get("style", "technical")
    if not arxiv_id:
        return "Error: 'arxiv_id' is required"
    if llm is None:
        return "Error: LLM client not available for summarization"
    if style not in _STYLE_INSTRUCTIONS:
        return f"Error: style must be 'technical' or 'linkedin', got '{style}'"

    # Step 1: Best-effort index in paper_db via Code Mode (portal aggregates paper_db upstream)
    paper_meta = None
    if _mcp_manager is not None and "mcp-portal" in _mcp_manager.servers:
        try:
            aid = json.dumps(str(arxiv_id))
            code = (
                f"const r = await mcp.paper_db.index_paper({{ arxiv_id: {aid} }});\n"
                'console.log(typeof r === "string" ? r : JSON.stringify(r));'
            )
            _idx_text, idx_err = await execute_mcp_tool(
                "mcp-portal__portal_codemode_execute",
                {"code": code, "timeout_seconds": 120},
            )
            if idx_err:
                log_event(
                    logger,
                    "summarize_paper_index_failed",
                    arxiv_id=arxiv_id,
                    error="portal_codemode_execute marked MCP error",
                )
            else:
                log_event(logger, "summarize_paper_indexed", arxiv_id=arxiv_id)
        except Exception as e:
            log_event(logger, "summarize_paper_index_failed", arxiv_id=arxiv_id, error=str(e))

    # Fetch metadata directly via arXiv API (always, to get the abstract)
    try:
        try:
            import arxiv as arxiv_lib
        except ImportError:
            return "Error: arxiv package not installed. Run: pip install 'mose-agent[paper]'"
        client = arxiv_lib.Client()
        paper = next(client.results(arxiv_lib.Search(id_list=[arxiv_id])))
        paper_meta = {
            "title": paper.title,
            "authors": ", ".join(a.name for a in paper.authors),
            "abstract": paper.summary,
        }
    except Exception as e:
        return f"Error fetching paper from arXiv: {e}"

    # Step 2: Extract facts at low temperature
    extract_messages = [
        {"role": "system", "content": "You are a precise fact extractor. Follow instructions exactly."},
        {"role": "user", "content": _EXTRACT_PROMPT.format(
            title=paper_meta["title"],
            authors=paper_meta["authors"],
            abstract=paper_meta["abstract"],
        )},
    ]

    try:
        extract_response = await llm.chat(extract_messages, temperature=0.2)
        extracts = extract_response.content or "(extraction failed)"
    except Exception as e:
        return f"Error during fact extraction: {e}"

    # Step 3: Summarize from extracts at slightly higher (but still low) temperature
    summarize_messages = [
        {"role": "system", "content": "You are a precise summarizer. Use ONLY the provided extracts."},
        {"role": "user", "content": _SUMMARIZE_PROMPT.format(
            style=style,
            style_instruction=_STYLE_INSTRUCTIONS[style],
            title=paper_meta["title"],
            extracts=extracts,
        )},
    ]

    try:
        summary_response = await llm.chat(summarize_messages, temperature=0.4)
        summary = summary_response.content or "(summarization failed)"
    except Exception as e:
        return f"Error during summarization: {e}"

    # Step 4: Return combined output
    output = (
        f"# {paper_meta['title']}\n"
        f"**Authors:** {paper_meta['authors']}\n\n"
        f"## Summary ({style})\n\n{summary}\n\n"
        f"---\n\n"
        f"## Extracted Facts\n\n{extracts}\n\n"
        f"---\n\n"
        f"## Raw Abstract\n\n{paper_meta['abstract']}"
    )

    log_event(logger, "summarize_paper_complete", arxiv_id=arxiv_id, style=style)
    return output


# --- Delegate sub-agent ---

_DELEGATE_ALLOWED_TOOLS = {"bash", "read_file", "write_file", "list_directory", "web_search", "web_fetch", "summarize_paper"}
_DELEGATE_MAX_ROUNDS = 5

_DELEGATE_SYSTEM_PROMPT = """\
You are a focused sub-agent. Complete the assigned task using the available tools, then provide your final answer.
Be direct and thorough. Do not ask clarifying questions — work with what you have."""


def _get_delegate_tools() -> list[dict[str, Any]]:
    """Return the subset of NATIVE_TOOLS that the delegate sub-agent can use."""
    return [t for t in NATIVE_TOOLS if t["function"]["name"] in _DELEGATE_ALLOWED_TOOLS]


async def _prepare_subagent_messages(
    messages: list[dict[str, Any]],
    task: str,
    llm: LLMExtractor,
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    budget = max_input_tokens(tools=tools)
    return await compress_messages_if_needed(
        messages,
        llm=llm,
        max_input_tokens=budget,
        query_context=task,
    )


async def _tool_delegate(args: dict, context: str = "", llm=None, root=None, **kwargs) -> str:
    task = args.get("task", "")
    if not task:
        return "Error: 'task' is required"
    if llm is None:
        return "Error: LLM client not available for delegation"

    extra_context = args.get("context", "")
    system_content = _DELEGATE_SYSTEM_PROMPT
    if extra_context:
        system_content += f"\n\nContext: {extra_context}"

    tools = _get_delegate_tools()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": task},
    ]

    log_event(logger, "delegate_start", task=task[:100])

    tool_budget = default_tool_result_token_budget(tools=tools)
    messages = await _prepare_subagent_messages(messages, task, llm, tools)
    response = await llm.chat(messages, tools=tools)
    rounds = 0

    while response.has_tool_calls() and rounds < _DELEGATE_MAX_ROUNDS:
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
            log_event(logger, "delegate_tool", tool=tc.name)
            try:
                if tc.name not in _DELEGATE_ALLOWED_TOOLS:
                    result = f"Error: Tool '{tc.name}' is not available in sub-agent context."
                elif is_native_tool(tc.name):
                    result = await call_native_tool(
                        tc.name, tc.arguments,
                        context=task, llm=llm, root=root,
                    )
                else:
                    result = f"Error: Tool '{tc.name}' is not available in sub-agent context."
            except Exception as e:
                result = f"Tool error: {e}"

            result = await compress_text_if_needed(
                result,
                llm=llm,
                query_context=task,
                max_output_tokens=tool_budget,
                source=f"delegate_{tc.name}",
                root=root,
            )
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

        messages = await _prepare_subagent_messages(messages, task, llm, tools)
        response = await llm.chat(messages, tools=tools)

    if rounds >= _DELEGATE_MAX_ROUNDS:
        messages.append({
            "role": "user",
            "content": "You have reached the tool limit. Please provide your final answer now.",
        })
        messages = await _prepare_subagent_messages(messages, task, llm, tools)
        response = await llm.chat(messages)

    final = response.content or "(sub-agent produced no response)"
    log_event(logger, "delegate_complete", rounds=rounds, response_len=len(final))
    return final


# --- Code task sub-agent ---

_CODE_TASK_ALLOWED_TOOLS = {
    "bash", "read_file", "write_file", "list_directory",
    "web_search", "web_fetch",
}
_CODE_TASK_MAX_ROUNDS = 25

_CODE_TASK_SYSTEM_PROMPT = """\
You are a coding sub-agent. Your job is to write code that WORKS, not code that looks right.

## Mandatory Workflow
For every piece of code you write, you MUST follow this cycle:
1. WRITE the code (write_file)
2. RUN the code (bash)
3. CHECK the output — did it succeed? Did it produce the expected result?
4. If it FAILED: read the error, diagnose, fix, go back to step 2
5. If it SUCCEEDED: verify the output makes sense (not empty, not error pages, not placeholder data)

NEVER skip steps 2-4. NEVER declare success without running the code.

## Anti-Hallucination Rules
- Do NOT guess API endpoints, DOM selectors, or URL patterns
- If you need to access a website or API: web_search for docs first, web_fetch to read them, then code
- If a library call fails, search for the correct approach — do NOT invent alternatives
- If you cannot verify something works, say so explicitly

## Error Handling
- Non-zero exit codes = FAILED. Read stderr, diagnose, fix, re-run.
- Empty output often = silent failure. Add print statements or assertions.
- HTTP 403/404/500 = wrong URL/API. Research the correct one.
- Import errors = missing package. Install it.

## Completion
Report: (1) what was accomplished, (2) files created/modified, (3) how verified, (4) known issues.

Working directory: {working_dir}
"""


async def _tool_code_task(args: dict, context: str = "", llm=None, root=None, **kwargs) -> str:
    task = args.get("task", "")
    if not task:
        return "Error: 'task' is required"
    if llm is None:
        return "Error: LLM client not available for code task"

    # Create working directory within workspace
    working_dir_name = args.get("working_dir", "")
    if not working_dir_name:
        safe_name = re.sub(r"[^\w\-]", "_", task[:40]).strip("_").lower()
        working_dir_name = safe_name
    working_dir = _workspace / working_dir_name if _workspace else Path(working_dir_name)
    working_dir.mkdir(parents=True, exist_ok=True)

    system = _CODE_TASK_SYSTEM_PROMPT.format(working_dir=str(working_dir))
    if args.get("context"):
        system += f"\n\nAdditional context: {args['context']}"

    tools = [t for t in NATIVE_TOOLS if t["function"]["name"] in _CODE_TASK_ALLOWED_TOOLS]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": task},
    ]

    log_event(logger, "code_task_start", task=task[:100])

    tool_budget = default_tool_result_token_budget(tools=tools)
    messages = await _prepare_subagent_messages(messages, task, llm, tools)
    response = await llm.chat(messages, tools=tools, temperature=0.4)
    rounds = 0

    while response.has_tool_calls() and rounds < _CODE_TASK_MAX_ROUNDS:
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
            log_event(logger, "code_task_tool", tool=tc.name)
            try:
                if tc.name not in _CODE_TASK_ALLOWED_TOOLS:
                    result = f"Error: Tool '{tc.name}' is not available in code task context."
                elif is_native_tool(tc.name):
                    result = await call_native_tool(
                        tc.name, tc.arguments,
                        context=task, llm=llm, root=root,
                    )
                else:
                    result = f"Error: Tool '{tc.name}' is not available in code task context."
            except Exception as e:
                result = f"Tool error: {e}"

            result = verify_tool_result(tc.name, result)
            result = await compress_text_if_needed(
                result,
                llm=llm,
                query_context=task,
                max_output_tokens=tool_budget,
                source=f"code_task_{tc.name}",
                root=root,
            )

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

        messages = await _prepare_subagent_messages(messages, task, llm, tools)
        response = await llm.chat(messages, tools=tools, temperature=0.4)

    if rounds >= _CODE_TASK_MAX_ROUNDS:
        messages.append({
            "role": "user",
            "content": (
                "You have reached the tool limit. Provide your final report now: "
                "(1) what was accomplished, (2) files created/modified, "
                "(3) how it was verified, (4) known issues or incomplete items."
            ),
        })
        messages = await _prepare_subagent_messages(messages, task, llm, tools)
        response = await llm.chat(messages, temperature=0.4)

    final = response.content or "(code task produced no response)"
    log_event(logger, "code_task_complete", rounds=rounds, response_len=len(final))
    return final


_VALID_TRACKER_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


async def _tool_tracker_propose(args: dict, **kwargs) -> str:
    from mose.tracker_decision import TRACKER_PROPOSAL_KIND

    if _tracker_memory is None or _tracker_config is None:
        return "Error: tracker subsystem not initialized."
    slug = str(args.get("slug") or "").strip()
    if not _VALID_TRACKER_SLUG.match(slug):
        return "Error: slug must match kebab-case [a-z0-9]+(-[a-z0-9]+)*."
    desc = str(args.get("description") or "").strip()
    if not desc:
        return "Error: description is required."
    kind = str(args.get("collector_kind") or "codemode").strip().lower()
    ref = str(args.get("collector_codemode") or args.get("collector_ref") or "").strip()
    if not ref:
        return "Error: collector_codemode is required."
    if kind == "bash":
        low = ref.lower()
        if any(x in low for x in ("plex", "32400", "sonarr", "radarr", "nzbget", "paper_db")):
            return (
                "Error: do not use bash to reach Plex/Sonarr/Radarr/NZBGet/paper_db — "
                "use collector_kind 'codemode' and mcp-portal__portal_codemode_execute."
            )
    try:
        default_sched = int(getattr(_tracker_config.trackers, "default_schedule_seconds", 5))
        schedule_seconds = int(args.get("schedule_seconds") or default_sched)
    except (TypeError, ValueError):
        return "Error: schedule_seconds must be an integer."
    schedule_seconds = max(5, min(schedule_seconds, 86400))
    aggs = args.get("aggregations")
    if aggs is not None and not isinstance(aggs, list):
        return "Error: aggregations must be a list of strings or omitted."
    rules = args.get("alert_rules")
    if rules is not None and not isinstance(rules, list):
        return "Error: alert_rules must be a list or omitted."
    recips = args.get("recipients")
    if recips is not None and not isinstance(recips, list):
        return "Error: recipients must be a list or omitted."
    if recips is None:
        recips = [getattr(_tracker_config.trackers, "default_recipient", "signal:admin")]
    recipient = str(getattr(_tracker_config.signal, "admin_group_id", "") or "").strip() or "cli"
    expires_at = time.time() + int(getattr(_tracker_config.signal, "proposal_timeout_seconds", 43200))
    payload = {
        "tracker_slug": slug,
        "description": desc,
        "collector_kind": kind,
        "collector_ref": ref,
        "schedule_seconds": schedule_seconds,
        "aggregations": aggs or [],
        "alert_rules": rules or [],
        "recipients": recips,
        "created_by_session": args.get("created_by_session"),
    }
    _tracker_memory.save_pending_approval(
        slug=slug,
        kind=TRACKER_PROPOSAL_KIND,
        recipient=recipient,
        proposal_path="",
        payload=payload,
        expires_at=expires_at,
    )
    if _tracker_propose_callback is not None:
        try:
            ret = _tracker_propose_callback(slug, desc, expires_at)
            if asyncio.iscoroutine(ret):
                await ret
        except Exception:
            logger.exception("tracker_propose_callback failed", extra={"slug": slug})
    return (
        f"Tracker proposal '{slug}' recorded (expires soon). "
        "Awaiting admin approval (Signal/CLI: python -m mose --decide <slug> y|n)."
    )


async def _tool_tracker_delete_propose(args: dict, **kwargs) -> str:
    from mose.tracker_decision import TRACKER_DELETION_KIND

    if _tracker_memory is None or _tracker_config is None:
        return "Error: tracker subsystem not initialized."
    target = str(args.get("target_slug") or "").strip()
    if not _VALID_TRACKER_SLUG.match(target):
        return "Error: target_slug must be kebab-case."
    if _tracker_memory.get_tracker(target) is None:
        return f"Error: no tracker named '{target}'."
    pending_slug = f"tracker-del-{target}"
    if not _VALID_TRACKER_SLUG.match(pending_slug):
        pending_slug = f"tracker-del-{target}"[:200]
    recipient = str(getattr(_tracker_config.signal, "admin_group_id", "") or "").strip() or "cli"
    expires_at = time.time() + int(getattr(_tracker_config.signal, "proposal_timeout_seconds", 43200))
    _tracker_memory.save_pending_approval(
        slug=pending_slug,
        kind=TRACKER_DELETION_KIND,
        recipient=recipient,
        proposal_path="",
        payload={"target_slug": target, "description": f"Delete tracker {target}"},
        expires_at=expires_at,
    )
    if _tracker_propose_callback is not None:
        try:
            ret = _tracker_propose_callback(pending_slug, f"Delete tracker {target}", expires_at)
            if asyncio.iscoroutine(ret):
                await ret
        except Exception:
            logger.exception("tracker_delete_propose_callback failed", extra={"slug": pending_slug})
    return (
        f"Deletion proposal '{pending_slug}' recorded. "
        "Admin must approve: python -m mose --decide <slug> y|n"
    )


async def _tool_tracker_list(args: dict, **kwargs) -> str:
    if _tracker_memory is None:
        return "Error: tracker subsystem not initialized."
    enabled_only = bool(args.get("enabled_only", False))
    include_collector = bool(args.get("include_collector", False))
    rows = _tracker_memory.list_trackers(enabled_only=enabled_only)
    out = []
    for t in rows:
        row: dict[str, Any] = {
            "slug": t.slug,
            "description": t.description,
            "enabled": t.enabled,
            "schedule_seconds": t.schedule_seconds,
            "last_run_at": t.last_run_at,
            "last_status": t.last_status,
            "consecutive_failures": t.consecutive_failures,
        }
        if include_collector:
            row["collector_kind"] = t.collector_kind
            row["collector_ref"] = t.collector_ref
        out.append(row)
    return json.dumps(out, indent=2)


async def _tool_tracker_update(args: dict, **kwargs) -> str:
    if _tracker_memory is None:
        return "Error: tracker subsystem not initialized."
    slug = str(args.get("slug") or "").strip()
    if not _VALID_TRACKER_SLUG.match(slug):
        return "Error: slug must match kebab-case [a-z0-9]+(-[a-z0-9]+)*."
    if _tracker_memory.get_tracker(slug) is None:
        return f"Error: unknown tracker '{slug}'."
    fields: dict[str, Any] = {}
    ref = str(args.get("collector_codemode") or "").strip()
    if ref:
        fields["collector_ref"] = ref
        fields["collector_kind"] = "codemode"
    if "enabled" in args and args["enabled"] is not None:
        fields["enabled"] = bool(args["enabled"])
    if args.get("schedule_seconds") is not None:
        try:
            sec = int(args["schedule_seconds"])
        except (TypeError, ValueError):
            return "Error: schedule_seconds must be an integer."
        fields["schedule_seconds"] = max(5, min(sec, 86400))
    if bool(args.get("reset_failures", False)):
        fields["consecutive_failures"] = 0
        fields["last_status"] = None
    if not fields:
        return "Error: provide at least one of collector_codemode, enabled, schedule_seconds, reset_failures."
    if not _tracker_memory.update_tracker(slug, **fields):
        return f"Error: update failed for '{slug}'."
    sch = _get_tracker_scheduler() if _get_tracker_scheduler else None
    if sch is not None:
        await sch.reconcile()
    return f"Tracker '{slug}' updated: {', '.join(fields.keys())}."


async def _tool_tracker_query(args: dict, **kwargs) -> str:
    if _tracker_memory is None:
        return "Error: tracker subsystem not initialized."
    slug = str(args.get("slug") or "").strip()
    if not slug:
        return "Error: slug is required."
    since = args.get("since")
    until = args.get("until")
    try:
        since_f = float(since) if since is not None else None
        until_f = float(until) if until is not None else None
    except (TypeError, ValueError):
        return "Error: since/until must be numbers."
    limit = int(args.get("limit") or 50)
    limit = max(1, min(limit, 500))
    samples = _tracker_memory.query_tracker_samples(slug, since=since_f, until=until_f, limit=limit)
    metric = args.get("metric")
    since_b = args.get("since_bucket")
    until_b = args.get("until_bucket")
    rollups = _tracker_memory.query_tracker_rollups(
        slug,
        metric=str(metric) if metric else None,
        since_bucket=str(since_b) if since_b else None,
        until_bucket=str(until_b) if until_b else None,
    )
    return json.dumps({"samples": samples, "rollups": rollups}, indent=2, default=str)


async def _tool_tracker_stats(args: dict, **kwargs) -> str:
    if _tracker_memory is None:
        return "Error: tracker subsystem not initialized."
    slug = str(args.get("slug") or "").strip()
    if not slug:
        return "Error: slug is required."
    since = args.get("since")
    until = args.get("until")
    try:
        since_f = float(since) if since is not None else None
        until_f = float(until) if until is not None else None
    except (TypeError, ValueError):
        return "Error: since/until must be numbers."
    metrics_arg = args.get("metrics")
    metrics: list[str] | None = None
    if metrics_arg is not None:
        if not isinstance(metrics_arg, list):
            return "Error: metrics must be a list of strings."
        metrics = [str(m) for m in metrics_arg if m is not None and str(m).strip()]
    stats = _tracker_memory.query_tracker_stats(
        slug, since=since_f, until=until_f, metrics=metrics
    )
    return json.dumps(stats, indent=2, default=str)


async def _tool_tracker_pause(args: dict, **kwargs) -> str:
    if _tracker_memory is None:
        return "Error: tracker subsystem not initialized."
    slug = str(args.get("slug") or "").strip()
    if not slug:
        return "Error: slug is required."
    if not _tracker_memory.update_tracker(slug, enabled=False):
        return f"Error: unknown tracker '{slug}'."
    sch = _get_tracker_scheduler() if _get_tracker_scheduler else None
    if sch is not None:
        await sch.reconcile()
    return f"Tracker '{slug}' paused."


async def _tool_tracker_resume(args: dict, **kwargs) -> str:
    if _tracker_memory is None:
        return "Error: tracker subsystem not initialized."
    slug = str(args.get("slug") or "").strip()
    if not slug:
        return "Error: slug is required."
    if not _tracker_memory.update_tracker(slug, enabled=True, consecutive_failures=0):
        return f"Error: unknown tracker '{slug}'."
    sch = _get_tracker_scheduler() if _get_tracker_scheduler else None
    if sch is not None:
        await sch.reconcile()
    return f"Tracker '{slug}' resumed."


async def _tool_tracker_run_now(args: dict, **kwargs) -> str:
    sch = _get_tracker_scheduler() if _get_tracker_scheduler else None
    if sch is None:
        return "Error: tracker scheduler not running."
    slug = str(args.get("slug") or "").strip()
    if not slug:
        return "Error: slug is required."
    return await sch.run_once(slug)


async def _tool_scheduled_task_propose(args: dict, **kwargs) -> str:
    from mose.schedule import RecurrenceError, validate_recurrence
    from mose.task_decision import SCHEDULED_TASK_PROPOSAL_KIND, notify_task_proposal

    if _scheduled_task_memory is None or _scheduled_task_config is None:
        return "Error: scheduled task subsystem not initialized."
    slug = str(args.get("slug") or "").strip()
    if not _VALID_TRACKER_SLUG.match(slug):
        return "Error: slug must match kebab-case [a-z0-9]+(-[a-z0-9]+)*."
    desc = str(args.get("description") or "").strip()
    if not desc:
        return "Error: description is required."
    user_prompt = str(args.get("user_prompt") or "").strip()
    if not user_prompt:
        return "Error: user_prompt is required."
    plan = args.get("execution_plan")
    if not isinstance(plan, dict):
        return "Error: execution_plan must be an object."
    allowed = plan.get("allowed_tools")
    if not isinstance(allowed, list) or not allowed:
        return "Error: execution_plan.allowed_tools must be a non-empty list."
    procedure = str(plan.get("procedure") or "").strip()
    if not procedure:
        return "Error: execution_plan.procedure is required."
    try:
        recurrence = validate_recurrence(args.get("recurrence") or {})
    except RecurrenceError as e:
        return f"Error: {e}"
    recips = args.get("recipients")
    if recips is not None and not isinstance(recips, list):
        return "Error: recipients must be a list or omitted."
    if recips is None:
        recips = [getattr(_scheduled_task_config.scheduler, "default_recipient", "signal:admin")]
    recipient = str(getattr(_scheduled_task_config.signal, "admin_group_id", "") or "").strip() or "cli"
    expires_at = time.time() + int(
        getattr(_scheduled_task_config.signal, "proposal_timeout_seconds", 43200)
    )
    payload = {
        "task_slug": slug,
        "description": desc,
        "recurrence": recurrence,
        "user_prompt": user_prompt,
        "system_addendum": args.get("system_addendum"),
        "execution_plan": plan,
        "recipients": recips,
        "created_by_session": args.get("created_by_session"),
    }
    _scheduled_task_memory.save_pending_approval(
        slug=slug,
        kind=SCHEDULED_TASK_PROPOSAL_KIND,
        recipient=recipient,
        proposal_path="",
        payload=payload,
        expires_at=expires_at,
    )
    await notify_task_proposal(slug, payload, expires_at)
    return (
        f"Scheduled task proposal '{slug}' recorded. "
        "Awaiting admin approval (approve <slug> in Signal or python -m mose --decide <slug> y)."
    )


async def _tool_scheduled_task_update_propose(args: dict, **kwargs) -> str:
    from mose.schedule import RecurrenceError, validate_recurrence
    from mose.task_decision import SCHEDULED_TASK_UPDATE_KIND, notify_task_proposal

    if _scheduled_task_memory is None or _scheduled_task_config is None:
        return "Error: scheduled task subsystem not initialized."
    target = str(args.get("target_slug") or "").strip()
    if not _VALID_TRACKER_SLUG.match(target):
        return "Error: target_slug must be kebab-case."
    current = _scheduled_task_memory.get_scheduled_task(target)
    if current is None:
        return f"Error: no scheduled task named '{target}'."
    updates: dict[str, Any] = {}
    before: dict[str, Any] = {}
    if "description" in args and args["description"] is not None:
        desc = str(args["description"]).strip()
        if not desc:
            return "Error: description must be non-empty when provided."
        updates["description"] = desc
        before["description"] = current.description
    if "user_prompt" in args and args["user_prompt"] is not None:
        user_prompt = str(args["user_prompt"]).strip()
        if not user_prompt:
            return "Error: user_prompt must be non-empty when provided."
        updates["user_prompt"] = user_prompt
        before["user_prompt"] = current.user_prompt
    if "system_addendum" in args:
        updates["system_addendum"] = args["system_addendum"]
        before["system_addendum"] = current.system_addendum
    if "recurrence" in args and args["recurrence"] is not None:
        try:
            recurrence = validate_recurrence(args["recurrence"])
        except RecurrenceError as e:
            return f"Error: {e}"
        updates["recurrence"] = recurrence
        before["recurrence"] = current.recurrence
    if "execution_plan" in args and args["execution_plan"] is not None:
        plan = args["execution_plan"]
        if not isinstance(plan, dict):
            return "Error: execution_plan must be an object."
        allowed = plan.get("allowed_tools")
        if not isinstance(allowed, list) or not allowed:
            return "Error: execution_plan.allowed_tools must be a non-empty list."
        procedure = str(plan.get("procedure") or "").strip()
        if not procedure:
            return "Error: execution_plan.procedure is required."
        updates["execution_plan"] = plan
        before["execution_plan"] = current.execution_plan
    if "recipients" in args and args["recipients"] is not None:
        recips = args["recipients"]
        if not isinstance(recips, list):
            return "Error: recipients must be a list."
        updates["recipients"] = recips
        before["recipients"] = current.recipients
    if not updates:
        return (
            "Error: provide at least one field to update: description, recurrence, "
            "user_prompt, system_addendum, execution_plan, recipients."
        )
    pending_slug = f"task-upd-{target}"
    recipient = str(getattr(_scheduled_task_config.signal, "admin_group_id", "") or "").strip() or "cli"
    expires_at = time.time() + int(
        getattr(_scheduled_task_config.signal, "proposal_timeout_seconds", 43200)
    )
    payload = {
        "target_slug": target,
        "pending_slug": pending_slug,
        "description": f"Update scheduled task {target}",
        "updates": updates,
        "before": before,
    }
    _scheduled_task_memory.save_pending_approval(
        slug=pending_slug,
        kind=SCHEDULED_TASK_UPDATE_KIND,
        recipient=recipient,
        proposal_path="",
        payload=payload,
        expires_at=expires_at,
    )
    await notify_task_proposal(pending_slug, payload, expires_at)
    return (
        f"Update proposal '{pending_slug}' recorded for task '{target}'. "
        "Awaiting admin approval (approve <pending_slug> in Signal or python -m mose --decide <pending_slug> y)."
    )


async def _tool_scheduled_task_delete_propose(args: dict, **kwargs) -> str:
    from mose.task_decision import SCHEDULED_TASK_DELETION_KIND, notify_task_proposal

    if _scheduled_task_memory is None or _scheduled_task_config is None:
        return "Error: scheduled task subsystem not initialized."
    target = str(args.get("target_slug") or "").strip()
    if not _VALID_TRACKER_SLUG.match(target):
        return "Error: target_slug must be kebab-case."
    if _scheduled_task_memory.get_scheduled_task(target) is None:
        return f"Error: no scheduled task named '{target}'."
    pending_slug = f"task-del-{target}"
    recipient = str(getattr(_scheduled_task_config.signal, "admin_group_id", "") or "").strip() or "cli"
    expires_at = time.time() + int(
        getattr(_scheduled_task_config.signal, "proposal_timeout_seconds", 43200)
    )
    payload = {"target_slug": target, "description": f"Delete scheduled task {target}"}
    _scheduled_task_memory.save_pending_approval(
        slug=pending_slug,
        kind=SCHEDULED_TASK_DELETION_KIND,
        recipient=recipient,
        proposal_path="",
        payload=payload,
        expires_at=expires_at,
    )
    await notify_task_proposal(pending_slug, payload, expires_at)
    return (
        f"Deletion proposal '{pending_slug}' recorded. "
        "Admin must approve: python -m mose --decide <slug> y|n"
    )


async def _tool_scheduled_task_list(args: dict, **kwargs) -> str:
    from mose.schedule import format_next_run

    if _scheduled_task_memory is None or _scheduled_task_config is None:
        return "Error: scheduled task subsystem not initialized."
    enabled_only = bool(args.get("enabled_only"))
    tz = str(getattr(_scheduled_task_config.scheduler, "timezone", "UTC"))
    tasks = _scheduled_task_memory.list_scheduled_tasks(enabled_only=enabled_only)
    out = []
    for t in tasks:
        out.append(
            {
                "slug": t.slug,
                "description": t.description,
                "enabled": t.enabled,
                "next_run": format_next_run(t.next_run_at, tz),
                "last_status": t.last_status,
                "consecutive_failures": t.consecutive_failures,
                "recipients": t.recipients,
            }
        )
    return json.dumps(out, indent=2)


async def _tool_scheduled_task_pause(args: dict, **kwargs) -> str:
    if _scheduled_task_memory is None:
        return "Error: scheduled task subsystem not initialized."
    slug = str(args.get("slug") or "").strip()
    if not slug:
        return "Error: slug is required."
    if not _scheduled_task_memory.update_scheduled_task(slug, enabled=False):
        return f"Error: unknown scheduled task '{slug}'."
    return f"Scheduled task '{slug}' paused."


async def _tool_scheduled_task_resume(args: dict, **kwargs) -> str:
    from mose.schedule import compute_next_run

    if _scheduled_task_memory is None or _scheduled_task_config is None:
        return "Error: scheduled task subsystem not initialized."
    slug = str(args.get("slug") or "").strip()
    if not slug:
        return "Error: slug is required."
    task = _scheduled_task_memory.get_scheduled_task(slug)
    if task is None:
        return f"Error: unknown scheduled task '{slug}'."
    tz = str(getattr(_scheduled_task_config.scheduler, "timezone", "UTC"))
    next_run = compute_next_run(task.recurrence, tz, after=time.time())
    _scheduled_task_memory.update_scheduled_task(
        slug,
        enabled=True,
        consecutive_failures=0,
        next_run_at=next_run,
    )
    sch = _get_task_scheduler() if _get_task_scheduler else None
    if sch is not None:
        await sch.reconcile()
    return f"Scheduled task '{slug}' resumed."


async def _tool_scheduled_task_run_now(args: dict, **kwargs) -> str:
    sch = _get_task_scheduler() if _get_task_scheduler else None
    if sch is None:
        return "Error: task scheduler not running."
    slug = str(args.get("slug") or "").strip()
    if not slug:
        return "Error: slug is required."
    return await sch.run_once(slug)


async def _tool_playbook_propose(args: dict, **kwargs) -> str:
    from mose.playbook_decision import PLAYBOOK_PROPOSAL_KIND, notify_playbook_proposal

    if _playbook_memory is None or _playbook_config is None:
        return "Error: playbook subsystem not initialized."
    slug = str(args.get("slug") or "").strip()
    if not _VALID_TRACKER_SLUG.match(slug):
        return "Error: slug must match kebab-case [a-z0-9]+(-[a-z0-9]+)*."
    desc = str(args.get("description") or "").strip()
    if not desc:
        return "Error: description is required."
    plan = args.get("execution_plan")
    if not isinstance(plan, dict):
        return "Error: execution_plan must be an object."
    allowed = plan.get("allowed_tools")
    if not isinstance(allowed, list) or not allowed:
        return "Error: execution_plan.allowed_tools must be a non-empty list."
    procedure = str(plan.get("procedure") or "").strip()
    if not procedure:
        return "Error: execution_plan.procedure is required."
    recipient = str(getattr(_playbook_config.signal, "admin_group_id", "") or "").strip() or "cli"
    expires_at = time.time() + int(
        getattr(_playbook_config.signal, "proposal_timeout_seconds", 43200)
    )
    payload = {
        "playbook_slug": slug,
        "proposal_kind": PLAYBOOK_PROPOSAL_KIND,
        "description": desc,
        "user_prompt_template": args.get("user_prompt_template"),
        "system_addendum": args.get("system_addendum"),
        "execution_plan": plan,
        "created_by_session": args.get("created_by_session"),
    }
    _playbook_memory.save_pending_approval(
        slug=slug,
        kind=PLAYBOOK_PROPOSAL_KIND,
        recipient=recipient,
        proposal_path="",
        payload=payload,
        expires_at=expires_at,
    )
    await notify_playbook_proposal(slug, payload, expires_at)
    return (
        f"Playbook proposal '{slug}' recorded. "
        "Awaiting admin approval (approve <slug> in Signal or python -m mose --decide <slug> y)."
    )


async def _tool_playbook_update_propose(args: dict, **kwargs) -> str:
    from mose.playbook_decision import PLAYBOOK_UPDATE_KIND, notify_playbook_proposal

    if _playbook_memory is None or _playbook_config is None:
        return "Error: playbook subsystem not initialized."
    target = str(args.get("target_slug") or "").strip()
    if not _VALID_TRACKER_SLUG.match(target):
        return "Error: target_slug must be kebab-case."
    current = _playbook_memory.get_playbook(target)
    if current is None:
        return f"Error: no playbook named '{target}'."
    updates: dict[str, Any] = {}
    before: dict[str, Any] = {}
    if "description" in args and args["description"] is not None:
        desc = str(args["description"]).strip()
        if not desc:
            return "Error: description must be non-empty when provided."
        updates["description"] = desc
        before["description"] = current.description
    if "user_prompt_template" in args:
        updates["user_prompt_template"] = args["user_prompt_template"]
        before["user_prompt_template"] = current.user_prompt_template
    if "system_addendum" in args:
        updates["system_addendum"] = args["system_addendum"]
        before["system_addendum"] = current.system_addendum
    if "execution_plan" in args and args["execution_plan"] is not None:
        plan = args["execution_plan"]
        if not isinstance(plan, dict):
            return "Error: execution_plan must be an object."
        allowed = plan.get("allowed_tools")
        if not isinstance(allowed, list) or not allowed:
            return "Error: execution_plan.allowed_tools must be a non-empty list."
        procedure = str(plan.get("procedure") or "").strip()
        if not procedure:
            return "Error: execution_plan.procedure is required."
        updates["execution_plan"] = plan
        before["execution_plan"] = current.execution_plan
    if not updates:
        return "Error: no fields to update."
    pending_slug = f"playbook-upd-{target}"
    recipient = str(getattr(_playbook_config.signal, "admin_group_id", "") or "").strip() or "cli"
    expires_at = time.time() + int(
        getattr(_playbook_config.signal, "proposal_timeout_seconds", 43200)
    )
    payload = {
        "target_slug": target,
        "pending_slug": pending_slug,
        "proposal_kind": PLAYBOOK_UPDATE_KIND,
        "description": f"Update playbook {target}",
        "updates": updates,
        "before": before,
    }
    _playbook_memory.save_pending_approval(
        slug=pending_slug,
        kind=PLAYBOOK_UPDATE_KIND,
        recipient=recipient,
        proposal_path="",
        payload=payload,
        expires_at=expires_at,
    )
    await notify_playbook_proposal(pending_slug, payload, expires_at)
    return (
        f"Update proposal '{pending_slug}' recorded for playbook '{target}'. "
        "Awaiting admin approval (approve <pending_slug> in Signal or python -m mose --decide <pending_slug> y)."
    )


async def _tool_playbook_delete_propose(args: dict, **kwargs) -> str:
    from mose.playbook_decision import PLAYBOOK_DELETION_KIND, notify_playbook_proposal

    if _playbook_memory is None or _playbook_config is None:
        return "Error: playbook subsystem not initialized."
    target = str(args.get("target_slug") or "").strip()
    if not _VALID_TRACKER_SLUG.match(target):
        return "Error: target_slug must be kebab-case."
    if _playbook_memory.get_playbook(target) is None:
        return f"Error: no playbook named '{target}'."
    pending_slug = f"playbook-del-{target}"
    recipient = str(getattr(_playbook_config.signal, "admin_group_id", "") or "").strip() or "cli"
    expires_at = time.time() + int(
        getattr(_playbook_config.signal, "proposal_timeout_seconds", 43200)
    )
    payload = {"target_slug": target, "description": f"Delete playbook {target}"}
    _playbook_memory.save_pending_approval(
        slug=pending_slug,
        kind=PLAYBOOK_DELETION_KIND,
        recipient=recipient,
        proposal_path="",
        payload=payload,
        expires_at=expires_at,
    )
    await notify_playbook_proposal(pending_slug, payload, expires_at)
    return (
        f"Deletion proposal '{pending_slug}' recorded. "
        "Admin must approve: python -m mose --decide <slug> y|n"
    )


async def _tool_playbook_list(args: dict, **kwargs) -> str:
    if _playbook_memory is None:
        return "Error: playbook subsystem not initialized."
    playbooks = _playbook_memory.list_playbooks()
    out = []
    for p in playbooks:
        plan = p.execution_plan or {}
        out.append(
            {
                "slug": p.slug,
                "description": p.description,
                "allowed_tools": plan.get("allowed_tools") or [],
                "user_prompt_template": p.user_prompt_template,
            }
        )
    return json.dumps(out, indent=2)


async def _tool_playbook_run_propose(args: dict, **kwargs) -> str:
    from mose.playbook_decision import PLAYBOOK_RUN_PROPOSAL_KIND, notify_playbook_proposal

    if _playbook_memory is None or _playbook_config is None:
        return "Error: playbook subsystem not initialized."
    playbook_slug = str(args.get("playbook_slug") or "").strip()
    if not _VALID_TRACKER_SLUG.match(playbook_slug):
        return "Error: playbook_slug must be kebab-case."
    playbook = _playbook_memory.get_playbook(playbook_slug)
    if playbook is None:
        return f"Error: no playbook named '{playbook_slug}'."
    preflight = str(args.get("preflight_summary") or "").strip()
    if not preflight:
        return "Error: preflight_summary is required (run read-only checks first)."
    user_prompt = str(args.get("user_prompt") or "").strip()
    if not user_prompt:
        return "Error: user_prompt is required."
    invocation_params = args.get("invocation_params")
    if not isinstance(invocation_params, dict):
        return "Error: invocation_params must be an object."
    run_slug = str(args.get("run_slug") or "").strip()
    if not run_slug:
        run_slug = f"run-{playbook_slug}-{secrets.token_hex(4)}"
    if not _VALID_TRACKER_SLUG.match(run_slug):
        return "Error: run_slug must be kebab-case."
    plan = playbook.execution_plan or {}
    if not plan.get("allowed_tools"):
        return f"Error: playbook '{playbook_slug}' has empty allowed_tools."
    recipient = str(getattr(_playbook_config.signal, "admin_group_id", "") or "").strip() or "cli"
    expires_at = time.time() + int(
        getattr(_playbook_config.signal, "proposal_timeout_seconds", 43200)
    )
    reply_session = str(args.get("reply_session_id") or kwargs.get("session_id") or "").strip()
    payload = {
        "playbook_slug": playbook_slug,
        "run_slug": run_slug,
        "proposal_kind": PLAYBOOK_RUN_PROPOSAL_KIND,
        "description": playbook.description,
        "invocation_params": invocation_params,
        "preflight_summary": preflight,
        "user_prompt": user_prompt,
        "execution_plan": plan,
        "system_addendum": playbook.system_addendum,
        "reply_session_id": reply_session or None,
    }
    _playbook_memory.save_pending_approval(
        slug=run_slug,
        kind=PLAYBOOK_RUN_PROPOSAL_KIND,
        recipient=recipient,
        proposal_path="",
        payload=payload,
        expires_at=expires_at,
    )
    await notify_playbook_proposal(run_slug, payload, expires_at)
    return (
        f"Playbook run proposal '{run_slug}' recorded for '{playbook_slug}'. "
        "Awaiting admin approval (approve <run_slug> in Signal or python -m mose --decide <run_slug> y). "
        "Mutating tools will run without per-action prompts once approved."
    )


_APPROVAL_KINDS = frozenset({
    "skill_proposal",
    "skill_update",
    "tracker_proposal",
    "tracker_deletion",
    "scheduled_task_proposal",
    "scheduled_task_update",
    "scheduled_task_deletion",
    "playbook_proposal",
    "playbook_update",
    "playbook_deletion",
    "playbook_run_proposal",
})


def _get_approvals_memory() -> Any | None:
    """Shared MemoryManager used by tracker, scheduled-task, and playbook subsystems."""
    return _tracker_memory or _scheduled_task_memory or _playbook_memory or _skill_memory


def _format_approval_timestamp(ts: float) -> str:
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    except (OSError, OverflowError, ValueError):
        return "?"


def _approval_row_summary(row: Any, *, include_payload: bool = False) -> dict[str, Any]:
    payload = row.payload or {}
    item: dict[str, Any] = {
        "slug": row.slug,
        "kind": row.kind,
        "title": str(payload.get("title") or ""),
        "description": str(payload.get("description") or "")[:200],
        "expires_at": _format_approval_timestamp(row.expires_at),
        "created_at": _format_approval_timestamp(row.created_at),
        "proposal_path": row.proposal_path or "",
    }
    if include_payload:
        rationale = payload.get("rationale")
        if rationale:
            item["rationale"] = str(rationale)[:500]
        if row.kind in ("skill_proposal", "skill_update") and row.proposal_path:
            try:
                proposal_file = Path(row.proposal_path)
                if proposal_file.is_file():
                    data = json.loads(proposal_file.read_text(encoding="utf-8"))
                    trace = data.get("tool_trace")
                    if isinstance(trace, list):
                        item["tool_trace_count"] = len(trace)
            except (OSError, json.JSONDecodeError, TypeError):
                pass
    return item


async def _tool_pending_approvals_list(args: dict, **kwargs) -> str:
    memory = _get_approvals_memory()
    if memory is None:
        return "Error: approvals subsystem not initialized."
    kind = str(args.get("kind") or "").strip()
    if kind and kind not in _APPROVAL_KINDS:
        valid = ", ".join(sorted(_APPROVAL_KINDS))
        return f"Error: unknown kind '{kind}'. Valid kinds: {valid}."
    include_payload = bool(args.get("include_payload", False))
    rows = memory.list_pending_approvals(kind=kind or None, status="pending")
    out = [_approval_row_summary(row, include_payload=include_payload) for row in rows]
    return json.dumps(out, indent=2)


async def _tool_skill_proposal_get(args: dict, **kwargs) -> str:
    memory = _get_approvals_memory()
    if memory is None:
        return "Error: approvals subsystem not initialized."
    slug = str(args.get("slug") or "").strip()
    if not slug:
        return "Error: slug is required."
    row = memory.get_pending_approval(slug)
    if row is None or row.status != "pending":
        return f"Error: no pending skill proposal found for '{slug}'."
    if row.kind not in ("skill_proposal", "skill_update"):
        return (
            f"Error: '{slug}' is a pending {row.kind}, not a skill proposal. "
            "Use pending_approvals_list."
        )
    payload = row.payload or {}
    result: dict[str, Any] = {
        "slug": row.slug,
        "kind": row.kind,
        "status": row.status,
        "title": str(payload.get("title") or ""),
        "description": str(payload.get("description") or ""),
        "rationale": str(payload.get("rationale") or ""),
        "expires_at": _format_approval_timestamp(row.expires_at),
        "proposal_path": row.proposal_path or "",
        "target_slug": str(payload.get("target_slug") or ""),
        "is_update": bool(payload.get("is_update")),
        "has_draft": bool(payload.get("has_draft")),
    }
    if row.proposal_path:
        try:
            proposal_file = Path(row.proposal_path)
            if proposal_file.is_file():
                data = json.loads(proposal_file.read_text(encoding="utf-8"))
                trace = data.get("tool_trace")
                if isinstance(trace, list):
                    result["tool_trace_count"] = len(trace)
                draft = str(data.get("draft_markdown") or "")
                if draft:
                    result["has_draft"] = True
                    result["draft_chars"] = len(draft)
                    result["draft_preview"] = draft[:400]
        except (OSError, json.JSONDecodeError, TypeError) as e:
            result["file_read_error"] = str(e)
    return json.dumps(result, indent=2)


async def _skill_propose_common(
    args: dict,
    *,
    is_update: bool,
    context: str = "",
    session_id: str = "",
    **_kwargs: Any,
) -> str:
    if _skill_learner is None or _skill_memory is None:
        return "Error: skill proposal subsystem not initialized."
    if is_update:
        slug = str(args.get("target_slug") or args.get("slug") or "").strip()
    else:
        slug = str(args.get("slug") or "").strip()
    if not slug:
        return "Error: slug is required." if not is_update else "Error: target_slug is required."
    recipient = ""
    if _skill_config is not None:
        recipient = str(getattr(_skill_config.signal, "admin_group_id", "") or "").strip()
    recipient = recipient or "cli"
    return await _skill_learner.propose_from_tool(
        slug=slug,
        title=str(args.get("title") or ""),
        description=str(args.get("description") or ""),
        rationale=str(args.get("rationale") or ""),
        is_update=is_update,
        draft_markdown=str(args.get("draft_markdown") or ""),
        session_id=session_id,
        user_message=context,
        memory=_skill_memory,
        recipient=recipient,
    )


async def _tool_skill_propose(args: dict, **kwargs) -> str:
    return await _skill_propose_common(args, is_update=False, **kwargs)


async def _tool_skill_propose_update(args: dict, **kwargs) -> str:
    return await _skill_propose_common(args, is_update=True, **kwargs)


# --- Registry ---

_TOOL_REGISTRY: dict[str, Any] = {
    "bash": _tool_bash,
    "sre_execute": _tool_sre_execute,
    "load_skill": _tool_load_skill,
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "list_directory": _tool_list_directory,
    "web_fetch": _tool_web_fetch,
    "web_search": _tool_web_search,
    "summarize_paper": _tool_summarize_paper,
    "delegate": _tool_delegate,
    "code_task": _tool_code_task,
    "tracker_propose": _tool_tracker_propose,
    "tracker_delete_propose": _tool_tracker_delete_propose,
    "tracker_list": _tool_tracker_list,
    "tracker_update": _tool_tracker_update,
    "tracker_query": _tool_tracker_query,
    "tracker_stats": _tool_tracker_stats,
    "tracker_pause": _tool_tracker_pause,
    "tracker_resume": _tool_tracker_resume,
    "tracker_run_now": _tool_tracker_run_now,
    "scheduled_task_propose": _tool_scheduled_task_propose,
    "scheduled_task_update_propose": _tool_scheduled_task_update_propose,
    "scheduled_task_delete_propose": _tool_scheduled_task_delete_propose,
    "scheduled_task_list": _tool_scheduled_task_list,
    "scheduled_task_pause": _tool_scheduled_task_pause,
    "scheduled_task_resume": _tool_scheduled_task_resume,
    "scheduled_task_run_now": _tool_scheduled_task_run_now,
    "playbook_propose": _tool_playbook_propose,
    "playbook_update_propose": _tool_playbook_update_propose,
    "playbook_delete_propose": _tool_playbook_delete_propose,
    "playbook_list": _tool_playbook_list,
    "playbook_run_propose": _tool_playbook_run_propose,
    "skill_propose": _tool_skill_propose,
    "skill_propose_update": _tool_skill_propose_update,
    "pending_approvals_list": _tool_pending_approvals_list,
    "skill_proposal_get": _tool_skill_proposal_get,
}
