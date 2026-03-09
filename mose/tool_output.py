"""Smart output pipeline: persist large outputs, filter, extract relevant parts."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Protocol

from mose.observe import get_logger, log_event

logger = get_logger("tool_output")

LARGE_OUTPUT_THRESHOLD = 10_000  # chars
MAX_FILTERED_SIZE = 4_000  # target size for filtered output
OUTPUT_DIR = "data/tool_outputs"


class LLMExtractor(Protocol):
    """Protocol for LLM extraction — matches LLMClient.chat signature."""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> Any: ...


def _ensure_output_dir(root: Path) -> Path:
    out = root / OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    return out


def _persist_output(raw: str, source: str, root: Path) -> Path:
    """Save full output to disk. Returns the file path."""
    out_dir = _ensure_output_dir(root)
    # Deterministic filename from content hash + source label
    h = hashlib.sha256(raw.encode()).hexdigest()[:12]
    safe_source = re.sub(r"[^\w\-.]", "_", source)[:60]
    path = out_dir / f"{safe_source}_{h}.txt"
    path.write_text(raw, encoding="utf-8")
    log_event(logger, "output_persisted", path=str(path), size=len(raw))
    return path


def _python_filter(raw: str, context: str) -> str:
    """Extract relevant lines using keyword matching and structure detection."""
    lines = raw.splitlines()

    # Extract keywords from context (words >= 3 chars, lowercased)
    keywords = {w.lower() for w in re.findall(r"\w{3,}", context)}

    # Score each line by keyword matches
    scored: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        line_lower = line.lower()
        score = sum(1 for kw in keywords if kw in line_lower)
        # Boost lines that look like headers, code blocks, or structured data
        if re.match(r"^#{1,6}\s", line) or re.match(r"^```", line):
            score += 2
        scored.append((score, i, line))

    # Collect matching lines with 1 line of context each side
    matching_indices: set[int] = set()
    for score, i, _line in scored:
        if score > 0:
            matching_indices.update(range(max(0, i - 1), min(len(lines), i + 2)))

    if matching_indices:
        # Build filtered output from matching regions
        sorted_indices = sorted(matching_indices)
        parts: list[str] = []
        prev = -2
        for idx in sorted_indices:
            if idx > prev + 1:
                parts.append(f"... (line {idx + 1})")
            parts.append(lines[idx])
            prev = idx
        filtered = "\n".join(parts)
    else:
        # No keyword matches — return head + tail
        head = lines[:30]
        tail = lines[-10:] if len(lines) > 40 else []
        filtered = "\n".join(head)
        if tail:
            filtered += f"\n... ({len(lines) - 40} lines omitted)\n" + "\n".join(tail)

    # Truncate if still too large
    if len(filtered) > MAX_FILTERED_SIZE:
        filtered = filtered[:MAX_FILTERED_SIZE] + "\n... (filtered output truncated)"

    return filtered


async def _llm_extract(raw: str, context: str, llm: LLMExtractor) -> str:
    """Use the LLM to extract relevant parts from large output."""
    # Chunk the raw output to fit in a single extraction call
    chunk = raw[:30_000]  # don't send more than 30K to extraction LLM

    messages = [
        {
            "role": "system",
            "content": (
                "Extract the parts of the following text that are relevant to the user's query. "
                "Return ONLY the relevant extracted content, preserving original formatting. "
                "Do not summarize — copy the actual relevant text. "
                "If nothing is relevant, say 'No relevant content found.'"
            ),
        },
        {
            "role": "user",
            "content": f"Query: {context}\n\n---\n\n{chunk}",
        },
    ]

    try:
        response = await llm.chat(messages)
        return response.content or "No relevant content found."
    except Exception as e:
        logger.exception("LLM extraction failed")
        return f"(LLM extraction failed: {e})"


async def process_large_output(
    raw: str,
    context: str,
    source: str,
    llm: LLMExtractor | None,
    root: Path | None = None,
) -> str:
    """Process potentially large tool output through the smart pipeline.

    Small outputs are returned directly. Large outputs are persisted to disk,
    filtered for relevance, and optionally refined via LLM extraction.
    """
    if len(raw) <= LARGE_OUTPUT_THRESHOLD:
        return raw

    # Resolve project root
    if root is None:
        root = Path(__file__).resolve().parent.parent

    # 1. Persist full output
    path = _persist_output(raw, source, root)

    # 2. Python filtering
    filtered = _python_filter(raw, context)

    # 3. LLM extraction if filtered result is still sparse or too large
    keyword_matches = sum(1 for line in filtered.splitlines() if not line.startswith("..."))
    if llm and keyword_matches < 5:
        log_event(logger, "llm_extraction_triggered", source=source, keyword_matches=keyword_matches)
        extracted = await _llm_extract(raw, context, llm)
        if "No relevant content found" not in extracted:
            filtered = extracted

    # 4. Append file reference
    footer = f"\n\n---\nFull output ({len(raw)} chars) saved to: {path}\nUse read_file or bash with grep to search further."
    return filtered + footer
