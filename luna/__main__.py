"""Entry point: python -m luna"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from luna.config import load_config
from luna.observe import setup_logging, get_logger, log_event
from luna.llm import LLMClient
from luna.memory import MemoryManager
from luna.mcp_manager import MCPManager
from luna.agent import Agent
from luna.discord_bot import LunaDiscordBot
from luna.tools import init_workspace, init_tool_registry


async def main() -> None:
    config = load_config()

    # Set up logging first
    setup_logging(config.observe.log_dir, config.observe.log_level)
    logger = get_logger("main")
    log_event(logger, "startup", llm_endpoint=config.llm.endpoint)

    # Initialize workspace sandbox
    init_workspace(config.agent.workspace, config.agent.allow_read_outside)

    # Initialize components
    llm = LLMClient(config.llm)
    memory = MemoryManager(config.memory)

    mcp = MCPManager()
    mcp_config_path = config.root_dir / "mcp_servers.json"
    await mcp.load_servers(mcp_config_path)
    init_tool_registry(mcp)

    agent = Agent(config, llm, memory, mcp)

    # Check for Discord token
    if not config.discord.token:
        log_event(logger, "no_discord_token")
        print("No DISCORD_TOKEN set. Running in headless mode.")
        print("Set DISCORD_TOKEN env var to enable Discord bot.")
        print("Agent is ready. Press Ctrl+C to exit.")

        # Keep alive for testing without Discord
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
    else:
        # Start Discord bot
        bot = LunaDiscordBot(agent)
        log_event(logger, "starting_discord_bot")

        try:
            await bot.start(config.discord.token)
        except KeyboardInterrupt:
            pass
        finally:
            await bot.close()

    # Cleanup
    await mcp.close()
    memory.close()
    log_event(logger, "shutdown")


if __name__ == "__main__":
    asyncio.run(main())
