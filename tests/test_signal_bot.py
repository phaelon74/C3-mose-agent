"""Tests for the Signal bot."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mose.signal_bot import (
    _split_message,
    _session_id_for,
    _format_status,
    _extract_message_from_envelope,
    MAX_MESSAGE_LENGTH,
)


class TestSplitMessage:
    def test_short_message_unchanged(self):
        text = "Hello"
        assert _split_message(text) == [text]

    def test_empty_message(self):
        assert _split_message("") == [""]

    def test_exactly_at_limit(self):
        text = "x" * MAX_MESSAGE_LENGTH
        assert _split_message(text) == [text]

    def test_one_over_limit(self):
        text = "x" * (MAX_MESSAGE_LENGTH + 1)
        chunks = _split_message(text)
        assert len(chunks) == 2
        assert len(chunks[0]) == MAX_MESSAGE_LENGTH
        assert len(chunks[1]) == 1

    def test_splits_at_newline(self):
        part1 = "a" * (MAX_MESSAGE_LENGTH - 5)
        part2 = "b" * 100
        text = part1 + "\n\n" + part2
        chunks = _split_message(text)
        assert len(chunks) >= 2
        assert chunks[0].endswith("\n\n") or "\n" in chunks[0]

    def test_splits_at_space_when_no_newline(self):
        part1 = "word " * (MAX_MESSAGE_LENGTH // 5)
        text = part1 + "more"
        chunks = _split_message(text)
        assert len(chunks) >= 2
        assert not chunks[0].endswith(" ")

    def test_custom_max_len(self):
        text = "a" * 100
        chunks = _split_message(text, max_len=50)
        assert len(chunks) == 2
        assert len(chunks[0]) == 50


class TestSessionIdFor:
    def test_deterministic(self):
        assert _session_id_for("+1234567890") == _session_id_for("+1234567890")

    def test_format(self):
        sid = _session_id_for("+1234567890")
        assert sid.startswith("signal-")
        assert len(sid) == len("signal-") + 16

    def test_different_numbers_different_ids(self):
        sid1 = _session_id_for("+1111111111")
        sid2 = _session_id_for("+2222222222")
        assert sid1 != sid2


class TestFormatStatus:
    def test_web_search(self):
        assert "query" in _format_status("web_search", '{"query": "test"}')

    def test_bash(self):
        assert "echo" in _format_status("bash", '{"command": "echo hi"}')

    def test_sre_execute(self):
        assert "approval" in _format_status("sre_execute", '{"command": "restart"}')

    def test_unknown_tool(self):
        assert "foo" in _format_status("foo", "{}")


class TestExtractMessageFromEnvelope:
    def test_data_message(self):
        envelope = {
            "source": "+1234567890",
            "sourceNumber": "+1234567890",
            "dataMessage": {"message": "hello mose"},
        }
        source, msg, group_id = _extract_message_from_envelope(envelope)
        assert source == "+1234567890"
        assert msg == "hello mose"
        assert group_id is None

    def test_with_group_info(self):
        envelope = {
            "source": "+1234567890",
            "dataMessage": {
                "message": "hi",
                "groupInfo": {"groupId": "abc123"},
            },
        }
        source, msg, group_id = _extract_message_from_envelope(envelope)
        assert source == "+1234567890"
        assert msg == "hi"
        assert group_id == "abc123"

    def test_sync_message_ignored(self):
        envelope = {
            "source": "+1234567890",
            "syncMessage": {"sentMessage": {"message": "synced"}},
        }
        source, msg, group_id = _extract_message_from_envelope(envelope)
        assert source is None
        assert msg is None

    def test_empty_envelope(self):
        source, msg, group_id = _extract_message_from_envelope({})
        assert source is None
        assert msg is None


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_handles_incoming_message(self):
        """_handle_message processes envelope and calls agent.process."""
        agent = AsyncMock()
        agent.process.return_value = "Hello back!"

        mock_send = AsyncMock()

        from mose.signal_bot import MoseSignalBot
        from mose.config import SignalConfig

        config = SignalConfig(phone_number="+1111111111", daemon_host="127.0.0.1", daemon_port=7583)
        bot = MoseSignalBot(agent, config)
        bot._send_message = mock_send

        envelope = {
            "source": "+1234567890",
            "dataMessage": {"message": "hello"},
        }

        await bot._handle_message(envelope)

        agent.process.assert_called_once()
        call_args = agent.process.call_args
        assert call_args[0][0] == "hello"
        assert call_args[0][1].startswith("signal-")
        assert call_args[1]["status_callback"] is not None

        mock_send.assert_called()
        # Response "Hello back!" sent to sender
        calls = mock_send.call_args_list
        assert any("Hello back" in (c[0][1] if c[0] else "") for c in calls)
