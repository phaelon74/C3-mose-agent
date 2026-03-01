"""Native built-in tools: bash, file I/O, web fetch, web search."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from luna.observe import get_logger, log_event
from luna.tool_output import LLMExtractor, process_large_output

logger = get_logger("tools")

BASH_MAX_OUTPUT = 50_000
BASH_DEFAULT_TIMEOUT = 30
BASH_MAX_TIMEOUT = 120
LIST_DIR_MAX_ENTRIES = 500

# Workspace — set by init_workspace() at startup
_workspace: Path | None = None
_allow_read_outside: bool = True

# MCP manager — set by init_tool_registry() at startup
_mcp_manager: "MCPManager | None" = None


def init_tool_registry(mcp: "MCPManager") -> None:
    """Register the MCP manager so meta-tools can discover and call MCP tools."""
    global _mcp_manager
    _mcp_manager = mcp


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

BLOCKED_PATTERNS = [
    r"\brm\s+-rf\s+/\s*$",
    r"\brm\s+-rf\s+/\s+",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\binit\s+0\b",
    r"\bsystemctl\s+(halt|poweroff|reboot)\b",
    r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;",  # fork bomb
    r"\b>\s*/dev/sda",
]

# --- Tool schemas (OpenAI function-calling format) ---

NATIVE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command. Use for system commands, git, package management, etc.",
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
            "name": "list_available_tools",
            "description": "List additional tools available beyond the built-in ones. Returns tool names and descriptions. Use this to discover what extra capabilities are available.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional keyword to filter tools by name or description.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "use_tool",
            "description": "Call a discovered tool by name. Use list_available_tools first to find available tools and their expected arguments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The tool name (as returned by list_available_tools).",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Arguments to pass to the tool.",
                    },
                },
                "required": ["name"],
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
) -> str:
    """Dispatch a native tool call and return the result string."""
    if isinstance(arguments, str):
        arguments = json.loads(arguments) if arguments else {}

    handler = _TOOL_REGISTRY.get(name)
    if handler is None:
        return f"Error: Unknown native tool '{name}'"

    log_event(logger, "native_tool_call", tool=name)
    try:
        return await handler(arguments, context=context, llm=llm, root=root)
    except Exception as e:
        logger.exception(f"Native tool error: {name}")
        return f"Error executing {name}: {e}"


# --- Tool implementations ---


def _check_blocked(command: str) -> str | None:
    """Return an error message if the command matches a blocked pattern."""
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command):
            return f"Blocked: command matches dangerous pattern '{pattern}'"
    return None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated, {len(text)} total chars)"


async def _tool_bash(args: dict, **kwargs) -> str:
    command = args.get("command", "")
    if not command:
        return "Error: 'command' is required"

    blocked = _check_blocked(command)
    if blocked:
        return blocked

    timeout = min(args.get("timeout", BASH_DEFAULT_TIMEOUT), BASH_MAX_TIMEOUT)
    cwd = args.get("cwd") or (_workspace and str(_workspace))

    try:
        proc = await asyncio.wait_for(
            asyncio.to_thread(
                subprocess.run,
                command,
                shell=True,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                timeout=timeout,
            ),
            timeout=timeout + 5,  # extra margin for thread overhead
        )
    except (asyncio.TimeoutError, subprocess.TimeoutExpired):
        return f"Error: Command timed out after {timeout}s"
    except FileNotFoundError:
        return f"Error: Working directory not found: {cwd}"

    output = ""
    if proc.stdout:
        output += proc.stdout
    if proc.stderr:
        output += ("\n--- stderr ---\n" if output else "") + proc.stderr

    if proc.returncode != 0:
        output += f"\n(exit code: {proc.returncode})"

    return _truncate(output, BASH_MAX_OUTPUT) if output else "(no output)"


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
            req = urllib.request.Request(url, headers={"User-Agent": "Luna-Agent/0.1"})
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
                resp = await client.get(url, headers={"User-Agent": "Luna-Agent/0.1"})
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


async def _tool_list_available_tools(args: dict, **kwargs) -> str:
    if _mcp_manager is None:
        return "No additional tools available (MCP not configured)."

    query = args.get("query", "").lower()
    lines: list[str] = []
    for server in _mcp_manager.servers.values():
        for tool in server.tools:
            name = tool["name"]
            desc = tool.get("description", "")
            if query and query not in name.lower() and query not in desc.lower():
                continue
            lines.append(f"- **{name}**: {desc}")

    if not lines:
        if query:
            return f"No tools matching '{query}'."
        return "No additional tools available."
    return f"Available tools ({len(lines)}):\n" + "\n".join(lines)


async def _tool_use_tool(args: dict, **kwargs) -> str:
    name = args.get("name", "")
    if not name:
        return "Error: 'name' is required"

    if _mcp_manager is None:
        return "Error: MCP not configured — no external tools available."

    arguments = args.get("arguments", {})
    if isinstance(arguments, str):
        arguments = json.loads(arguments) if arguments else {}

    return await _mcp_manager.call_tool(name, arguments)


# --- Delegate sub-agent ---

_DELEGATE_ALLOWED_TOOLS = {"bash", "read_file", "write_file", "list_directory", "web_search", "web_fetch"}
_DELEGATE_MAX_ROUNDS = 5

_DELEGATE_SYSTEM_PROMPT = """\
You are a focused sub-agent. Complete the assigned task using the available tools, then provide your final answer.
Be direct and thorough. Do not ask clarifying questions — work with what you have."""


def _get_delegate_tools() -> list[dict[str, Any]]:
    """Return the subset of NATIVE_TOOLS that the delegate sub-agent can use."""
    return [t for t in NATIVE_TOOLS if t["function"]["name"] in _DELEGATE_ALLOWED_TOOLS]


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

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

        response = await llm.chat(messages, tools=tools)

    if rounds >= _DELEGATE_MAX_ROUNDS:
        messages.append({
            "role": "user",
            "content": "You have reached the tool limit. Please provide your final answer now.",
        })
        response = await llm.chat(messages)

    final = response.content or "(sub-agent produced no response)"
    log_event(logger, "delegate_complete", rounds=rounds, response_len=len(final))
    return final


# --- Registry ---

_TOOL_REGISTRY: dict[str, Any] = {
    "bash": _tool_bash,
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "list_directory": _tool_list_directory,
    "web_fetch": _tool_web_fetch,
    "web_search": _tool_web_search,
    "list_available_tools": _tool_list_available_tools,
    "use_tool": _tool_use_tool,
    "delegate": _tool_delegate,
}
