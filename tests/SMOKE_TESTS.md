# Luna Agent Smoke Tests

Manual test cases to run via Discord after any significant change (new model, prompt changes, tool updates).
Start a **new thread** for each test run to avoid memory contamination.

---

## 1. Simple Q&A (no tools)

> What is the capital of Portugal?

**Expect:** Direct answer ("Lisbon"), no tool calls, no thinking tags leaked, response within ~5 seconds.
**Tests:** Basic inference, response sanitization, thinking model produces visible content.

---

## 2. Tool use: bash

> How much disk space is free on this machine?

**Expect:** Calls `bash` with `df -h` or similar, reports actual disk numbers.
**Tests:** Single tool call round-trip, bash execution, result interpretation.

---

## 3. Tool use: web search + fetch

> What are the top 3 trending AI papers this week?

**Expect:** Calls `web_search`, possibly `web_fetch` to read results, returns a coherent summary with real titles/links.
**Tests:** Multi-step tool chain (search -> fetch -> summarize), web tools working.

---

## 4. Multi-round tool use

> Create a Python script that prints the first 20 Fibonacci numbers, save it to fibonacci.py, run it, and show me the output.

**Expect:** Uses `write_file` to create the script, `bash` to run it, reports the output. Multiple tool rounds.
**Tests:** write_file -> bash pipeline, multiple tool rounds, workspace file creation.

---

## 5. Memory recall

> What do you know about me and what we've worked on?

**Expect:** References memories (paper indexing, home automation/Kasa, LinkedIn summaries, newsletter). Should NOT hallucinate projects.
**Tests:** Memory retrieval (hybrid search), long-term fact recall, no fabrication.

---

## 6. Long response (thinking budget)

> Explain the difference between transformers and state space models. Cover architecture, training, inference cost, and when to use each.

**Expect:** Multi-paragraph detailed response with actual content (not just thinking). Should complete without "(no response)" or fallback.
**Tests:** Thinking model allocates enough tokens for both reasoning AND output on longer responses.

---

## 7. MCP tool discovery

> What tools do you have available beyond your built-ins?

**Expect:** Calls `list_available_tools`, reports MCP tools (e.g. paper_db tools: index_paper, search_papers, get_stats).
**Tests:** MCP tool registry, meta-tool discovery.

---

## Failure Checklist

For **every** test, verify:

- [ ] No `<thinking>` tags in the Discord message
- [ ] No `<tool_call>` or `<function=...>` markup in the Discord message
- [ ] No "(no response)" replies
- [ ] Response is coherent and answers the question
- [ ] Tool calls execute (check `journalctl -u luna-agent -f` for `tool_executing` events)

## Quick Log Check

```bash
# Watch live during testing
journalctl -u luna-agent -f

# After testing, check for issues
grep "thinking_fallback\|tool_loop_limit\|Tool error" data/logs/luna-$(date -u +%Y-%m-%d).jsonl
```
