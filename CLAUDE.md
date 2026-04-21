# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Mose Agent — a minimal AI assistant with persistent memory, MCP tool use, and Discord/CLI interface. It runs on a homelab (4x RTX 3060) using Qwen3.5-27B-AWQ via vLLM as the LLM backend.

## Commands

```bash
# Activate venv (required for all commands)
source .venv/bin/activate

# Install dependencies (recommended: uv for a lockfile — run `uv lock` then `uv sync`)
pip install -e ".[dev]"
# or: uv sync

# Run the agent (needs DISCORD_TOKEN env var for Discord, otherwise interactive CLI)
python -m mose

# Run all tests
pytest

# Run a single test file or test
pytest tests/test_memory.py
pytest tests/test_agent.py::TestAgent::test_basic_response

# Systemd services (the agent depends on the LLM server)
sudo systemctl start worker-agent    # starts vLLM on port 8001 (Qwen3.5-27B-AWQ)
sudo systemctl start mose-agent      # starts the bot (requires worker-agent)
journalctl -u mose-agent -f          # live logs
```

## Architecture

The agent follows a straightforward pipeline: **Discord message (or CLI input) → Agent loop → LLM (with tool calls) → Response**.

**`agent.py`** — The orchestrator. `Agent.process()` is the main entry point: it saves the user message to memory, retrieves relevant memories via hybrid search, builds a system prompt with memory context, calls the LLM, executes any tool calls in a loop (max 25 rounds), saves the response, and periodically triggers summarization/fact extraction. Accepts an optional `tool_callback` for real-time tool call observation (used by CLI REPL).

**`memory.py`** — Persistent memory using SQLite with three search mechanisms:
- **FTS5** for keyword search
- **sqlite-vec** for vector similarity search (embeddings from `nomic-embed-text-v1.5`, 384 dims)
- **Reciprocal Rank Fusion (RRF)** to merge both result sets, plus recency decay and importance weighting

Three tables: `memories` (long-term facts), `messages` (conversation history per session), `summaries` (periodic LLM-generated conversation summaries). The embedding model is lazy-loaded on first search to avoid slow startup.

**`mcp_manager.py`** — Connects to MCP servers defined in `mcp_servers.json` via stdio transport. Tools are namespaced as `{server}__{tool}` to avoid collisions. Tools are exposed to the LLM in OpenAI function-calling format.

**`llm.py`** — Thin async wrapper around the OpenAI client, pointing at the local vLLM server (`localhost:8001`). Returns `LLMResponse` dataclass with parsed tool calls.

**`tools.py`** — Native tools (bash, file I/O, web search/fetch, delegate, code_task, summarize_paper, MCP meta-tools) plus `verify_tool_result()` which annotates tool output with error hints. Sub-agent tools (`delegate`, `code_task`) get restricted tool subsets to prevent recursion.

**`discord_bot.py`** — Discord.py client. Responds to DMs, @mentions, and thread replies. Session IDs are derived from channel/thread/DM context. Long messages are split at newline/space boundaries to fit Discord's 2000-char limit.

**`__main__.py`** — Entry point. With `DISCORD_TOKEN`: starts Discord bot. Without: starts an interactive CLI REPL where tool calls print inline as they execute. Console log handlers are suppressed to WARNING in CLI mode.

**`observe.py`** — Structured JSON logging (`data/logs/mose-YYYY-MM-DD.jsonl`). All components use `log_event()` for structured data and `log_duration()` context manager for latency tracking.

**`config.py`** — Loads `config.toml` with env var overrides. LLM options use `LLM_ENDPOINT`, `LLM_MODEL`, `LLM_MAX_TOKENS`, `LLM_TEMPERATURE`, `LLM_CONTEXT_WINDOW`, `LLM_API_KEY`, `LLM_PROVIDER`; also `DISCORD_TOKEN`, `SIGNAL_*`, `MEMORY_DB_PATH`, `LOG_DIR`. All config sections are dataclasses.

## Key Design Decisions

- **No API key needed** for the LLM — vLLM doesn't require auth
- **Embedding model lazy-loads** on first `search()` call; tests that don't need embeddings mock `memory.search` to avoid loading it
- **`data/` directory** is gitignored — contains the SQLite database and logs at runtime
- **pytest-asyncio** with `asyncio_mode = "auto"` — async test functions work without the `@pytest.mark.asyncio` decorator
- **nomic-embed-text** requires prefix: `"search_query: "` for queries, `"search_document: "` for stored documents
