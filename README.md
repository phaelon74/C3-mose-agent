# Luna Agent

A custom minimal AI agent with persistent memory, MCP tool integration, Discord interface, and structured observability. Runs entirely on local hardware — no cloud API costs.

## Why Custom

We evaluated existing agent frameworks and rejected them all:

- **OpenClaw**: 400K lines of code, 42K exposed instances on Shodan. Too large to audit, too large to trust.
- **ZeroClaw**: 9 days old at time of evaluation. Too immature.
- **NanoClaw**: Too thin — would need to rebuild most of it anyway.

The core needs (memory, tools, chat, logging) are individually well-solved problems. No 400K-line framework needed.

## Architecture

```
Discord (discord.py)
     |
     v
+-----------------------+
|    Luna Agent Core    |
|                       |
|  agent.py             |  agent loop: msg → memory → prompt → LLM → tools → respond
|    ├── llm.py         |  single LLM client, configurable endpoint
|    ├── memory.py      |  SQLite + FTS5 + sqlite-vec hybrid search
|    ├── mcp_manager.py |  MCP client for community tool servers
|    └── observe.py     |  structured JSON logging
|                       |
+-----------------------+
         |
         v
   llama-server          Qwen3-Coder-Next on 2x RTX 3090
```

All LLM traffic flows through a single `LLMClient` with a configurable endpoint URL. Today it points at `localhost:8001` (llama-server). To insert an AI firewall later, change the URL to `localhost:9000` — zero code changes required.

## Hardware

- Intel i7-13700K, 64GB DDR4
- 2x NVIDIA RTX 3090 (24GB each, 48GB total)
- Qwen3-Coder-Next UD-Q4_K_XL (44.6GB) via llama-server with layer split across both GPUs

## Quick Start

```bash
# On Luna
cd ~/luna-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run without Discord (headless mode)
python -m luna

# Run with Discord
DISCORD_TOKEN=your-token-here python -m luna

# Run tests
pytest tests/ -v
```

## Project Structure

```
luna-agent/
├── config.toml              # All configuration
├── mcp_servers.json         # MCP server registry
├── pyproject.toml           # Dependencies
├── luna/
│   ├── __main__.py          # Entry point (python -m luna)
│   ├── agent.py             # Core agent loop
│   ├── llm.py               # LLM client (OpenAI-compatible)
│   ├── memory.py            # Memory (SQLite + FTS5 + sqlite-vec)
│   ├── mcp_manager.py       # MCP tool client
│   ├── discord_bot.py       # Discord interface
│   ├── observe.py           # Structured JSON logging
│   └── config.py            # Config loader
├── tests/
│   ├── test_agent.py        # Agent loop tests
│   ├── test_llm.py          # LLM client tests
│   └── test_memory.py       # Memory system tests
├── luna-agent.service        # systemd unit for the agent
├── worker-agent.service      # systemd unit for llama-server
└── data/                     # Created at runtime
    ├── memory.db             # SQLite database
    └── logs/                 # JSON log files
```

## Configuration

All settings live in `config.toml`. Environment variables override for secrets:

| Env Var | Overrides | Required |
|---------|-----------|----------|
| `DISCORD_TOKEN` | Discord bot token | Yes (for Discord) |
| `LLM_ENDPOINT` | `[llm] endpoint` | No |
| `LLM_MODEL` | `[llm] model` | No |
| `MEMORY_DB_PATH` | `[memory] db_path` | No |
| `LOG_DIR` | `[observe] log_dir` | No |

See `config.toml` for all available settings and their defaults.

## Components

### LLM Client (`llm.py`)

Thin async wrapper around the OpenAI-compatible API. Single `chat()` method that handles tool calls. This is the only code that talks to the LLM — the AI firewall insertion point.

### Memory (`memory.py`)

SQLite-based persistent memory with three search strategies combined via Reciprocal Rank Fusion:

1. **FTS5 keyword search** — fast exact/stemmed term matching
2. **sqlite-vec cosine similarity** — semantic search via nomic-embed-text-v1.5 embeddings
3. **Recency + importance weighting** — recent and important memories rank higher

The memory system also handles:
- **Message history** — every message persisted per session
- **Session summaries** — LLM-generated compression of old message blocks
- **Fact extraction** — LLM extracts key facts from conversations, stored with importance scores

All retrieval parameters (top_k, RRF k, recency weight, importance threshold, etc.) are in `config.toml` for experimentation.

### MCP Manager (`mcp_manager.py`)

Connects to community MCP servers via stdio transport. On startup it spawns configured servers, discovers their tools, and converts schemas to OpenAI function-calling format. Tool calls from the LLM are routed to the correct server automatically.

Configure servers in `mcp_servers.json`:

```json
{
  "servers": {
    "browser": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp"],
      "transport": "stdio"
    }
  }
}
```

### Discord Bot (`discord_bot.py`)

Responds to DMs, @mentions, and replies in threads it created. Derives session IDs from context (thread/DM/channel) for memory isolation. Splits long responses to stay within Discord's 2000-char limit.

### Observability (`observe.py`)

Every LLM call, tool execution, memory operation, and Discord message is logged as structured JSON to `data/logs/luna-YYYY-MM-DD.jsonl`. Inspect with `jq` or `tail -f`.

## Deployment

Copy the systemd service files and enable them:

```bash
sudo cp luna-agent.service /etc/systemd/system/
sudo cp worker-agent.service /etc/systemd/system/

# Edit luna-agent.service to set DISCORD_TOKEN
sudo systemctl daemon-reload
sudo systemctl enable --now worker-agent
sudo systemctl enable --now luna-agent
```

## Dependencies

6 runtime packages, no heavy frameworks:

- `discord.py` — Discord API
- `openai` — OpenAI-compatible HTTP client
- `mcp[cli]` — Model Context Protocol SDK
- `sentence-transformers` + `einops` — Embedding model
- `sqlite-vec` — Vector search in SQLite

## What This Doesn't Include (By Design)

- No AI firewall (future — just don't block the insertion point)
- No web dashboard (future phase of observability)
- No multi-user auth (single user)
- No cloud LLM fallback (local only)
- No containers for the agent (systemd is simpler)
