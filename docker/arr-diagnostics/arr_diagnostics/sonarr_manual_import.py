"""Sonarr v3 manual import commit: GET /manualimport → POST /manualimport.

Upstream Sonarr exposes ``POST /api/v3/manualimport`` with an array of
``ManualImportReprocessResource``. The non-standard ``POST /queue/import`` route
used elsewhere returns **405** on stock Sonarr builds.
"""

from __future__ import annotations

import json
from typing import Any

from arr_diagnostics.client import ArrClient

_REPROCESS_KEYS = frozenset({
    "id",
    "path",
    "seriesId",
    "seasonNumber",
    "episodes",
    "quality",
    "languages",
    "releaseGroup",
    "downloadId",
    "customFormats",
    "customFormatScore",
    "indexerFlags",
    "releaseType",
    "rejections",
})


def post_manual_import_reprocess(c: ArrClient, reprocess: dict[str, Any]) -> str:
    """POST a single ``ManualImportReprocessResource`` (wrapped in a one-element array)."""
    return c.post_json_documented_error("/manualimport", [reprocess])


def manual_import_commit(c: ArrClient, payload_dict: dict[str, Any]) -> str:
    """Import a queued download via GET /manualimport then POST /manualimport.

    ``payload_dict`` uses the same logical fields as the old queue/import helper:
    ``downloadId``, ``seriesId``, ``episodeIds`` (non-empty list). Nested
    ``options`` is ignored (copy/move is chosen in Sonarr UI / defaults).
    """
    download_id = payload_dict.get("downloadId")
    series_id = payload_dict.get("seriesId")
    episode_ids = payload_dict.get("episodeIds")
    if download_id is None or series_id is None:
        return json.dumps({"error": "missing_fields", "need": ["downloadId", "seriesId", "episodeIds"]})
    if not episode_ids or not isinstance(episode_ids, list):
        return json.dumps({"error": "episodeIds_must_be_non_empty_list"})
    sid = int(series_id)
    eids = [int(x) for x in episode_ids]
    prep = _prepare_row(c, str(download_id), sid, eids[0])
    if isinstance(prep, str):
        return prep
    _rows, reprocess = prep
    return c.post_json_documented_error("/manualimport", [reprocess])


def prepare_manual_import_payload(
    c: ArrClient,
    download_id: str,
    series_id: int,
    episode_id: int,
) -> tuple[list[Any], dict[str, Any]] | str:
    """Return ``(manualimport GET rows, single POST body element)`` or error JSON string."""
    return _prepare_row(c, download_id, series_id, episode_id)


def _prepare_row(
    c: ArrClient,
    download_id: str,
    series_id: int,
    episode_id: int,
) -> tuple[list[Any], dict[str, Any]] | str:
    rows_any = c.get_json(
        "/manualimport",
        {"downloadId": download_id, "seriesId": series_id},
    )
    if not rows_any:
        return json.dumps({
            "error": "manualimport_get_empty",
            "hint": "Nothing returned for this downloadId/seriesId — release may have left the queue.",
        })
    rows = rows_any if isinstance(rows_any, list) else [rows_any]
    row = _pick_manual_row(rows, series_id, episode_id)
    if row is None:
        return json.dumps({"error": "no_matching_manualimport_row", "candidates": len(rows)})
    return rows, _to_reprocess(row, [episode_id])


def _pick_manual_row(rows: list[Any], series_id: int, episode_id: int) -> dict[str, Any] | None:
    scoped: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("seriesId")
        if sid is None and isinstance(row.get("series"), dict):
            sid = row["series"].get("id")
        if sid is not None and int(sid) != series_id:
            continue
        scoped.append(row)
    pool = scoped or [r for r in rows if isinstance(r, dict)]

    for row in pool:
        for er in row.get("episodes") or []:
            if isinstance(er, dict) and er.get("id") == episode_id:
                return row
    if len(pool) == 1:
        return pool[0]
    return pool[0] if pool else None


def _to_reprocess(row: dict[str, Any], episode_ids: list[int]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in _REPROCESS_KEYS:
        if k in row and row[k] is not None:
            out[k] = row[k]
    if "seriesId" not in out:
        ser = row.get("series")
        if isinstance(ser, dict) and ser.get("id") is not None:
            out["seriesId"] = int(ser["id"])
    out["episodeIds"] = episode_ids
    return out
