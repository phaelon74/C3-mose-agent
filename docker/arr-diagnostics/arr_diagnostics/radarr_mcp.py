"""FastMCP server exposing radarr_* tools (Radarr API v3)."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from arr_diagnostics.client import ArrClient, json_response, safe_tool_decorator, truncate_output
from arr_diagnostics.radarr_manual_import import manual_import_commit as radarr_manual_import_commit

RADARR_COMMANDS = frozenset({
    "ManualImport",
    "RefreshMovie",
    "MoviesSearch",
    "DownloadedMoviesScan",
    "RssSync",
    "RefreshMonitoredDownloads",
})


def radarr_manual_import_missing_scope_error(
    folder: str | None,
    download_id: str | None,
    movie_id: int | None,
) -> str | None:
    """If all scope params are absent, return a JSON error string; else ``None``."""
    if folder is None and download_id is None and movie_id is None:
        return json.dumps({
            "error": "missing_scope",
            "hint": (
                "Pass downloadId (from queue), movieId, or folder — unscoped GET /manualimport "
                "can return 500 on Radarr."
            ),
        })
    return None


def radarr_queue_import_execute(
    c: ArrClient,
    downloadId: str,
    movieId: int,
    *,
    importMode: str | None = None,
    pathHints: list[str] | None = None,
) -> str:
    """Build payload and run ``manual_import_commit`` from ``radarr_manual_import`` (MCP tool + tests)."""
    body: dict[str, Any] = {"downloadId": downloadId, "movieId": int(movieId)}
    if importMode is not None and str(importMode).strip():
        body["importMode"] = str(importMode).strip()
    if pathHints:
        body["pathHints"] = [str(x) for x in pathHints if str(x).strip()]
    return radarr_manual_import_commit(c, body)


def build_radarr_app(c: ArrClient) -> FastMCP:
    mcp = FastMCP("radarr-diagnostics")
    # See sonarr_mcp.build_sonarr_app: wrap every tool so API errors return JSON
    # instead of crashing the MCP stdio session (anyio.ClosedResourceError).
    tool = safe_tool_decorator(mcp.tool)

    @tool()
    def radarr_get_queue(
        page: int | None = None,
        pageSize: int | None = None,
        sortKey: str | None = None,
        sortDirection: str | None = None,
        includeUnknownMovieItems: bool | None = None,
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
        if includeUnknownMovieItems is not None:
            params["includeUnknownMovieItems"] = includeUnknownMovieItems
        return json_response(c.get_json("/queue", params or None))

    @tool()
    def radarr_get_queue_details(
        movieId: int | None = None,
        includeMovie: bool | None = None,
    ) -> str:
        params: dict[str, Any] = {}
        if movieId is not None:
            params["movieId"] = movieId
        if includeMovie is not None:
            params["includeMovie"] = includeMovie
        return json_response(c.get_json("/queue/details", params or None))

    @tool()
    def radarr_get_queue_status() -> str:
        return json_response(c.get_json("/queue/status"))

    @tool()
    def radarr_get_health() -> str:
        return json_response(c.get_json("/health"))

    @tool()
    def radarr_get_log_file(filename: str | None = None) -> str:
        if filename:
            raw = c.get_text(f"/log/file/{filename}")
        else:
            raw = c.get_text("/log/file")
        return truncate_output(raw)

    @tool()
    def radarr_get_log(
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
    def radarr_get_manual_import(
        folder: str | None = None,
        downloadId: str | None = None,
        movieId: int | None = None,
        filterExistingFiles: bool | None = None,
    ) -> str:
        """GET /manualimport — pass folder, downloadId, and/or movieId (unscoped GET can 500)."""
        scope_err = radarr_manual_import_missing_scope_error(folder, downloadId, movieId)
        if scope_err is not None:
            return scope_err
        params: dict[str, Any] = {}
        if folder is not None:
            params["folder"] = folder
        if downloadId is not None:
            params["downloadId"] = downloadId
        if movieId is not None:
            params["movieId"] = movieId
        if filterExistingFiles is not None:
            params["filterExistingFiles"] = filterExistingFiles
        return json_response(c.get_json("/manualimport", params or None))

    @tool()
    def radarr_delete_queue_item(id: int) -> str:
        return json_response(c.delete_json(f"/queue/{id}"))

    @tool()
    def radarr_post_queue_grab(id: int) -> str:
        return json_response(c.post_json(f"/queue/grab/{id}", {}))

    @tool()
    def radarr_post_queue_import(
        downloadId: str,
        movieId: int,
        importMode: str | None = None,
        pathHints: list[str] | None = None,
    ) -> str:
        """Commit import for a queued/blocked movie via **GET /manualimport → POST /manualimport → POST /command ManualImport** (Radarr v3). Pass ``downloadId`` and ``movieId`` from the queue (not a JSON ``payload`` string). Optional: ``importMode`` (``auto``|``move``|``copy``), ``pathHints`` (strings). Halts with ``manualimport_rejected`` if reprocess returns rejections. Requires approval."""
        return radarr_queue_import_execute(
            c,
            downloadId,
            movieId,
            importMode=importMode,
            pathHints=pathHints,
        )

    @tool()
    def radarr_post_manual_import(payload: str) -> str:
        """POST /manualimport — body is a JSON array of ``ManualImportReprocessResource`` (reprocess/validate only). To commit a queue download end-to-end, use ``radarr_post_queue_import`` with ``downloadId`` and ``movieId``. ``radarr_command_ManualImport`` is a no-arg task trigger, not a per-file commit."""
        try:
            body = json.loads(payload)
        except json.JSONDecodeError as e:
            return json.dumps({"error": "invalid_json", "detail": str(e)})
        if not isinstance(body, list):
            return json.dumps({"error": "payload_must_be_a_json_array"})
        return c.post_json_documented_error("/manualimport", body)

    def _command_tool(name: str):
        def _run() -> str:
            if name not in RADARR_COMMANDS:
                return json.dumps({"error": f"invalid command {name!r}", "allowed": sorted(RADARR_COMMANDS)})
            return json_response(c.post_json("/command", {"name": name}))

        _run.__name__ = f"radarr_command_{name}"
        _run.__doc__ = f"POST /command with name={name!r}."
        return _run

    for _cmd in sorted(RADARR_COMMANDS):
        mcp.tool(name=f"radarr_command_{_cmd}")(_command_tool(_cmd))

    @tool()
    def radarr_get_history(
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
    def radarr_get_movie(
        tmdbId: int | None = None,
        excludeLocalCovers: bool | None = None,
        languageId: int | None = None,
    ) -> str:
        """GET /movie — warning: empty query returns full library (may be slow/large)."""
        params: dict[str, Any] = {}
        if tmdbId is not None:
            params["tmdbId"] = tmdbId
        if excludeLocalCovers is not None:
            params["excludeLocalCovers"] = excludeLocalCovers
        if languageId is not None:
            params["languageId"] = languageId
        return json_response(c.get_json("/movie", params or None))

    @tool()
    def radarr_get_movie_by_id(id: int) -> str:
        return json_response(c.get_json(f"/movie/{id}"))

    @tool()
    def radarr_get_movie_files(movieId: int | None = None) -> str:
        params = {"movieId": movieId} if movieId is not None else {}
        return json_response(c.get_json("/moviefile", params or None))

    @tool()
    def radarr_get_movie_folder(id: int) -> str:
        return json_response(c.get_json(f"/movie/{id}/folder"))

    @tool()
    def radarr_get_diskspace() -> str:
        return json_response(c.get_json("/diskspace"))

    @tool()
    def radarr_get_filesystem(path: str | None = None, allowFoldersWithoutTrailingSlashes: bool | None = None) -> str:
        params: dict[str, Any] = {}
        if path is not None:
            params["path"] = path
        if allowFoldersWithoutTrailingSlashes is not None:
            params["allowFoldersWithoutTrailingSlashes"] = allowFoldersWithoutTrailingSlashes
        return json_response(c.get_json("/filesystem", params or None))

    @tool()
    def radarr_get_filesystem_mediafiles(path: str | None = None) -> str:
        params = {"path": path} if path else {}
        return json_response(c.get_json("/filesystem/mediafiles", params or None))

    @tool()
    def radarr_get_system_status() -> str:
        return json_response(c.get_json("/system/status"))

    @tool()
    def radarr_get_system_task() -> str:
        return json_response(c.get_json("/system/task"))

    @tool()
    def radarr_get_system_task_by_id(id: int) -> str:
        return json_response(c.get_json(f"/system/task/{id}"))

    @tool()
    def radarr_get_update() -> str:
        return json_response(c.get_json("/update"))

    @tool()
    def radarr_post_system_restart() -> str:
        """[destructive] POST /system/restart."""
        return json_response(c.post_empty("/system/restart"))

    @tool()
    def radarr_post_system_shutdown() -> str:
        """[destructive] POST /system/shutdown."""
        return json_response(c.post_empty("/system/shutdown"))

    @tool()
    def radarr_get_indexers() -> str:
        return json_response(c.get_json("/indexer"))

    @tool()
    def radarr_get_indexer(id: int) -> str:
        return json_response(c.get_json(f"/indexer/{id}"))

    @tool()
    def radarr_post_indexer_test(id: int) -> str:
        return json_response(c.post_json("/indexer/test", {"id": id}))

    @tool()
    def radarr_get_downloadclients() -> str:
        return json_response(c.get_json("/downloadclient"))

    @tool()
    def radarr_get_downloadclient(id: int) -> str:
        return json_response(c.get_json(f"/downloadclient/{id}"))

    @tool()
    def radarr_post_downloadclient_test(id: int) -> str:
        return json_response(c.post_json("/downloadclient/test", {"id": id}))

    return mcp
