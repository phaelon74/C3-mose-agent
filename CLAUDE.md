# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Luna Agent — a minimal AI assistant with persistent memory, MCP tool use, and Discord as its interface. It runs on Fabio's homelab (dual RTX 3090s) using a local Qwen3-Coder-Next model via llama-server as the LLM backend.

## Commands

```bash
# Activate venv (required for all commands)
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Run the agent (needs DISCORD_TOKEN env var for Discord, otherwise headless mode)
python -m luna

# Run all tests
pytest

# Run a single test file or test
pytest tests/test_memory.py
pytest tests/test_agent.py::TestAgent::test_basic_response

# Systemd services (the agent depends on the LLM server)
sudo systemctl start qwen3-server    # starts llama-server on port 8001
sudo systemctl start luna-agent      # starts the bot (requires qwen3-server)
journalctl -u luna-agent -f          # live logs
```

## Architecture

The agent follows a straightforward pipeline: **Discord message → Agent loop → LLM (with tool calls) → Response**.

**`agent.py`** — The orchestrator. `Agent.process()` is the main entry point: it saves the user message to memory, retrieves relevant memories via hybrid search, builds a system prompt with memory context, calls the LLM, executes any tool calls in a loop (max 10 rounds), saves the response, and periodically triggers summarization/fact extraction.

**`memory.py`** — Persistent memory using SQLite with three search mechanisms:
- **FTS5** for keyword search
- **sqlite-vec** for vector similarity search (embeddings from `nomic-embed-text-v1.5`, 384 dims)
- **Reciprocal Rank Fusion (RRF)** to merge both result sets, plus recency decay and importance weighting

Three tables: `memories` (long-term facts), `messages` (conversation history per session), `summaries` (periodic LLM-generated conversation summaries). The embedding model is lazy-loaded on first search to avoid slow startup.

**`mcp_manager.py`** — Connects to MCP servers defined in `mcp_servers.json` via stdio transport. Tools are namespaced as `{server}__{tool}` to avoid collisions. Tools are exposed to the LLM in OpenAI function-calling format.

**`llm.py`** — Thin async wrapper around the OpenAI client, pointing at the local llama-server (`localhost:8001`). Returns `LLMResponse` dataclass with parsed tool calls.

**`discord_bot.py`** — Discord.py client. Responds to DMs, @mentions, and thread replies. Session IDs are derived from channel/thread/DM context. Long messages are split at newline/space boundaries to fit Discord's 2000-char limit.

**`observe.py`** — Structured JSON logging (`data/logs/luna-YYYY-MM-DD.jsonl`). All components use `log_event()` for structured data and `log_duration()` context manager for latency tracking.

**`config.py`** — Loads `config.toml` with env var overrides (`DISCORD_TOKEN`, `LLM_ENDPOINT`, `LLM_MODEL`, `MEMORY_DB_PATH`, `LOG_DIR`). All config sections are dataclasses.

## Key Design Decisions

- **No API key needed** for the LLM — llama-server doesn't require auth
- **Embedding model lazy-loads** on first `search()` call; tests that don't need embeddings mock `memory.search` to avoid loading it
- **`data/` directory** is gitignored — contains the SQLite database and logs at runtime
- **pytest-asyncio** with `asyncio_mode = "auto"` — async test functions work without the `@pytest.mark.asyncio` decorator
- **nomic-embed-text** requires prefix: `"search_query: "` for queries, `"search_document: "` for stored documents
