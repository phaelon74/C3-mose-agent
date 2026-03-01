"""Tests for the smart output pipeline."""

from __future__ import annotations

import pytest

from luna.tool_output import (
    LARGE_OUTPUT_THRESHOLD,
    _persist_output,
    _python_filter,
    process_large_output,
)


class TestPersistOutput:
    def test_creates_file(self, tmp_path):
        path = _persist_output("hello world", "test_source", tmp_path)
        assert path.exists()
        assert path.read_text() == "hello world"

    def test_deterministic_filename(self, tmp_path):
        p1 = _persist_output("same content", "src", tmp_path)
        p2 = _persist_output("same content", "src", tmp_path)
        assert p1 == p2

    def test_different_content_different_file(self, tmp_path):
        p1 = _persist_output("content A", "src", tmp_path)
        p2 = _persist_output("content B", "src", tmp_path)
        assert p1 != p2

    def test_creates_output_dir(self, tmp_path):
        _persist_output("test", "src", tmp_path)
        assert (tmp_path / "data" / "tool_outputs").is_dir()


class TestPythonFilter:
    def test_keyword_match(self):
        raw = "line one\nPython is great\nline three\nJava is okay"
        filtered = _python_filter(raw, "Python programming")
        assert "Python" in filtered

    def test_no_match_returns_head_tail(self):
        lines = [f"line {i}" for i in range(100)]
        raw = "\n".join(lines)
        filtered = _python_filter(raw, "xyznonexistent")
        # Should contain head lines
        assert "line 0" in filtered
        assert "line 1" in filtered

    def test_context_lines_included(self):
        lines = ["before", "match Python here", "after", "unrelated"]
        raw = "\n".join(lines)
        filtered = _python_filter(raw, "Python")
        assert "before" in filtered
        assert "after" in filtered

    def test_truncation_on_large_filter(self):
        # Generate content where every line matches
        lines = [f"Python keyword line {i}" for i in range(5000)]
        raw = "\n".join(lines)
        filtered = _python_filter(raw, "Python keyword")
        # Should be truncated
        assert len(filtered) <= 5000  # MAX_FILTERED_SIZE + some margin


class TestProcessLargeOutput:
    @pytest.mark.asyncio
    async def test_small_output_passthrough(self, tmp_path):
        result = await process_large_output(
            "small output", "context", "test", None, root=tmp_path
        )
        assert result == "small output"
        # No file should be created for small output
        output_dir = tmp_path / "data" / "tool_outputs"
        assert not output_dir.exists()

    @pytest.mark.asyncio
    async def test_large_output_persisted(self, tmp_path):
        raw = "x" * (LARGE_OUTPUT_THRESHOLD + 100)
        result = await process_large_output(
            raw, "context", "test", None, root=tmp_path
        )
        # Should reference the persisted file
        assert "Full output" in result
        assert "saved to" in result
        # File should exist
        output_dir = tmp_path / "data" / "tool_outputs"
        assert output_dir.exists()
        files = list(output_dir.iterdir())
        assert len(files) == 1
        assert files[0].read_text() == raw

    @pytest.mark.asyncio
    async def test_large_output_with_keywords(self, tmp_path):
        lines = [f"irrelevant padding line number {i} with extra text to make it longer" for i in range(500)]
        lines[250] = "The Python programming language is very popular"
        raw = "\n".join(lines)
        assert len(raw) > LARGE_OUTPUT_THRESHOLD  # ensure we trigger the pipeline
        result = await process_large_output(
            raw, "Python programming", "test", None, root=tmp_path
        )
        assert "Python" in result
        assert "saved to" in result
