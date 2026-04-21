"""Classify MCP tools for Plex sidecars: read-only vs requires human approval.

Only servers registered in ``PROTECTED_MCP_SERVERS`` are gated. Other MCP
servers (e.g. paper_db) are left unrestricted so existing installs keep working.
"""

from __future__ import annotations

from typing import Literal

# Keys must match the ``servers`` entry names in ``mcp_servers.json``.
PROTECTED_MCP_SERVERS = frozenset({"plex-ops-admin", "plex-stack-automation"})

# vladimir-tutin/plex-mcp-server — read-only tools (everything else needs approval).
_PLEX_OPS_READS: frozenset[str] = frozenset({
    "library_list",
    "library_get_stats",
    "library_get_details",
    "library_get_recently_added",
    "library_get_contents",
    "media_search",
    "media_get_details",
    "media_get_artwork",
    "media_list_available_artwork",
    "playlist_list",
    "playlist_get_contents",
    "collection_list",
    "user_search_users",
    "user_list_all_users",
    "user_get_info",
    "user_get_on_deck",
    "user_get_continue_watching",
    "user_get_watch_history",
    "user_get_statistics",
    "sessions_get_active",
    "sessions_get_media_playback_history",
    "server_get_plex_logs",
    "server_get_info",
    "server_get_bandwidth",
    "server_get_current_resources",
    "server_get_butler_tasks",
    "server_get_alerts",
    "client_list",
    "client_get_details",
    "client_get_timelines",
})

# niavasha/plex-mcp-server — read-only tools (everything else needs approval).
# Conservative: ``trakt_sync_from_trakt`` is not allowlisted (may change remote state).
_PLEX_STACK_READS: frozenset[str] = frozenset({
    "get_libraries",
    "get_library_items",
    "search_media",
    "get_recently_added",
    "get_on_deck",
    "get_media_details",
    "get_editable_fields",
    "get_playlists",
    "get_playlist_items",
    "get_watchlist",
    "get_recently_watched",
    "get_watch_history",
    "get_fully_watched",
    "get_watch_stats",
    "get_user_stats",
    "get_library_stats",
    "get_popular_content",
    "get_recommendations",
    "sonarr_get_series",
    "sonarr_search",
    "sonarr_get_missing",
    "sonarr_get_queue",
    "sonarr_get_calendar",
    "sonarr_get_profiles",
    "radarr_get_movies",
    "radarr_search",
    "radarr_get_missing",
    "radarr_get_queue",
    "radarr_get_calendar",
    "radarr_get_profiles",
    "arr_get_status",
    "trakt_get_auth_status",
    "trakt_get_user_stats",
    "trakt_search",
    "trakt_get_sync_status",
})

_READ_BY_SERVER: dict[str, frozenset[str]] = {
    "plex-ops-admin": _PLEX_OPS_READS,
    "plex-stack-automation": _PLEX_STACK_READS,
}


def classify_mcp_tool(server: str, bare_tool: str) -> Literal["read", "write"]:
    """Return ``read`` if the tool may run without approval; otherwise ``write``.

    Callers must pass non-empty ``server`` and ``bare_tool`` (already stripped).
    """
    server = server.strip()
    bare_tool = bare_tool.strip()
    if not server or not bare_tool:
        return "write"
    if server not in PROTECTED_MCP_SERVERS:
        return "read"
    if bare_tool in _READ_BY_SERVER.get(server, frozenset()):
        return "read"
    return "write"


def use_tool_needs_approval(full_tool_name: str) -> bool:
    """True if ``use_tool`` should require the same approval flow as ``sre_execute``."""
    if "__" not in full_tool_name:
        return True
    server, bare = full_tool_name.split("__", 1)
    return classify_mcp_tool(server, bare) != "read"
