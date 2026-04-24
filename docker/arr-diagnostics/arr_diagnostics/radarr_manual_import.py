"""Radarr v3 manual import commit: GET /manualimport → POST /manualimport → POST /command.

1. ``GET /api/v3/manualimport?downloadId=<id>`` returns ``ManualImportResource`` rows.
2. ``POST /api/v3/manualimport`` **reprocesses** rows (``ManualImportController.ReprocessItems``);
   it does **not** import by itself.
3. Commit is ``POST /api/v3/command`` with ``name: ManualImport``, ``importMode``, and ``files``
   (``ManualImportFile``: path, folderName, quality, languages, releaseGroup, indexerFlags,
   downloadId, movieId).

``POST /queue/import`` is not part of Radarr's published v3 API; use this pipeline instead.
"""

from __future__ import annotations

import json
import re
from typing import Any

from arr_diagnostics.client import ArrClient
from arr_diagnostics.sonarr_manual_import import _expand_path_hints, _manual_row_hint_blob

_REPROCESS_KEYS = frozenset({
    "id",
    "path",
    "movieId",
    "quality",
    "languages",
    "releaseGroup",
    "downloadId",
    "customFormats",
    "customFormatScore",
    "indexerFlags",
    "rejections",
})

_RADARR_COMMAND_FILE_KEYS = (
    "path",
    "folderName",
    "quality",
    "languages",
    "releaseGroup",
    "downloadId",
    "indexerFlags",
    "movieId",
)


def _movie_id_from_row(row: dict[str, Any]) -> int | None:
    mid = row.get("movieId")
    if mid is not None:
        try:
            return int(mid)
        except (TypeError, ValueError):
            pass
    movie = row.get("movie")
    if isinstance(movie, dict) and movie.get("id") is not None:
        try:
            return int(movie["id"])
        except (TypeError, ValueError):
            pass
    return None


def build_radarr_manual_import_command_file(validated_row: dict[str, Any]) -> dict[str, Any]:
    """Build a ``ManualImportFile`` payload for ``POST /command`` (name ``ManualImport``)."""
    out: dict[str, Any] = {}
    for k in _RADARR_COMMAND_FILE_KEYS:
        if k in validated_row and validated_row[k] is not None:
            out[k] = validated_row[k]
    mid = _movie_id_from_row(validated_row)
    if mid is not None:
        out["movieId"] = mid
    return out


def execute_radarr_manual_import_command(
    c: ArrClient,
    files: list[dict[str, Any]],
    *,
    import_mode: str = "auto",
) -> str:
    """Fire ``POST /command`` with ``name=ManualImport`` to commit the import."""
    body = {
        "name": "ManualImport",
        "importMode": import_mode,
        "files": files,
    }
    return c.post_json_documented_error("/command", body)


def manual_import_commit(c: ArrClient, payload_dict: dict[str, Any]) -> str:
    """Import a queued movie download: GET /manualimport → POST /manualimport → POST /command.

    ``payload_dict`` requires ``downloadId`` and ``movieId`` (from the queue / queue details).
    Optional: ``importMode`` (``auto`` | ``move`` | ``copy``), ``pathHints`` (list of strings).
    """
    download_id = payload_dict.get("downloadId")
    movie_id_raw = payload_dict.get("movieId")
    if download_id is None or movie_id_raw is None:
        return json.dumps({"error": "missing_fields", "need": ["downloadId", "movieId"]})
    try:
        movie_id = int(movie_id_raw)
    except (TypeError, ValueError):
        return json.dumps({"error": "movieId_must_be_int", "got": repr(movie_id_raw)})

    import_mode = str(payload_dict.get("importMode") or "auto").lower()
    if import_mode not in {"auto", "move", "copy"}:
        import_mode = "auto"

    raw_hints = payload_dict.get("pathHints")
    path_hints: list[str] | None = None
    if isinstance(raw_hints, list):
        path_hints = [str(x).strip() for x in raw_hints if str(x).strip()]
        if not path_hints:
            path_hints = None

    prep = _prepare_row(
        c,
        str(download_id),
        movie_id,
        path_hints=path_hints,
    )
    if isinstance(prep, str):
        return prep
    _rows, reprocess = prep

    reprocess_raw = c.post_json_documented_error("/manualimport", [reprocess])
    try:
        validated = json.loads(reprocess_raw)
    except json.JSONDecodeError:
        return json.dumps({
            "error": "manualimport_reprocess_unparseable",
            "raw": reprocess_raw[:2000],
        })
    if isinstance(validated, dict) and validated.get("error"):
        return reprocess_raw

    validated_rows = validated if isinstance(validated, list) else [validated]
    if not validated_rows or not isinstance(validated_rows[0], dict):
        return json.dumps({
            "error": "manualimport_reprocess_no_rows",
            "hint": "Radarr returned no rows from POST /manualimport.",
        })
    row0 = validated_rows[0]
    rejections = list(row0.get("rejections") or [])
    if rejections:
        return json.dumps({
            "error": "manualimport_rejected",
            "rejections": rejections,
            "hint": (
                "Radarr rejected the row during reprocess. Fix mapping or monitored state in "
                "the Radarr UI, then retry."
            ),
        })

    file_payload = build_radarr_manual_import_command_file(row0)
    if not file_payload.get("path"):
        return json.dumps({
            "error": "manualimport_no_path",
            "hint": "Validated row had no file path for ManualImport command.",
        })
    return execute_radarr_manual_import_command(c, [file_payload], import_mode=import_mode)


def _prepare_row(
    c: ArrClient,
    download_id: str,
    movie_id: int,
    *,
    path_hints: list[str] | None = None,
) -> tuple[list[Any], dict[str, Any]] | str:
    # Radarr: use downloadId alone so we get the download-scoped rows (same pitfall as Sonarr
    # when mixing seriesId/movieId with downloadId on GET).
    rows_any = c.get_json("/manualimport", {"downloadId": download_id})
    if not rows_any:
        return json.dumps({
            "error": "manualimport_get_empty",
            "hint": (
                "GET /manualimport?downloadId=... returned nothing — release may have left "
                "the queue."
            ),
        })
    rows = rows_any if isinstance(rows_any, list) else [rows_any]
    scoped_count = sum(
        1
        for r in rows
        if isinstance(r, dict) and _movie_id_from_row(r) == movie_id
    )
    row = _pick_manual_movie_row(rows, movie_id, path_hints=path_hints)
    if row is None:
        if scoped_count > 1 and not path_hints:
            return json.dumps({
                "error": "ambiguous_manualimport_rows",
                "candidates_same_movie": scoped_count,
                "hint": (
                    "Multiple files for this movie in the download; pass pathHints (tokens from "
                    "the release folder or filename) to pick one."
                ),
            })
        return json.dumps({
            "error": "no_matching_manualimport_row",
            "candidates_after_get": len(rows),
            "hint": (
                "GET /manualimport?downloadId=<id> returned rows but none matched movieId. "
                "Try pathHints from the queue release title or verify movieId."
            ),
        })
    return rows, _to_reprocess(row)


def _pick_manual_movie_row(
    rows: list[Any],
    movie_id: int,
    *,
    path_hints: list[str] | None,
) -> dict[str, Any] | None:
    scoped: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rid = _movie_id_from_row(row)
        if rid is not None and rid == movie_id:
            scoped.append(row)
    if not scoped:
        return None

    if path_hints:
        best: dict[str, Any] | None = None
        best_score = 0
        expanded = _expand_path_hints(path_hints)
        for row in scoped:
            blob = _manual_row_hint_blob(row)
            fold_blob = re.sub(r"[^a-z0-9]+", "", blob)
            for hint in expanded:
                hl = hint.lower().strip().replace("\\", "/")
                token = re.fullmatch(r"s\d{1,2}e\d{1,3}", hl)
                min_len = 4 if token else 8
                scored = 0
                if len(hl) >= min_len and hl in blob:
                    scored = len(hl)
                else:
                    hf = re.sub(r"[^a-z0-9]+", "", hl)
                    if len(hf) >= 14 and hf in fold_blob:
                        scored = len(hf)
                if scored > best_score:
                    best_score = scored
                    best = row
        if best is not None:
            return best

    if len(scoped) == 1:
        return scoped[0]

    return None


def _to_reprocess(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in _REPROCESS_KEYS:
        if k in row and row[k] is not None:
            out[k] = row[k]
    if "movieId" not in out:
        mid = _movie_id_from_row(row)
        if mid is not None:
            out["movieId"] = mid
    if "indexerFlags" not in out:
        out["indexerFlags"] = 0
    return out


__all__ = [
    "build_radarr_manual_import_command_file",
    "execute_radarr_manual_import_command",
    "manual_import_commit",
]
