"""Tests for the Signal bot."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mose.signal_bot import (
    MAX_MESSAGE_LENGTH,
    MoseSignalBot,
    _extract_message_from_envelope,
    _format_status,
    _handle_skill_approval_reply,
    _session_id_for_signal_group,
    _split_message,
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


class TestSessionIdForSignalGroup:
    def test_engagement_vs_admin_prefix(self):
        gid = "same-group-id"
        eng = _session_id_for_signal_group(gid, admin=False)
        adm = _session_id_for_signal_group(gid, admin=True)
        assert eng.startswith("signal-grp-eng-")
        assert adm.startswith("signal-grp-adm-")
        assert eng != adm

    def test_deterministic(self):
        gid = "abc123"
        assert _session_id_for_signal_group(gid, admin=False) == _session_id_for_signal_group(
            gid, admin=False
        )


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


def _signal_config(*, eng: str = "eng-gid", adm: str = "adm-gid") -> "SignalConfig":
    from mose.config import SignalConfig

    return SignalConfig(
        phone_number="+1111111111",
        engagement_group_id=eng,
        admin_group_id=adm,
        daemon_host="127.0.0.1",
        daemon_port=7583,
    )


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_handles_incoming_message_engagement_group(self):
        """_handle_message processes envelope in engagement group."""
        agent = AsyncMock()
        agent.process.return_value = "Hello back!"

        mock_send = AsyncMock()

        config = _signal_config()
        bot = MoseSignalBot(agent, config)
        bot._send_message = mock_send

        envelope = {
            "source": "+1234567890",
            "dataMessage": {
                "message": "hello",
                "groupInfo": {"groupId": "eng-gid"},
            },
        }

        await bot._handle_message(envelope)

        agent.process.assert_called_once()
        call_args = agent.process.call_args
        assert call_args[0][0] == "hello"
        assert call_args[0][1].startswith("signal-grp-eng-")
        assert call_args[1]["status_callback"] is not None

        mock_send.assert_called()
        calls = mock_send.call_args_list
        assert any("Hello back" in (c[0][1] if c[0] else "") for c in calls)
        assert all(c[0][0] == "eng-gid" for c in calls if c[0])

    @pytest.mark.asyncio
    async def test_ignores_unknown_group(self):
        agent = AsyncMock()
        config = _signal_config()
        bot = MoseSignalBot(agent, config)
        bot._send_message = AsyncMock()
        envelope = {
            "source": "+1234567890",
            "dataMessage": {
                "message": "hello",
                "groupInfo": {"groupId": "other-gid"},
            },
        }
        await bot._handle_message(envelope)
        agent.process.assert_not_called()
        bot._send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_dm(self):
        agent = AsyncMock()
        config = _signal_config()
        bot = MoseSignalBot(agent, config)
        bot._send_message = AsyncMock()
        envelope = {
            "source": "+1234567890",
            "dataMessage": {"message": "hello"},
        }
        await bot._handle_message(envelope)
        agent.process.assert_not_called()
        bot._send_message.assert_not_called()


@pytest.mark.asyncio
async def test_send_message_uses_group_id_only():
    """JSON-RPC send must use groupId, not recipient."""
    agent = AsyncMock()
    config = _signal_config()
    bot = MoseSignalBot(agent, config)
    recorded: list[tuple[str, dict | None]] = []

    async def capture_rpc(method: str, params: dict | None = None) -> dict:
        recorded.append((method, params))
        return {}

    bot._send_rpc = capture_rpc
    await bot._send_message("my-group-id", "hello")
    assert recorded
    assert recorded[0][0] == "send"
    assert recorded[0][1] is not None
    assert recorded[0][1].get("groupId") == "my-group-id"
    assert "recipient" not in recorded[0][1]


@pytest.mark.asyncio
async def test_handle_skill_approval_wrong_group():
    agent = MagicMock()
    config = _signal_config()
    bot = MoseSignalBot(agent, config)
    bot._send_message = AsyncMock()
    ok = await _handle_skill_approval_reply(bot, "wrong-gid", "approve foo")
    assert ok is False
    bot._send_message.assert_not_called()


@pytest.mark.asyncio
async def test_handle_skill_approval_admin_group_ignores_source_phone():
    agent = MagicMock()
    config = _signal_config(eng="eng-gid", adm="adm-gid")
    bot = MoseSignalBot(agent, config)
    bot._send_message = AsyncMock()
    agent.memory = MagicMock()
    agent.memory.list_pending_approvals.return_value = [
        MagicMock(slug="my-skill"),
    ]
    with patch("mose.learning.handle_skill_decision", new_callable=AsyncMock) as hsd:
        hsd.return_value = True
        ok = await _handle_skill_approval_reply(bot, "adm-gid", "approve my-skill")
    assert ok is True
    hsd.assert_called_once()
    bot._send_message.assert_called()


@pytest.mark.asyncio
async def test_sre_execute_prompt_goes_to_admin_and_notifies_engagement():
    """Approval callback posts the prompt to the admin group; engagement sees a short notice."""
    from mose.signal_bot import _signal_approval_callback, set_approval_context

    agent = AsyncMock()
    config = _signal_config(eng="eng-gid", adm="adm-gid")
    bot = MoseSignalBot(agent, config)
    sent: list[tuple[str, str]] = []

    async def capture_send(gid: str, text: str) -> None:
        sent.append((gid, text))

    bot._send_message = capture_send
    set_approval_context("eng-gid", bot)

    async def timeout_wait(_aw, _t):
        raise asyncio.TimeoutError()

    with patch("mose.signal_bot.asyncio.wait_for", timeout_wait):
        result = await _signal_approval_callback("ls -la", "debug", "prod")

    assert result is False
    assert any(g == "adm-gid" and "SRE Execute Approval" in t for g, t in sent)
    assert any(g == "eng-gid" and "Awaiting admin approval" in t for g, t in sent)
    assert any(g == "adm-gid" and "timed out" in t.lower() for g, t in sent)
