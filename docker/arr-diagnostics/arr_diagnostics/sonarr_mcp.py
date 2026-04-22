"""FastMCP server exposing sonarr_* tools (Sonarr API v3)."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from arr_diagnostics.client import ArrClient, json_response, truncate_output
from arr_diagnostics.sonarr_manual_import import manual_import_commit

SONARR_COMMANDS = frozenset({
    "ManualImport",
    "RescanSeries",
    "RefreshSeries",
    "EpisodeSearch",
    "DownloadedEpisodesScan",
    "RssSync",
    "RefreshMonitoredDownloads",
})


def build_sonarr_app(c: ArrClient) -> FastMCP:
    mcp = FastMCP("sonarr-diagnostics")

    @mcp.tool()
    def sonarr_get_queue(
        page: int | None = None,
        pageSize: int | None = None,
        sortKey: str | None = None,
        sortDirection: str | None = None,
        includeUnknownSeriesItems: bool | None = None,
    ) -> str:
        """GET /queue. Optional paging/sort params."""
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        if pageSize is not None:
            params["pageSize"] = pageSize
        if sortKey is not None:
            params["sortKey"] = sortKey
        if sortDirection is not None:
            params["sortDirection"] = sortDirection
        if includeUnknownSeriesItems is not None:
            params["includeUnknownSeriesItems"] = includeUnknownSeriesItems
        return json_response(c.get_json("/queue", params or None))

    @mcp.tool()
    def sonarr_get_queue_details(
        seriesId: int | None = None,
        episodeId: int | None = None,
        includeSeries: bool | None = None,
        includeEpisode: bool | None = None,
    ) -> str:
        """GET /queue/details — optional series/episode filters."""
        params: dict[str, Any] = {}
        if seriesId is not None:
            params["seriesId"] = seriesId
        if episodeId is not None:
            params["episodeId"] = episodeId
        if includeSeries is not None:
            params["includeSeries"] = includeSeries
        if includeEpisode is not None:
            params["includeEpisode"] = includeEpisode
        return json_response(c.get_json("/queue/details", params or None))

    @mcp.tool()
    def sonarr_get_queue_status() -> str:
        return json_response(c.get_json("/queue/status"))

    @mcp.tool()
    def sonarr_get_health() -> str:
        return json_response(c.get_json("/health"))

    @mcp.tool()
    def sonarr_get_log_file(filename: str | None = None) -> str:
        """Latest log file, or named file under /log/file/{filename}. Output capped (~200 lines / 20KB)."""
        if filename:
            raw = c.get_text(f"/log/file/{filename}")
        else:
            raw = c.get_text("/log/file")
        return truncate_output(raw)

    @mcp.tool()
    def sonarr_get_log(
        page: int | None = None,
        pageSize: int | None = None,
    ) -> str:
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        if pageSize is not None:
            params["pageSize"] = pageSize
        return json_response(c.get_json("/log", params or None))

    @mcp.tool()
    def sonarr_get_manual_import(
        folder: str | None = None,
        downloadId: str | None = None,
        seriesId: int | None = None,
        filterExistingFiles: bool | None = None,
    ) -> str:
        """GET /manualimport — optional folder, downloadId, seriesId, filterExistingFiles."""
        params: dict[str, Any] = {}
        if folder is not None:
            params["folder"] = folder
        if downloadId is not None:
            params["downloadId"] = downloadId
        if seriesId is not None:
            params["seriesId"] = seriesId
        if filterExistingFiles is not None:
            params["filterExistingFiles"] = filterExistingFiles
        return json_response(c.get_json("/manualimport", params or None))

    @mcp.tool()
    def sonarr_delete_queue_item(id: int) -> str:
        """DELETE /queue/{id}. Write — Mose approval when protected."""
        return json_response(c.delete_json(f"/queue/{id}"))

    @mcp.tool()
    def sonarr_post_queue_grab(id: int) -> str:
        """POST /queue/grab/{id}."""
        return json_response(c.post_json(f"/queue/grab/{id}", {}))

    @mcp.tool()
    def sonarr_post_queue_import(payload: str) -> str:
        """Commit import for a queued/blocked release via Sonarr v3 **GET+POST /manualimport** (not ``/queue/import``, which stock Sonarr does not expose). ``payload`` JSON object: ``downloadId``, ``seriesId``, ``episodeIds`` (list). Legacy ``options`` keys are ignored. Requires approval — distinct from ``sonarr_command_ManualImport`` (background command)."""
        try:
            body = json.loads(payload)
        except json.JSONDecodeError as e:
            return json.dumps({"error": "invalid_json", "detail": str(e)})
        if not isinstance(body, dict):
            return json.dumps({"error": "payload_must_be_a_json_object"})
        return manual_import_commit(c, body)

    def _command_tool(name: str):
        def _run() -> str:
            if name not in SONARR_COMMANDS:
                return json.dumps({"error": f"invalid command {name!r}", "allowed": sorted(SONARR_COMMANDS)})
            return json_response(c.post_json("/command", {"name": name}))

        _run.__name__ = f"sonarr_command_{name}"
        _run.__doc__ = f"POST /command with name={name!r}."
        return _run

    for _cmd in sorted(SONARR_COMMANDS):
        mcp.tool(name=f"sonarr_command_{_cmd}")(_command_tool(_cmd))

    @mcp.tool()
    def sonarr_get_history(
        page: int | None = None,
        pageSize: int | None = None,
        sortKey: str | None = None,
        sortDirection: str | None = None,
    ) -> str:
        params: dict[str, Any] = {}
        if page is not None:
            params["page"] = page
        if pageSize is not None:
            params["pageSize"] = pageSize
        if sortKey is not None:
            params["sortKey"] = sortKey
        if sortDirection is not None:
            params["sortDirection"] = sortDirection
        return json_response(c.get_json("/history", params or None))

    @mcp.tool()
    def sonarr_get_episode(
        seriesId: int | None = None,
        episodeIds: str | None = None,
        episodeFileId: int | None = None,
        seasonNumber: int | None = None,
    ) -> str:
        """GET /episode — pass episodeIds as comma-separated ids if needed."""
        params: dict[str, Any] = {}
        if seriesId is not None:
            params["seriesId"] = seriesId
        if episodeIds is not None:
            params["episodeIds"] = episodeIds
        if episodeFileId is not None:
            params["episodeFileId"] = episodeFileId
        if seasonNumber is not None:
            params["seasonNumber"] = seasonNumber
        return json_response(c.get_json("/episode", params or None))

    @mcp.tool()
    def sonarr_get_episode_by_id(id: int) -> str:
        return json_response(c.get_json(f"/episode/{id}"))

    @mcp.tool()
    def sonarr_get_episode_files(
        seriesId: int | None = None,
        episodeFileIds: str | None = None,
    ) -> str:
        params: dict[str, Any] = {}
        if seriesId is not None:
            params["seriesId"] = seriesId
        if episodeFileIds is not None:
            params["episodeFileIds"] = episodeFileIds
        return json_response(c.get_json("/episodefile", params or None))

    @mcp.tool()
    def sonarr_get_series_by_id(id: int) -> str:
        return json_response(c.get_json(f"/series/{id}"))

    @mcp.tool()
    def sonarr_get_series_folder(id: int) -> str:
        return json_response(c.get_json(f"/series/{id}/folder"))

    @mcp.tool()
    def sonarr_get_diskspace() -> str:
        return json_response(c.get_json("/diskspace"))

    @mcp.tool()
    def sonarr_get_filesystem(path: str | None = None, allowFoldersWithoutTrailingSlashes: bool | None = None) -> str:
        params: dict[str, Any] = {}
        if path is not None:
            params["path"] = path
        if allowFoldersWithoutTrailingSlashes is not None:
            params["allowFoldersWithoutTrailingSlashes"] = allowFoldersWithoutTrailingSlashes
        return json_response(c.get_json("/filesystem", params or None))

    @mcp.tool()
    def sonarr_get_filesystem_mediafiles(path: str | None = None) -> str:
        params = {"path": path} if path else {}
        return json_response(c.get_json("/filesystem/mediafiles", params or None))

    @mcp.tool()
    def sonarr_get_system_status() -> str:
        return json_response(c.get_json("/system/status"))

    @mcp.tool()
    def sonarr_get_system_task() -> str:
        return json_response(c.get_json("/system/task"))

    @mcp.tool()
    def sonarr_get_system_task_by_id(id: int) -> str:
        return json_response(c.get_json(f"/system/task/{id}"))

    @mcp.tool()
    def sonarr_get_update() -> str:
        return json_response(c.get_json("/update"))

    @mcp.tool()
    def sonarr_post_system_restart() -> str:
        """[destructive] POST /system/restart."""
        return json_response(c.post_empty("/system/restart"))

    @mcp.tool()
    def sonarr_post_system_shutdown() -> str:
        """[destructive] POST /system/shutdown."""
        return json_response(c.post_empty("/system/shutdown"))

    @mcp.tool()
    def sonarr_get_indexers() -> str:
        return json_response(c.get_json("/indexer"))

    @mcp.tool()
    def sonarr_get_indexer(id: int) -> str:
        return json_response(c.get_json(f"/indexer/{id}"))

    @mcp.tool()
    def sonarr_post_indexer_test(id: int) -> str:
        """POST /indexer/test with body {{\"id\": id}}."""
        return json_response(c.post_json("/indexer/test", {"id": id}))

    @mcp.tool()
    def sonarr_get_downloadclients() -> str:
        return json_response(c.get_json("/downloadclient"))

    @mcp.tool()
    def sonarr_get_downloadclient(id: int) -> str:
        return json_response(c.get_json(f"/downloadclient/{id}"))

    @mcp.tool()
    def sonarr_post_downloadclient_test(id: int) -> str:
        """POST /downloadclient/test with body {{\"id\": id}}."""
        return json_response(c.post_json("/downloadclient/test", {"id": id}))

    return mcp
