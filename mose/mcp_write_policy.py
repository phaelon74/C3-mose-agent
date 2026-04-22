"""Classify MCP tools for gated MCP servers: read-only vs requires human approval.

Servers in ``PROTECTED_MCP_SERVERS`` (Plex sidecars, Sonarr/Radarr diagnostics)
use per-server read allowlists. Other MCP servers (e.g. paper_db) are left
unrestricted so existing installs keep working.
"""

from __future__ import annotations

from typing import Literal

# Keys must match the ``servers`` entry names in ``mcp_servers.json``.
PROTECTED_MCP_SERVERS = frozenset({
    "plex-ops-admin",
    "plex-stack-automation",
    "sonarr-diagnostics",
    "radarr-diagnostics",
})

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

# docker/arr-diagnostics Sonarr MCP — GET-only tools (everything else requires approval).
_SONARR_DIAG_READS: frozenset[str] = frozenset({
    "sonarr_get_queue",
    "sonarr_get_queue_details",
    "sonarr_get_queue_status",
    "sonarr_get_health",
    "sonarr_get_log_file",
    "sonarr_get_log",
    "sonarr_get_manual_import",
    "sonarr_get_history",
    "sonarr_get_episode",
    "sonarr_get_episode_by_id",
    "sonarr_get_episode_files",
    "sonarr_get_series",
    "sonarr_get_series_by_id",
    "sonarr_get_series_folder",
    "sonarr_get_diskspace",
    "sonarr_get_filesystem",
    "sonarr_get_filesystem_mediafiles",
    "sonarr_get_system_status",
    "sonarr_get_system_task",
    "sonarr_get_system_task_by_id",
    "sonarr_get_update",
    "sonarr_get_indexers",
    "sonarr_get_indexer",
    "sonarr_get_downloadclients",
    "sonarr_get_downloadclient",
})

# docker/arr-diagnostics Radarr MCP — GET-only tools.
_RADARR_DIAG_READS: frozenset[str] = frozenset({
    "radarr_get_queue",
    "radarr_get_queue_details",
    "radarr_get_queue_status",
    "radarr_get_health",
    "radarr_get_log_file",
    "radarr_get_log",
    "radarr_get_manual_import",
    "radarr_get_history",
    "radarr_get_movie",
    "radarr_get_movie_by_id",
    "radarr_get_movie_files",
    "radarr_get_movie_folder",
    "radarr_get_diskspace",
    "radarr_get_filesystem",
    "radarr_get_filesystem_mediafiles",
    "radarr_get_system_status",
    "radarr_get_system_task",
    "radarr_get_system_task_by_id",
    "radarr_get_update",
    "radarr_get_indexers",
    "radarr_get_indexer",
    "radarr_get_downloadclients",
    "radarr_get_downloadclient",
})

_READ_BY_SERVER: dict[str, frozenset[str]] = {
    "plex-ops-admin": _PLEX_OPS_READS,
    "plex-stack-automation": _PLEX_STACK_READS,
    "sonarr-diagnostics": _SONARR_DIAG_READS,
    "radarr-diagnostics": _RADARR_DIAG_READS,
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
