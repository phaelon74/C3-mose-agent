"""Tests for MCP read/write classification (Plex sidecars)."""

from __future__ import annotations

import pytest

from mose.mcp_write_policy import classify_mcp_tool, use_tool_needs_approval


@pytest.mark.parametrize(
    "bare,expected",
    [
        ("library_list", "read"),
        ("library_refresh", "write"),
        ("media_delete", "write"),
        ("server_get_info", "read"),
        ("client_control_playback", "write"),
    ],
)
def test_plex_ops_admin(bare: str, expected: str) -> None:
    assert classify_mcp_tool("plex-ops-admin", bare) == expected


@pytest.mark.parametrize(
    "bare,expected",
    [
        ("get_libraries", "read"),
        ("sonarr_get_queue", "read"),
        ("sonarr_add_series", "write"),
        ("export_library", "write"),
        ("trakt_sync_to_trakt", "write"),
    ],
)
def test_plex_stack_automation(bare: str, expected: str) -> None:
    assert classify_mcp_tool("plex-stack-automation", bare) == expected


@pytest.mark.parametrize(
    "bare,expected",
    [
        ("sonarr_get_queue", "read"),
        ("sonarr_delete_queue_item", "write"),
        ("sonarr_command_RssSync", "write"),
        ("sonarr_post_system_shutdown", "write"),
        ("sonarr_get_series_by_id", "read"),
    ],
)
def test_sonarr_diagnostics(bare: str, expected: str) -> None:
    assert classify_mcp_tool("sonarr-diagnostics", bare) == expected


@pytest.mark.parametrize(
    "bare,expected",
    [
        ("radarr_get_movie", "read"),
        ("radarr_delete_queue_item", "write"),
        ("radarr_command_RssSync", "write"),
        ("radarr_post_indexer_test", "write"),
        ("radarr_get_health", "read"),
    ],
)
def test_radarr_diagnostics(bare: str, expected: str) -> None:
    assert classify_mcp_tool("radarr-diagnostics", bare) == expected


def test_unprotected_server_always_read_for_policy() -> None:
    assert classify_mcp_tool("paper_db", "anything") == "read"


def test_use_tool_needs_approval() -> None:
    assert use_tool_needs_approval("plex-ops-admin__library_list") is False
    assert use_tool_needs_approval("plex-ops-admin__library_scan") is True
    assert use_tool_needs_approval("sonarr-diagnostics__sonarr_get_queue") is False
    assert use_tool_needs_approval("sonarr-diagnostics__sonarr_command_RssSync") is True
    assert use_tool_needs_approval("radarr-diagnostics__radarr_post_system_restart") is True
    assert use_tool_needs_approval("paper_db__foo") is False
    assert use_tool_needs_approval("notnamespaced") is True
    assert use_tool_needs_approval("plex-ops-admin__") is True
