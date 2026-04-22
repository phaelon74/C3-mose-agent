#!/usr/bin/env python3
"""Resolve series + episode, find queue downloadId, commit via POST /manualimport.

Sonarr v3 commits manual imports with **GET /manualimport** then **POST /manualimport**
(array of ManualImportReprocessResource). Stock Sonarr returns **405** for
``POST /queue/import`` — that route is not part of upstream OpenAPI.

Run inside the sonarr-diagnostics container:

  docker compose exec -T sonarr-diagnostics \\
    python /opt/arr-diagnostics/scripts/sonarr_import_episode.py \\
    --series \"IMPACT x Nightline\" --season 4 --episode 26

Use ``-T`` (no TTY) when piping/heredocs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any


def _normalize_base(url: str) -> str:
    u = url.strip().rstrip("/")
    if u.endswith("/api/v3"):
        u = u[: -len("/api/v3")]
    return u.rstrip("/")


def _strip_bom(s: str) -> str:
    if s.startswith("\ufeff"):
        return s[1:]
    return s


def main() -> None:
    p = argparse.ArgumentParser(
        description="Resolve S+E, find queue downloadId, POST Sonarr /manualimport (official v3 API).",
    )
    p.add_argument("--series", required=True, help="Substring to match series title (case-insensitive)")
    p.add_argument("--season", type=int, required=True)
    p.add_argument("--episode", type=int, required=True)
    p.add_argument("--dry-run", action="store_true", help="Print GET + POST payload only; do not commit")
    p.add_argument(
        "--debug-rows",
        action="store_true",
        help=(
            "Dump raw /manualimport rows as JSON (downloadId-only, then +seasonNumber variant) "
            "and exit before POST. Use this to diagnose 'no_matching_manualimport_row'."
        ),
    )
    p.add_argument(
        "--dump-queue-rec",
        action="store_true",
        help="Also print the raw queue record that resolved downloadId.",
    )
    p.add_argument(
        "--debug-row-limit",
        type=int,
        default=5,
        help="Max rows printed by --debug-rows per GET (default: 5).",
    )
    args = p.parse_args()

    url_raw = os.environ.get("SONARR_URL", "").strip()
    key_raw = os.environ.get("SONARR_API_KEY", "").strip()
    url_raw = _strip_bom(url_raw)
    key_raw = _strip_bom(key_raw)
    if not url_raw or not key_raw:
        print("SONARR_URL and SONARR_API_KEY must be set in the container.", file=sys.stderr)
        sys.exit(1)

    # Import after env check so --help works without PYTHONPATH package
    from arr_diagnostics.client import ArrClient
    from arr_diagnostics.sonarr_manual_import import (
        post_manual_import_reprocess,
        prepare_manual_import_payload,
    )

    client = ArrClient(url_raw, key_raw)

    try:
        base = _normalize_base(url_raw)
        api = f"{base}/api/v3"
        import httpx

        http = httpx.Client(timeout=120.0, headers={"X-Api-Key": key_raw})

        try:
            # --- series ---
            r = http.get(f"{api}/series")
            r.raise_for_status()
            series_list = r.json()
            needle = args.series.lower()
            matches = [s for s in series_list if needle in (s.get("title") or "").lower()]
            if not matches:
                print(f"No series matching substring {args.series!r}", file=sys.stderr)
                sys.exit(2)
            if len(matches) > 1:
                print("Multiple series matches — refine --series:", file=sys.stderr)
                for s in matches:
                    print(f"  id={s['id']}  {s.get('title')!r}", file=sys.stderr)
                sys.exit(3)
            series = matches[0]
            series_id = int(series["id"])
            print(f"seriesId={series_id}  title={series.get('title')!r}")

            # --- episode id ---
            r = http.get(
                f"{api}/episode",
                params={"seriesId": series_id, "seasonNumber": args.season},
            )
            r.raise_for_status()
            eps = r.json()
            ep = None
            for e in eps:
                if e.get("seasonNumber") == args.season and e.get("episodeNumber") == args.episode:
                    ep = e
                    break
            if not ep:
                print(
                    f"No episode S{args.season:02d}E{args.episode:02d} for this series.",
                    file=sys.stderr,
                )
                sys.exit(4)
            episode_id = int(ep["id"])
            print(f"episodeId={episode_id}  title={ep.get('title')!r}")

            queue_rec = _resolve_queue_record(http, api, series_id, episode_id)

            if not queue_rec:
                print(
                    "Could not find a queue row with downloadId for this episode.",
                    file=sys.stderr,
                )
                print(
                    "Check Activity → Queue; item may already be imported or removed.",
                    file=sys.stderr,
                )
                sys.exit(5)

            download_id = queue_rec.get("downloadId")
            if not download_id:
                print("Queue row matched but downloadId is missing.", file=sys.stderr)
                sys.exit(5)

            path_hints = _queue_path_hints(queue_rec, args.season, args.episode)

            print(f"downloadId={download_id}")
            if path_hints:
                print(f"path_hints={path_hints!r}")

            if args.dump_queue_rec:
                print("=== queue record (raw) ===")
                print(json.dumps(queue_rec, indent=2, sort_keys=True))

            if args.debug_rows:
                _debug_dump_manualimport(
                    http,
                    api,
                    series_id=series_id,
                    download_id=str(download_id),
                    season=args.season,
                    episode=args.episode,
                    limit=max(1, int(args.debug_row_limit)),
                )
                return

            prep = prepare_manual_import_payload(
                client,
                str(download_id),
                series_id,
                episode_id,
                season_number=args.season,
                episode_number=args.episode,
                path_hints=path_hints,
            )
            if isinstance(prep, str):
                print(prep, file=sys.stderr)
                sys.exit(6)
            rows, reprocess = prep
            print(f"manualImport GET: {len(rows)} candidate row(s)")
            print("POST /manualimport body:", json.dumps([reprocess], indent=2))

            if args.dry_run:
                return

            out = post_manual_import_reprocess(client, reprocess)
            print(out)
            try:
                err_probe = json.loads(out)
            except json.JSONDecodeError:
                err_probe = {}
            if isinstance(err_probe, dict) and err_probe.get("error") == "http_error":
                sys.exit(7)
        finally:
            http.close()
    finally:
        client.close()


def _debug_dump_manualimport(
    http: Any,
    api: str,
    *,
    series_id: int,
    download_id: str,
    season: int,
    episode: int,
    limit: int,
) -> None:
    """Dump raw GET /manualimport responses (several filter variants) for diagnosis."""
    variants: list[tuple[str, dict[str, Any]]] = [
        (
            "downloadId+seriesId",
            {"downloadId": download_id, "seriesId": series_id},
        ),
        (
            "downloadId+seriesId+seasonNumber",
            {"downloadId": download_id, "seriesId": series_id, "seasonNumber": season},
        ),
        (
            "downloadId only",
            {"downloadId": download_id},
        ),
    ]
    print("=== GET /manualimport variants ===")
    for label, params in variants:
        try:
            r = http.get(f"{api}/manualimport", params=params)
            print(f"\n--- {label} -> HTTP {r.status_code}  params={params} ---")
            if not r.is_success:
                print(r.text[:2000])
                continue
            data = r.json()
            if not isinstance(data, list):
                data = [data] if data else []
            print(f"row_count={len(data)}")
            _summarize_rows(data, series_id=series_id, season=season, episode=episode)
            print("--- sample rows (full JSON) ---")
            for row in data[:limit]:
                print(json.dumps(row, indent=2, sort_keys=True))
                print("---")
        except Exception as e:  # noqa: BLE001
            print(f"[error] variant {label!r}: {e!r}")
    print("\nHint: grep sample rows for the expected release name. If it's absent, "
          "the real episode is not being returned by /manualimport for this downloadId.")


def _summarize_rows(
    rows: list[Any],
    *,
    series_id: int,
    season: int,
    episode: int,
) -> None:
    """Compact per-row summary: keys present, seriesId, season/episode hints, short path."""
    matching_series = 0
    matching_season = 0
    matching_episode = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("seriesId")
        if sid is None and isinstance(row.get("series"), dict):
            sid = row["series"].get("id")
        if sid is not None and str(sid) == str(series_id):
            matching_series += 1
        rs = row.get("seasonNumber")
        if rs is not None and str(rs) == str(season):
            matching_season += 1
        eps = row.get("episodes") or []
        for er in eps:
            if not isinstance(er, dict):
                continue
            if str(er.get("seasonNumber")) == str(season) and str(er.get("episodeNumber")) == str(episode):
                matching_episode += 1
                break
    print(
        f"summary: rows_with_seriesId_match={matching_series}  "
        f"row_season_match={matching_season}  "
        f"nested_episode_SxxEyy_match={matching_episode}",
    )
    for i, row in enumerate(rows[:25]):
        if not isinstance(row, dict):
            continue
        keys = sorted(row.keys())
        sid = row.get("seriesId")
        if sid is None and isinstance(row.get("series"), dict):
            sid = row["series"].get("id")
        path = row.get("path") or row.get("relativePath") or row.get("name") or ""
        rs = row.get("seasonNumber")
        ep_info: list[str] = []
        for er in row.get("episodes") or []:
            if isinstance(er, dict):
                ep_info.append(f"{er.get('seasonNumber')}x{er.get('episodeNumber')}#{er.get('id')}")
        print(
            f"  row[{i}] seriesId={sid} season={rs} eps={ep_info}  path={path!r}  keys={keys}",
        )


def _resolve_queue_record(http: Any, api: str, series_id: int, episode_id: int) -> dict[str, Any] | None:
    r = http.get(
        f"{api}/queue/details",
        params={
            "seriesId": series_id,
            "episodeId": episode_id,
            "includeEpisode": True,
        },
    )
    if r.is_success:
        detail = r.json()
        rec = _find_matching_queue_record(detail, series_id, episode_id)
        if rec and rec.get("downloadId"):
            return rec

    page = 1
    page_size = 200
    while True:
        r = http.get(f"{api}/queue", params={"page": page, "pageSize": page_size})
        r.raise_for_status()
        qdata = r.json()
        records = qdata.get("records") or []
        found = _scan_queue_for_record(records, series_id, episode_id)
        if found and found.get("downloadId"):
            return found
        total = qdata.get("totalRecords") or len(records)
        if page * page_size >= total or not records:
            break
        page += 1
    return None


def _find_matching_queue_record(data: Any, series_id: int, episode_id: int) -> dict[str, Any] | None:
    if isinstance(data, list):
        for item in data:
            found = _find_matching_queue_record(item, series_id, episode_id)
            if found:
                return found
        return None
    if isinstance(data, dict):
        did = data.get("downloadId")
        if did and _episode_matches_structure(data, series_id, episode_id):
            return data
        for v in data.values():
            found = _find_matching_queue_record(v, series_id, episode_id)
            if found:
                return found
    return None


def _queue_path_hints(rec: dict[str, Any], season: int, episode: int) -> list[str]:
    """Strings from the Activity queue row + ``SxxEyy`` tokens for manualimport row matching."""
    out: list[str] = []
    for key in ("outputPath", "title", "sourceTitle", "name", "releaseTitle"):
        v = rec.get(key)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    ep = rec.get("episode")
    if isinstance(ep, dict):
        t = ep.get("title")
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
    ss, ee = season, episode
    for t in (
        f"S{ss:02d}E{ee:02d}",
        f"s{ss:02d}e{ee:02d}",
        f".S{ss:02d}E{ee:02d}.",
    ):
        out.append(t)
    seen: set[str] = set()
    uniq: list[str] = []
    for h in out:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    return uniq


def _episode_matches_structure(rec: dict[str, Any], series_id: int, episode_id: int) -> bool:
    ep_ids = rec.get("episodeIds") or []
    ep_obj = rec.get("episode") or {}
    sid = rec.get("seriesId")
    if sid is None and isinstance(rec.get("series"), dict):
        sid = rec["series"].get("id")
    if sid is not None and int(sid) != series_id:
        return False
    if episode_id in [int(x) for x in ep_ids]:
        return True
    if ep_obj.get("id") == episode_id:
        return True
    return False


def _scan_queue_for_record(records: list[Any], series_id: int, episode_id: int) -> dict[str, Any] | None:
    for rec in records:
        if not isinstance(rec, dict):
            continue
        sid = rec.get("seriesId")
        if sid is None and isinstance(rec.get("series"), dict):
            sid = rec["series"].get("id")
        if sid is None or int(sid) != series_id:
            continue
        ep_ids = rec.get("episodeIds") or []
        ep_obj = rec.get("episode") or {}
        if episode_id in [int(x) for x in ep_ids]:
            return rec
        if ep_obj.get("id") == episode_id:
            return rec
    return None


if __name__ == "__main__":
    main()
