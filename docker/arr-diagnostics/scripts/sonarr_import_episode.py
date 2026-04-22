#!/usr/bin/env python3
"""Resolve series + episode, find queue downloadId, POST /api/v3/queue/import.

Run inside the sonarr-diagnostics container (same env as MCP):

  docker compose exec -T sonarr-diagnostics \\
    python /opt/arr-diagnostics/scripts/sonarr_import_episode.py \\
    --series \"IMPACT x Nightline\" --season 4 --episode 26

Use ``-T`` (no TTY) so stdin/heredocs work; avoids:
``cannot attach stdin to a TTY-enabled container``.
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
        description="Find Sonarr queue row for S+E and POST /queue/import (uses SONARR_URL + SONARR_API_KEY).",
    )
    p.add_argument("--series", required=True, help="Substring to match series title (case-insensitive)")
    p.add_argument("--season", type=int, required=True)
    p.add_argument("--episode", type=int, required=True)
    p.add_argument("--import-mode", default="copy", choices=["copy", "move", "hardlink"])
    p.add_argument("--should-grab", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Print ids and payload only; do not POST")
    args = p.parse_args()

    url_raw = os.environ.get("SONARR_URL", "").strip()
    key_raw = os.environ.get("SONARR_API_KEY", "").strip()
    url_raw = _strip_bom(url_raw)
    key_raw = _strip_bom(key_raw)
    if not url_raw or not key_raw:
        print("SONARR_URL and SONARR_API_KEY must be set in the container.", file=sys.stderr)
        sys.exit(1)

    import httpx

    base = _normalize_base(url_raw)
    api = f"{base}/api/v3"
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

        # --- downloadId: prefer queue/details filtered by episode ---
        download_id: str | None = None
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
            download_id = _extract_download_id(detail, series_id, episode_id)

        if not download_id:
            # Paginate /queue as fallback
            page = 1
            page_size = 200
            while True:
                r = http.get(
                    f"{api}/queue",
                    params={"page": page, "pageSize": page_size},
                )
                r.raise_for_status()
                qdata = r.json()
                records = qdata.get("records") or []
                download_id = _scan_queue_records(records, series_id, episode_id)
                if download_id:
                    break
                total = qdata.get("totalRecords") or len(records)
                if page * page_size >= total or not records:
                    break
                page += 1

        if not download_id:
            print(
                "Could not find a queue row with downloadId for this episode.",
                file=sys.stderr,
            )
            print(
                "Check Activity → Queue; item may already be imported or removed.",
                file=sys.stderr,
            )
            sys.exit(5)

        print(f"downloadId={download_id}")

        payload: dict[str, Any] = {
            "downloadId": download_id,
            "seriesId": series_id,
            "episodeIds": [episode_id],
            "options": {
                "importMode": args.import_mode,
                "shouldGrab": args.should_grab,
            },
        }
        print("payload:", json.dumps(payload, indent=2))

        if args.dry_run:
            return

        r = http.post(f"{api}/queue/import", json=payload)
        print("HTTP", r.status_code)
        body = r.text[:12000]
        print(body)
        if not r.is_success:
            sys.exit(6)
    finally:
        http.close()


def _extract_download_id(data: Any, series_id: int, episode_id: int) -> str | None:
    """queue/details returns a list or nested structure — find first matching queue row."""
    if isinstance(data, list):
        for item in data:
            found = _extract_download_id(item, series_id, episode_id)
            if found:
                return found
        return None
    if isinstance(data, dict):
        did = data.get("downloadId")
        if did and _episode_matches_structure(data, series_id, episode_id):
            return str(did)
        for v in data.values():
            found = _extract_download_id(v, series_id, episode_id)
            if found:
                return found
    return None


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


def _scan_queue_records(records: list[Any], series_id: int, episode_id: int) -> str | None:
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
            did = rec.get("downloadId")
            return str(did) if did else None
        if ep_obj.get("id") == episode_id:
            did = rec.get("downloadId")
            return str(did) if did else None
    return None


if __name__ == "__main__":
    main()
