"""Entry point: python -m mose"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path

from mose.config import load_config
from mose.observe import setup_logging, get_logger, log_event
from mose.llm import create_llm_client
from mose.memory import MemoryManager
from mose.mcp_manager import MCPManager
from mose.agent import Agent
from mose.tools import init_workspace, init_tool_registry, init_approval


async def _cli_approval_callback(command: str, reason: str, target_system: str) -> bool:
    """Prompt user for approval via stdin. Used in CLI mode."""
    print(f"\n[sre_execute] Approval required")
    print(f"  System: {target_system}")
    print(f"  Reason: {reason}")
    print(f"  Command: {command}")
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(None, lambda: input("Approve? [y/N]: "))
    return response.strip().lower() in ("y", "yes")


def _format_tool_args(name: str, arguments: str) -> str:
    """Extract a short summary from tool call arguments."""
    try:
        args = json.loads(arguments) if arguments else {}
    except (json.JSONDecodeError, TypeError):
        args = {}

    if name == "bash" and "command" in args:
        return args["command"]
    if name == "sre_execute" and "command" in args:
        return args["command"]
    if name in ("read_file", "write_file") and "path" in args:
        return args["path"]
    if name == "list_directory" and "path" in args:
        return args["path"]
    if name == "web_search" and "query" in args:
        return args["query"]
    if name == "web_fetch" and "url" in args:
        return args["url"]
    if name in ("delegate", "code_task") and "task" in args:
        return args["task"]

    # Fallback: first string value or raw length
    for v in args.values():
        if isinstance(v, str) and v:
            return v[:80]
    return f"({len(arguments)} chars)" if arguments else ""


def _print_tool_call(name: str, arguments: str, result: str) -> None:
    """Print a tool call inline during CLI mode."""
    summary = _format_tool_args(name, arguments)
    # Truncate summary to 120 chars
    if len(summary) > 120:
        summary = summary[:117] + "..."
    print(f"  [{name}] {summary}")

    # Show first non-empty line of result as preview
    preview = ""
    for line in result.splitlines():
        stripped = line.strip()
        if stripped:
            preview = stripped
            break
    if preview:
        if len(preview) > 120:
            preview = preview[:117] + "..."
        print(f"  -> {preview}")


async def _run_cli(agent: Agent) -> None:
    """Interactive CLI REPL for testing without Discord."""
    session_id = f"cli-{int(time.time())}"
    print("Mose CLI (type 'exit' or Ctrl+D to quit)")
    print(f"Session: {session_id}\n")

    loop = asyncio.get_event_loop()
    while True:
        try:
            user_input = await loop.run_in_executor(None, input, "mose> ")
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input.strip():
            continue
        if user_input.strip().lower() in ("exit", "quit"):
            break

        try:
            response = await agent.process(user_input.strip(), session_id)
            print(f"\n{response}\n")
        except Exception as e:
            print(f"\nError: {e}\n")


async def main() -> None:
    config = load_config()

    # Set up logging first
    setup_logging(config.observe.log_dir, config.observe.log_level)
    logger = get_logger("main")
    log_event(logger, "startup", llm_endpoint=config.llm.endpoint)

    # Initialize workspace sandbox
    init_workspace(config.agent.workspace, config.agent.allow_read_outside)

    # Initialize components
    llm = create_llm_client(config.llm)
    memory = MemoryManager(config.memory)

    mcp = MCPManager()
    mcp_config_path = config.root_dir / "mcp_servers.json"
    await mcp.load_servers(mcp_config_path)
    init_tool_registry(mcp)

    # Choose mode: Signal > Discord > CLI
    if config.signal.phone_number:
        from mose.signal_bot import MoseSignalBot, _signal_approval_callback
        init_approval(_signal_approval_callback)
        agent = Agent(config, llm, memory, mcp)
        bot = MoseSignalBot(agent, config.signal)
        log_event(logger, "starting_signal_bot")
        try:
            await bot.start()
        except KeyboardInterrupt:
            pass
        finally:
            await bot.close()
    elif config.discord.token:
        from mose.discord_bot import MoseDiscordBot, _discord_approval_callback
        init_approval(_discord_approval_callback)
        agent = Agent(config, llm, memory, mcp)
        bot = MoseDiscordBot(agent)
        log_event(logger, "starting_discord_bot")
        try:
            await bot.start(config.discord.token)
        except KeyboardInterrupt:
            pass
        finally:
            await bot.close()
    else:
        init_approval(_cli_approval_callback)
        log_event(logger, "cli_mode")

        # Suppress console log noise in CLI mode
        for h in logging.getLogger("mose").handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.WARNING)

        agent = Agent(config, llm, memory, mcp, tool_callback=_print_tool_call)
        await _run_cli(agent)

    # Cleanup
    await mcp.close()
    memory.close()
    log_event(logger, "shutdown")


if __name__ == "__main__":
    asyncio.run(main())
