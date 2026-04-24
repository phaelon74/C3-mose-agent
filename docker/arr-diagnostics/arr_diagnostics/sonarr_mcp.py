"""FastMCP server exposing sonarr_* tools (Sonarr API v3)."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from arr_diagnostics.client import ArrClient, json_response, safe_tool_decorator, truncate_output
from arr_diagnostics.sonarr_manual_import import manual_import_commit

SONARR_COMMANDS = frozenset({
    "ManualImport",
    "RescanSeries",
    "RefreshSeries",
    "DownloadedEpisodesScan",
    "RssSync",
    "RefreshMonitoredDownloads",
})


def sonarr_queue_import_execute(
    c: ArrClient,
    downloadId: str,
    seriesId: int,
    episodeIds: list[int],
    *,
    seasonNumber: int | None = None,
    episodeNumber: int | None = None,
    importMode: str | None = None,
    pathHints: list[str] | None = None,
) -> str:
    """Build payload and run :func:`sonarr_manual_import.manual_import_commit` (MCP tool + tests)."""
    body: dict[str, Any] = {
        "downloadId": downloadId,
        "seriesId": int(seriesId),
        "episodeIds": [int(x) for x in episodeIds],
    }
    if seasonNumber is not None:
        body["seasonNumber"] = int(seasonNumber)
    if episodeNumber is not None:
        body["episodeNumber"] = int(episodeNumber)
    if importMode is not None and str(importMode).strip():
        body["importMode"] = str(importMode).strip()
    if pathHints:
        body["pathHints"] = [str(x) for x in pathHints if str(x).strip()]
    return manual_import_commit(c, body)


def _post_episode_search_command(client: ArrClient, episode_ids: list[int]) -> str:
    """POST ``EpisodeSearch`` with explicit ids (Sonarr treats missing ``episodeIds`` as search-all-missing)."""
    if not episode_ids:
        return json.dumps({
            "error": "episodeIds_required",
            "detail": "Pass one or more Sonarr episode ids. Resolve SxxEyy via GET /episode before searching.",
        })
    return json_response(client.post_json("/command", {"name": "EpisodeSearch", "episodeIds": episode_ids}))


def _get_series_lookup(client: ArrClient, term: str) -> str:
    """GET ``/series/lookup`` with ``term`` (whitespace-stripped)."""
    t = term.strip()
    if not t:
        return json.dumps({
            "error": "term_required",
            "detail": "Pass a non-empty search string (series title).",
        })
    return json_response(client.get_json("/series/lookup", {"term": t}))


def build_sonarr_app(c: ArrClient) -> FastMCP:
    mcp = FastMCP("sonarr-diagnostics")
    # Every tool below is wrapped so httpx/connection errors return JSON instead
    # of tearing down the MCP stdio session (anyio.ClosedResourceError).
    tool = safe_tool_decorator(mcp.tool)

    @tool()
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

    @tool()
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

    @tool()
    def sonarr_get_queue_status() -> str:
        return json_response(c.get_json("/queue/status"))

    @tool()
    def sonarr_get_health() -> str:
        return json_response(c.get_json("/health"))

    @tool()
    def sonarr_get_log_file(filename: str | None = None) -> str:
        """Latest log file, or named file under /log/file/{filename}. Output capped (~200 lines / 20KB)."""
        if filename:
            raw = c.get_text(f"/log/file/{filename}")
        else:
            raw = c.get_text("/log/file")
        return truncate_output(raw)

    @tool()
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

    @tool()
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

    @tool()
    def sonarr_delete_queue_item(id: int) -> str:
        """DELETE /queue/{id}. Write — Mose approval when protected."""
        return json_response(c.delete_json(f"/queue/{id}"))

    @tool()
    def sonarr_post_queue_grab(id: int) -> str:
        """POST /queue/grab/{id}."""
        return json_response(c.post_json(f"/queue/grab/{id}", {}))

    @tool()
    def sonarr_post_queue_import(
        downloadId: str,
        seriesId: int,
        episodeIds: list[int],
        seasonNumber: int | None = None,
        episodeNumber: int | None = None,
        importMode: str | None = None,
        pathHints: list[str] | None = None,
    ) -> str:
        """Commit import for a queued/blocked release via Sonarr v3 **GET /manualimport → POST /manualimport (validate) → POST /command ManualImport**. Pass ``downloadId``, ``seriesId``, and ``episodeIds`` from the queue (not a JSON ``payload`` string). Optional: ``seasonNumber``, ``episodeNumber``, ``pathHints``, ``importMode`` (``auto``|``move``|``copy``). Halts with ``manualimport_rejected`` if Sonarr's validation returns rejections. Requires approval."""
        return sonarr_queue_import_execute(
            c,
            downloadId,
            seriesId,
            episodeIds,
            seasonNumber=seasonNumber,
            episodeNumber=episodeNumber,
            importMode=importMode,
            pathHints=pathHints,
        )

    def _command_tool(name: str):
        def _run() -> str:
            if name not in SONARR_COMMANDS:
                return json.dumps({"error": f"invalid command {name!r}", "allowed": sorted(SONARR_COMMANDS)})
            return json_response(c.post_json("/command", {"name": name}))

        _run.__name__ = f"sonarr_command_{name}"
        _run.__doc__ = f"POST /command with name={name!r}."
        return _run

    @tool()
    def sonarr_post_command_episode_search(episodeIds: list[int]) -> str:
        """POST /command ``EpisodeSearch`` for **specific Sonarr episode row ids only** (from ``sonarr_get_episode`` / ``sonarr_get_episode_by_id``). A parameterless ``EpisodeSearch`` in Sonarr searches *all* missing monitored episodes; that path is not exposed here on purpose. Requires approval."""
        return _post_episode_search_command(c, episodeIds)

    for _cmd in sorted(SONARR_COMMANDS):
        mcp.tool(name=f"sonarr_command_{_cmd}")(_command_tool(_cmd))

    @tool()
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

    @tool()
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

    @tool()
    def sonarr_get_episode_by_id(id: int) -> str:
        return json_response(c.get_json(f"/episode/{id}"))

    @tool()
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

    @tool()
    def sonarr_get_series() -> str:
        """GET /series — all series in the library (response can be large). Match ``title`` / ``sortTitle`` to find ``id``, then use ``sonarr_get_episode`` and ``sonarr_post_command_episode_search``."""
        return json_response(c.get_json("/series"))

    @tool()
    def sonarr_get_series_by_id(id: int) -> str:
        return json_response(c.get_json(f"/series/{id}"))

    @tool()
    def sonarr_get_series_folder(id: int) -> str:
        return json_response(c.get_json(f"/series/{id}/folder"))

    @tool()
    def sonarr_get_series_lookup(term: str) -> str:
        """GET /series/lookup — metadata search by title (Sonarr indexer/metadata; not your library list). Use to resolve a show name to ``tvdbId`` / canonical title, then match to a library ``id`` from ``sonarr_get_series`` or ``sonarr_get_series_by_id``."""
        return _get_series_lookup(c, term)

    @tool()
    def sonarr_get_diskspace() -> str:
        return json_response(c.get_json("/diskspace"))

    @tool()
    def sonarr_get_filesystem(path: str | None = None, allowFoldersWithoutTrailingSlashes: bool | None = None) -> str:
        params: dict[str, Any] = {}
        if path is not None:
            params["path"] = path
        if allowFoldersWithoutTrailingSlashes is not None:
            params["allowFoldersWithoutTrailingSlashes"] = allowFoldersWithoutTrailingSlashes
        return json_response(c.get_json("/filesystem", params or None))

    @tool()
    def sonarr_get_filesystem_mediafiles(path: str | None = None) -> str:
        params = {"path": path} if path else {}
        return json_response(c.get_json("/filesystem/mediafiles", params or None))

    @tool()
    def sonarr_get_system_status() -> str:
        return json_response(c.get_json("/system/status"))

    @tool()
    def sonarr_get_system_task() -> str:
        return json_response(c.get_json("/system/task"))

    @tool()
    def sonarr_get_system_task_by_id(id: int) -> str:
        return json_response(c.get_json(f"/system/task/{id}"))

    @tool()
    def sonarr_get_update() -> str:
        return json_response(c.get_json("/update"))

    @tool()
    def sonarr_post_system_restart() -> str:
        """[destructive] POST /system/restart."""
        return json_response(c.post_empty("/system/restart"))

    @tool()
    def sonarr_post_system_shutdown() -> str:
        """[destructive] POST /system/shutdown."""
        return json_response(c.post_empty("/system/shutdown"))

    @tool()
    def sonarr_get_indexers() -> str:
        return json_response(c.get_json("/indexer"))

    @tool()
    def sonarr_get_indexer(id: int) -> str:
        return json_response(c.get_json(f"/indexer/{id}"))

    @tool()
    def sonarr_post_indexer_test(id: int) -> str:
        """POST /indexer/test with body {{\"id\": id}}."""
        return json_response(c.post_json("/indexer/test", {"id": id}))

    @tool()
    def sonarr_get_downloadclients() -> str:
        return json_response(c.get_json("/downloadclient"))

    @tool()
    def sonarr_get_downloadclient(id: int) -> str:
        return json_response(c.get_json(f"/downloadclient/{id}"))

    @tool()
    def sonarr_post_downloadclient_test(id: int) -> str:
        """POST /downloadclient/test with body {{\"id\": id}}."""
        return json_response(c.post_json("/downloadclient/test", {"id": id}))

    return mcp
