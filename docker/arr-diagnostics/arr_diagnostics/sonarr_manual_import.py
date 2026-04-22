"""Sonarr v3 manual import commit: GET /manualimport → POST /manualimport.

Upstream Sonarr exposes ``POST /api/v3/manualimport`` with an array of
``ManualImportReprocessResource``. The non-standard ``POST /queue/import`` route
used elsewhere returns **405** on stock Sonarr builds.
"""

from __future__ import annotations

import json
import re
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
    season_num = payload_dict.get("seasonNumber")
    episode_num = payload_dict.get("episodeNumber")
    raw_hints = payload_dict.get("pathHints")
    path_hints: list[str] | None = None
    if isinstance(raw_hints, list):
        path_hints = [str(x).strip() for x in raw_hints if str(x).strip()]
        if not path_hints:
            path_hints = None
    prep = _prepare_row(
        c,
        str(download_id),
        sid,
        eids[0],
        season_number=int(season_num) if season_num is not None else None,
        episode_number=int(episode_num) if episode_num is not None else None,
        path_hints=path_hints,
    )
    if isinstance(prep, str):
        return prep
    _rows, reprocess = prep
    return c.post_json_documented_error("/manualimport", [reprocess])


def prepare_manual_import_payload(
    c: ArrClient,
    download_id: str,
    series_id: int,
    episode_id: int,
    *,
    season_number: int | None = None,
    episode_number: int | None = None,
    path_hints: list[str] | None = None,
) -> tuple[list[Any], dict[str, Any]] | str:
    """Return ``(manualimport GET rows, single POST body element)`` or error JSON string."""
    return _prepare_row(
        c,
        download_id,
        series_id,
        episode_id,
        season_number=season_number,
        episode_number=episode_number,
        path_hints=path_hints,
    )


def _prepare_row(
    c: ArrClient,
    download_id: str,
    series_id: int,
    episode_id: int,
    *,
    season_number: int | None = None,
    episode_number: int | None = None,
    path_hints: list[str] | None = None,
) -> tuple[list[Any], dict[str, Any]] | str:
    params: dict[str, Any] = {"downloadId": download_id, "seriesId": series_id}
    if season_number is not None:
        params["seasonNumber"] = season_number
    rows_any = c.get_json("/manualimport", params)
    if not rows_any:
        return json.dumps({
            "error": "manualimport_get_empty",
            "hint": "Nothing returned for this downloadId/seriesId — release may have left the queue.",
        })
    rows = rows_any if isinstance(rows_any, list) else [rows_any]
    row = _pick_manual_row(
        rows,
        series_id,
        episode_id,
        download_id=download_id,
        season_number=season_number,
        episode_number=episode_number,
        path_hints=path_hints,
    )
    if row is None:
        return json.dumps({
            "error": "no_matching_manualimport_row",
            "candidates_after_get": len(rows),
            "hint": (
                "Rebuild sonarr-diagnostics so scripts pass pathHints; or ensure seasonNumber/episodeNumber "
                "match nested episodes on /manualimport rows. Rows often map by S/E on episodes[], not id."
            ),
        })
    return rows, _to_reprocess(row, [episode_id])


def _manual_row_path_blob(row: dict[str, Any]) -> str:
    """Concatenate filename-related strings for regex / substring matching."""
    parts: list[str] = []
    for k in ("path", "relativePath", "name", "folderName", "releaseGroup"):
        v = row.get(k)
        if isinstance(v, str) and v:
            parts.append(v)
    for er in row.get("episodes") or []:
        if isinstance(er, dict):
            for k in ("title", "releaseTitle"):
                tv = er.get(k)
                if isinstance(tv, str) and tv:
                    parts.append(tv)
    blob = "\0".join(parts).replace("\\", "/").lower()
    return blob


def _pick_manual_row(
    rows: list[Any],
    series_id: int,
    episode_id: int,
    *,
    download_id: str | None = None,
    season_number: int | None,
    episode_number: int | None,
    path_hints: list[str] | None,
) -> dict[str, Any] | None:
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

    def season_ok(row: dict[str, Any]) -> bool:
        if season_number is None:
            return True
        if row.get("seasonNumber") is not None and int(row["seasonNumber"]) == season_number:
            return True
        for er in row.get("episodes") or []:
            if isinstance(er, dict) and er.get("seasonNumber") == season_number:
                return True
        return False

    narrowed = [r for r in pool if season_ok(r)] if season_number is not None else pool

    # 0) Prefer rows whose downloadId matches the GET query (multi-file releases)
    if download_id and str(download_id).strip():
        q = str(download_id).strip().lower()
        with_did = [
            r for r in narrowed
            if str(r.get("downloadId") or "").strip().lower() == q
        ]
        if with_did:
            narrowed = with_did

    # 1) Episode id appears on a nested episode (correct row for this file)
    for row in narrowed:
        for er in row.get("episodes") or []:
            if isinstance(er, dict) and er.get("id") == episode_id:
                return row

    # 2) Nested episodes match season/episode numbers (Sonarr often sets S/E before DB id lines up)
    if season_number is not None and episode_number is not None:
        for row in narrowed:
            for er in row.get("episodes") or []:
                if not isinstance(er, dict):
                    continue
                sn = er.get("seasonNumber")
                en = er.get("episodeNumber")
                if sn is None or en is None:
                    continue
                if int(sn) == season_number and int(en) == episode_number:
                    return row

    # 3) Row-level season/episode (some payloads expose one episode flat on the row)
    if season_number is not None and episode_number is not None:
        for row in narrowed:
            rs = row.get("seasonNumber")
            re_ = row.get("episodeNumber")
            if rs is None or re_ is None:
                continue
            if int(rs) == season_number and int(re_) == episode_number:
                return row

    # 4) Path matches SxxEyy, ``4x26``, etc.
    if season_number is not None and episode_number is not None:
        patterns = [
            rf"[Ss]{season_number:02d}[Ee]{episode_number:02d}",
            rf"[Ss]{season_number}[Ee]{episode_number:02d}\b",
            rf"[Ss]{season_number:02d}[Ee]{episode_number}\b",
            rf"[Ss]{season_number}[Ee]{episode_number}\b",
            rf"(?i)\b{season_number}[xX]{episode_number}\b",
        ]
        for row in narrowed:
            path = _manual_row_path_blob(row)
            for pat in patterns:
                if re.search(pat, path, re.I):
                    return row

    # 5) Queue-derived path hints (title / outputPath from Activity)
    if path_hints:
        best: dict[str, Any] | None = None
        best_score = 0
        for row in narrowed:
            blob = _manual_row_path_blob(row)
            for hint in sorted({h.strip() for h in path_hints if len(h.strip()) > 3}, key=len, reverse=True):
                hl = hint.lower().strip().replace("\\", "/")
                _token = re.fullmatch(r"s\d{1,2}e\d{1,3}", hl)
                min_len = 4 if _token else 8
                if len(hl) >= min_len and hl in blob:
                    if len(hl) > best_score:
                        best_score = len(hl)
                        best = row
        if best is not None:
            return best

    # 6) Single candidate after season filter
    if len(narrowed) == 1:
        return narrowed[0]

    # 7) Single candidate overall (only safe ambiguous case)
    if len(pool) == 1:
        return pool[0]

    return None


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
    # Drop nested episodes that are not the chosen ids (avoids posting S01E01 with episodeIds [S04E26])
    eps = out.get("episodes")
    if isinstance(eps, list):
        out["episodes"] = [e for e in eps if isinstance(e, dict) and e.get("id") in episode_ids]
        if not out["episodes"]:
            del out["episodes"]
    return out
