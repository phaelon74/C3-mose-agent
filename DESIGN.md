# Design Decisions

This document records the reasoning behind key architectural choices in Luna Agent.

## Custom Build Over Frameworks

**Decision:** Build from scratch instead of using an existing agent framework.

**Context:** We evaluated three frameworks:

| Framework | Lines of Code | Issue |
|-----------|--------------|-------|
| OpenClaw | ~400,000 | 42K instances exposed on Shodan. Impossible to audit. Attack surface too large. |
| ZeroClaw | ~2,000 | 9 days old at evaluation. No track record, no community, uncertain future. |
| NanoClaw | ~500 | Too thin — missing memory, MCP, observability. Would rebuild most of it. |

The core needs (LLM chat, memory, tool calling, Discord, logging) are individually simple and well-understood. The risk of a framework is inheriting its complexity, its security surface, and its opinions. The risk of building is spending a few days writing ~1400 lines of Python. Easy tradeoff.

**Guiding principle:** "Sophisticated is the enemy of simple, complex is the enemy of valuable."

## Python Over Other Languages

**Decision:** Python with discord.py, sentence-transformers, sqlite3, MCP SDK.

**Why:** Every dependency we need has a mature Python library. sentence-transformers only runs in Python. discord.py is the most established Discord bot library. The MCP SDK's reference implementation is Python. Fighting the ecosystem for Go or Rust performance gains makes no sense when the bottleneck is LLM inference latency (~1-2s), not agent code (~1ms).

## Local LLM Over Cloud API

**Decision:** Qwen3-Coder-Next on local hardware via llama-server. No cloud fallback.

**Why:**
- Zero ongoing API cost (already own the hardware)
- No data leaves the machine
- Full control over model, quantization, and context length
- 2x RTX 3090 (48GB total) fits Q4_K_XL quantization (44.6GB) with room for KV cache

**Model choice:** Qwen3-Coder-Next was selected for strong tool-calling support and coding ability. The UD-Q4_K_XL quantization preserves quality while fitting in VRAM. Layer split (not tensor parallel) is used because the GPUs connect via PHB, not NVLink.

## Single LLM Endpoint (AI Firewall Ready)

**Decision:** All LLM traffic goes through one `LLMClient` class pointing at one configurable URL.

**Why:** When we're ready to add an AI firewall (input/output filtering proxy), we change one config value from `localhost:8001` to `localhost:9000`. The proxy forwards to the real LLM after inspection. No code changes. No abstraction layers built speculatively. Just one URL.

This is the "don't build it, don't block it" principle.

## SQLite Over Postgres/Redis/Vector DBs

**Decision:** SQLite for everything — messages, memories, FTS, and vectors.

**Why:**
- Single file (`data/memory.db`), trivially backed up with `cp`
- FTS5 is built into SQLite — no external search engine
- sqlite-vec adds vector search without a separate vector database
- WAL mode handles concurrent reads from logging while the agent writes
- No database server to manage, monitor, or secure
- Single-user agent doesn't need Postgres-level concurrency

**Tradeoff:** If we ever need multi-process access or replication, we'd migrate. That's not on the roadmap.

## Hybrid Search with Reciprocal Rank Fusion

**Decision:** Combine FTS5 keyword search and sqlite-vec vector search using RRF, then weight by recency and importance.

**Why:** Keyword search finds exact matches that embeddings might miss ("error code E1234"). Vector search finds semantic matches that keywords miss ("the bug where the server crashes" → memory about a segfault). RRF is a simple, proven fusion method (one line of math per result) that doesn't need training or tuning.

**Recency decay:** Memories lose relevance over time. Exponential decay with a 7-day half-life means a week-old memory scores half as much as a fresh one. Configurable in `config.toml`.

**Importance scoring:** The LLM assigns importance (1-10) when extracting facts. A score of 10 ("user's name is Fabio") always surfaces; a score of 2 ("the weather was nice") fades quickly.

## nomic-embed-text-v1.5 for Embeddings

**Decision:** Use nomic-embed-text-v1.5 via sentence-transformers, running on CPU.

**Why:**
- 22M parameters — loads in seconds, runs on CPU without impacting GPU memory
- Matryoshka representation learning — can use 384 dimensions now and scale to 768 later without re-embedding
- Asymmetric search prefixes (`search_query:` vs `search_document:`) improve retrieval quality
- Open-source, well-benchmarked, widely used

**Note:** This model uses custom code (`trust_remote_code=True`). The model is from Nomic AI, a reputable org. The custom code is their NomicBERT architecture, viewable on HuggingFace.

## Discord Over WhatsApp/Telegram/Slack

**Decision:** Discord as the messaging interface.

**Why:**
- No phone number / SIM card required (WhatsApp needs one)
- Free, no API costs
- discord.py is mature and well-maintained
- Threads provide natural multi-turn conversation isolation
- Bot permissions are granular
- Easy to add more channels/servers later

## Conversation Compression via Summaries

**Decision:** Every N messages (configurable, default 50), the LLM summarizes the conversation and extracts facts.

**Why:** LLM context windows are finite (128K tokens for Qwen3-Coder). Without compression, long conversations overflow. Summaries preserve the important context while discarding verbatim history. Extracted facts persist independently in the memory system, retrievable across sessions.

This enables effectively infinite-running conversations: the agent always has access to a summary of what came before plus a searchable memory of key facts.

## Structured JSON Logging Over Traditional Logging

**Decision:** Every event is a JSON object logged to daily JSONL files.

**Why:**
- Machine-parseable — `jq` can slice and dice logs instantly
- Every LLM call records tokens, latency, tools used
- Every memory search records hits, method breakdown, results returned
- Every tool call records server, tool name, duration, errors
- Rotation is trivial — one file per day, old files can be compressed/deleted

**Future:** A web dashboard that reads these JSONL files is planned but not built. The logging format is ready for it.

## MCP for Tool Integration

**Decision:** Use the Model Context Protocol (MCP) for all tool integration instead of building custom tool implementations.

**Why:**
- Community-maintained servers for browser automation, filesystem, APIs, etc.
- Standardized discovery — `tools/list` returns schemas automatically
- Standardized execution — `tools/call` with JSON arguments
- New tools added by editing `mcp_servers.json`, no code changes
- Tools run as separate processes (stdio transport), natural isolation

**Namespacing:** Tool names are prefixed with the server name (`browser__navigate`) to avoid collisions between servers.

## systemd Over Docker for the Agent

**Decision:** Run the agent as a systemd service, not in a container.

**Why:**
- Simpler — one service file, `systemctl enable`, done
- Direct GPU access without container runtime configuration
- The agent is a single-user Python process, not a multi-tenant service
- Docker adds complexity (image builds, volume mounts, networking) without clear benefit here

**Note:** MCP tool servers may run in Docker later for isolation. That's a different concern from the agent itself.
