"""Tests for the memory system. Use this for experimenting with retrieval strategies."""

from __future__ import annotations

import os
import tempfile
import time

import pytest

from luna.config import MemoryConfig
from luna.memory import MemoryManager
from luna.observe import setup_logging


@pytest.fixture
def memory(tmp_path):
    """Create a fresh memory manager with a temp database."""
    setup_logging(str(tmp_path / "logs"), "DEBUG")
    config = MemoryConfig(
        db_path=str(tmp_path / "test_memory.db"),
        embedding_model="nomic-ai/nomic-embed-text-v1.5",
        embedding_dimensions=384,
        top_k=5,
        chunk_size=500,
        summary_interval=5,
        rrf_k=60,
        importance_threshold=3.0,
        recency_weight=0.3,
    )
    mm = MemoryManager(config)
    yield mm
    mm.close()


class TestMessageHistory:
    def test_save_and_retrieve(self, memory):
        memory.save_message("s1", "user", "Hello!")
        memory.save_message("s1", "assistant", "Hi there!")
        memory.save_message("s1", "user", "How are you?")

        messages = memory.get_recent_messages("s1", limit=10)
        assert len(messages) == 3
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello!"
        assert messages[2]["content"] == "How are you?"

    def test_session_isolation(self, memory):
        memory.save_message("s1", "user", "Session 1")
        memory.save_message("s2", "user", "Session 2")

        m1 = memory.get_recent_messages("s1", limit=10)
        m2 = memory.get_recent_messages("s2", limit=10)
        assert len(m1) == 1
        assert len(m2) == 1
        assert m1[0]["content"] == "Session 1"

    def test_message_count(self, memory):
        for i in range(10):
            memory.save_message("s1", "user", f"Message {i}")
        assert memory.get_message_count("s1") == 10

    def test_limit_returns_most_recent(self, memory):
        for i in range(10):
            memory.save_message("s1", "user", f"Message {i}")

        messages = memory.get_recent_messages("s1", limit=3)
        assert len(messages) == 3
        assert messages[0]["content"] == "Message 7"
        assert messages[2]["content"] == "Message 9"


class TestMemoryStorage:
    def test_store_and_search(self, memory):
        memory.store_memory("Python is a programming language", memory_type="fact", importance=7.0)
        memory.store_memory("The weather is sunny today", memory_type="episodic", importance=3.0)
        memory.store_memory("User prefers dark mode in editors", memory_type="fact", importance=6.0)

        results = memory.search("programming language")
        assert len(results) > 0
        assert any("Python" in r.content for r in results)

    def test_importance_scoring(self, memory):
        memory.store_memory("Critical fact", memory_type="fact", importance=10.0)
        memory.store_memory("Trivial fact", memory_type="fact", importance=1.0)

        # Both should be searchable
        results = memory.search("fact")
        assert len(results) == 2

    def test_hybrid_search_combines_fts_and_vec(self, memory):
        memory.store_memory("The capital of France is Paris", memory_type="fact")
        memory.store_memory("French cuisine is world-renowned", memory_type="fact")
        memory.store_memory("Python was created by Guido van Rossum", memory_type="fact")

        # Keyword "France" should boost FTS, semantic similarity should also contribute
        results = memory.search("France")
        assert len(results) > 0
        # France-related results should rank higher
        assert "France" in results[0].content or "French" in results[0].content


class TestSummaries:
    def test_should_summarize(self, memory):
        assert not memory.should_summarize("s1")

        # Add enough messages to trigger summarization (interval=5 in test config)
        for i in range(5):
            memory.save_message("s1", "user", f"Message {i}")
        assert memory.should_summarize("s1")

    def test_store_summary(self, memory):
        memory.store_summary("s1", "They discussed programming.", 1, 5)
        summary = memory.get_session_summary("s1")
        assert summary == "They discussed programming."

    def test_no_summary_returns_none(self, memory):
        assert memory.get_session_summary("nonexistent") is None
