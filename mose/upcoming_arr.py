"""Direct HTTP snapshots of Radarr/Sonarr libraries and root-folder resolution."""

from __future__ import annotations

import json
import re
from typing import Any

import aiohttp

from mose.observe import get_logger, log_event
from mose.upcoming_store import LibraryMovie, LibrarySeries

logger = get_logger("upcoming_arr")


def normalize_base_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if u.endswith("/api/v3"):
        u = u[: -len("/api/v3")]
    return u.rstrip("/")


def compact_movie_row(movie: dict[str, Any]) -> dict[str, Any]:
    return {
        "tmdbId": movie.get("tmdbId"),
        "id": movie.get("id"),
        "title": movie.get("title"),
        "year": movie.get("year"),
        "hasFile": movie.get("hasFile"),
        "path": movie.get("path"),
    }


def compact_series_row(series: dict[str, Any]) -> dict[str, Any]:
    return {
        "tvdbId": series.get("tvdbId"),
        "id": series.get("id"),
        "title": series.get("title"),
        "year": series.get("year"),
        "rootFolderPath": series.get("rootFolderPath"),
        "seriesType": series.get("seriesType"),
        "path": series.get("path"),
    }


def paginate_index(
    items: list[dict[str, Any]],
    *,
    page: int = 1,
    page_size: int = 400,
) -> dict[str, Any]:
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 2000))
    total = len(items)
    start = (page - 1) * page_size
    slice_ = items[start : start + page_size]
    return {
        "page": page,
        "pageSize": page_size,
        "total": total,
        "totalPages": (total + page_size - 1) // page_size if page_size else 1,
        "items": slice_,
    }


def movie_from_radarr(row: dict[str, Any]) -> LibraryMovie | None:
    tmdb = row.get("tmdbId")
    try:
        tmdb_id = int(tmdb)
    except (TypeError, ValueError):
        return None
    if tmdb_id <= 0:
        return None
    year = row.get("year")
    try:
        year_i = int(year) if year is not None else None
    except (TypeError, ValueError):
        year_i = None
    rid = row.get("id")
    try:
        radarr_id = int(rid) if rid is not None else None
    except (TypeError, ValueError):
        radarr_id = None
    return LibraryMovie(
        tmdb_id=tmdb_id,
        radarr_id=radarr_id,
        title=str(row.get("title") or ""),
        year=year_i,
        has_file=bool(row.get("hasFile")),
        path=row.get("path"),
    )


def series_from_sonarr(row: dict[str, Any]) -> LibrarySeries | None:
    tvdb = row.get("tvdbId")
    try:
        tvdb_id = int(tvdb)
    except (TypeError, ValueError):
        return None
    if tvdb_id <= 0:
        return None
    year = row.get("year")
    try:
        year_i = int(year) if year is not None else None
    except (TypeError, ValueError):
        year_i = None
    sid = row.get("id")
    try:
        sonarr_id = int(sid) if sid is not None else None
    except (TypeError, ValueError):
        sonarr_id = None
    return LibrarySeries(
        tvdb_id=tvdb_id,
        sonarr_id=sonarr_id,
        title=str(row.get("title") or ""),
        year=year_i,
        root_folder_path=row.get("rootFolderPath"),
        series_type=row.get("seriesType"),
        path=row.get("path"),
    )


def pick_root_folder(
    folders: list[dict[str, Any]],
    *,
    match: str = "",
    explicit: str = "",
) -> str:
    if (explicit or "").strip():
        return explicit.strip()
    paths = []
    for f in folders:
        if not isinstance(f, dict):
            continue
        p = str(f.get("path") or "").rstrip("/")
        if p:
            paths.append(p)
    needle = (match or "").strip()
    if needle:
        hits = [p for p in paths if needle.lower() in p.lower()]
        if hits:
            hits.sort(key=len, reverse=True)
            return hits[0]
    return paths[0] if paths else ""


def pick_quality_profile(profiles: list[dict[str, Any]], *, explicit_id: int = 0) -> int | None:
    if explicit_id > 0:
        return explicit_id
    ids: list[int] = []
    for p in profiles:
        if not isinstance(p, dict):
            continue
        try:
            ids.append(int(p["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return ids[0] if ids else None


MOVIE_GENRE_FAMILY_ID = 10751
MOVIE_GENRE_HORROR_ID = 27


def build_movie_routing(
    folders: list[dict[str, Any]],
    *,
    base_path: str = "",
    kids_match: str = "Kids",
    horror_match: str = "Scary",
) -> dict[str, Any]:
    """Pre-resolve Radarr root folders into a bucket map for movie adds.

    Buckets: ``kids`` (Kids Movies), ``horror`` (Scary Movies), ``base``
    (the main Movies root, catch-all), ``years`` (exact-year folders that
    Radarr has registered, keyed by int year), ``decades`` (60s-70s/80s/90s/
    00s, keyed by the decade start year, e.g. 1990 -> /Movies/90s). ``osr``
    and any other folder are never routed automatically.
    """
    paths: list[str] = []
    for f in folders:
        if not isinstance(f, dict):
            continue
        p = str(f.get("path") or "").rstrip("/")
        if p:
            paths.append(p)

    def hit(needle: str) -> str:
        needle = (needle or "").strip()
        if not needle:
            return ""
        hits = [p for p in paths if needle.lower() in p.lower()]
        if not hits:
            return ""
        hits.sort(key=len, reverse=True)
        return hits[0]

    kids = hit(kids_match)
    horror = hit(horror_match)
    decade_re = re.compile(r"(?:^|/)(\d{2})s(?:-70s)?$", re.IGNORECASE)
    year_re = re.compile(r"(?:^|/)(\d{4})$")
    decades: dict[int, str] = {}
    years: dict[int, str] = {}
    for p in paths:
        if kids and p == kids:
            continue
        if horror and p == horror:
            continue
        m = decade_re.search(p)
        if m:
            yy = int(m.group(1))
            decade = 1900 + yy if yy >= 60 else 2000 + yy
            if 1960 <= decade <= 2000:
                decades.setdefault(decade, p)
                if m.group(0).lower().endswith("-70s"):
                    # "60s-70s" spans two decades; register both keys.
                    decades.setdefault(decade + 10, p)
            continue
        m = year_re.search(p)
        if m:
            yyyy = int(m.group(1))
            if 1900 <= yyyy <= 2199:
                years.setdefault(yyyy, p)
    if (base_path or "").strip():
        base = base_path.strip().rstrip("/")
    else:
        candidates = [
            p
            for p in paths
            if p not in (kids, horror)
            and not decade_re.search(p)
            and not year_re.search(p)
            and not p.lower().endswith("/osr")
        ]
        candidates.sort(key=len)
        base = candidates[0] if candidates else ""
    return {"kids": kids, "horror": horror, "base": base, "years": years, "decades": decades}


def route_movie_root(routing: dict[str, Any], *, genres: list[Any], year: int | None) -> str:
    """Pick the Radarr root folder for one movie.

    Precedence: horror (Horror wins Kids/Family ties) -> kids (TMDB Family
    genre 10751) -> exact year folder (whatever Radarr registered) -> decade
    folder (60s-70s, 80s, 90s, 00s) -> base Movies root. ``osr`` is never
    routed automatically.
    """
    ids = {int(g) for g in (genres or []) if isinstance(g, (int, float)) or (isinstance(g, str) and g.isdigit())}
    if MOVIE_GENRE_HORROR_ID in ids and routing.get("horror"):
        return routing["horror"]
    if MOVIE_GENRE_FAMILY_ID in ids and routing.get("kids"):
        return routing["kids"]
    if year:
        y = int(year)
        years = routing.get("years") or {}
        decades = routing.get("decades") or {}
        # Keys arrive as ints in-memory but as strings after a JSON roundtrip
        # through the arr_config table.
        hit = years.get(y) or years.get(str(y))
        if hit:
            return hit
        decade = (y // 10) * 10
        return decades.get(decade) or decades.get(str(decade)) or routing.get("base") or ""
    return routing.get("base") or ""



def resolve_arr_targets(
    *,
    radarr_folders: list[dict[str, Any]],
    radarr_profiles: list[dict[str, Any]],
    sonarr_folders: list[dict[str, Any]],
    sonarr_profiles: list[dict[str, Any]],
    radarr_root_path: str = "",
    sonarr_tv_root_path: str = "",
    sonarr_anime_root_path: str = "",
    sonarr_tv_root_match: str = "/TV",
    sonarr_anime_root_match: str = "/Anime",
    radarr_kids_root_match: str = "Kids",
    radarr_horror_root_match: str = "Scary",
    radarr_quality_profile_id: int = 0,
    sonarr_tv_quality_profile_id: int = 0,
    sonarr_anime_quality_profile_id: int = 0,
) -> dict[str, Any]:
    tv_root = pick_root_folder(
        sonarr_folders, match=sonarr_tv_root_match, explicit=sonarr_tv_root_path
    )
    anime_root = pick_root_folder(
        sonarr_folders, match=sonarr_anime_root_match, explicit=sonarr_anime_root_path
    )
    if tv_root and anime_root and tv_root == anime_root and sonarr_tv_root_match != sonarr_anime_root_match:
        # Prefer the more specific (longer) match when both hit the same folder.
        tv_alt = pick_root_folder(sonarr_folders, match=sonarr_tv_root_match, explicit="")
        anime_alt = pick_root_folder(sonarr_folders, match=sonarr_anime_root_match, explicit="")
        if tv_alt != anime_alt:
            tv_root, anime_root = tv_alt, anime_alt
    movie_routing = build_movie_routing(
        radarr_folders,
        base_path=radarr_root_path,
        kids_match=radarr_kids_root_match,
        horror_match=radarr_horror_root_match,
    )
    return {
        "radarr_root_path": movie_routing.get("base") or "",
        "radarr_movie_routing": movie_routing,
        "sonarr_tv_root_path": tv_root,
        "sonarr_anime_root_path": anime_root,
        "radarr_quality_profile_id": pick_quality_profile(
            radarr_profiles, explicit_id=radarr_quality_profile_id
        ),
        "sonarr_tv_quality_profile_id": pick_quality_profile(
            sonarr_profiles, explicit_id=sonarr_tv_quality_profile_id
        ),
        "sonarr_anime_quality_profile_id": pick_quality_profile(
            sonarr_profiles, explicit_id=sonarr_anime_quality_profile_id
        ),
    }


class ArrHttpClient:
    """Minimal async *arr API v3 client (agent-side snapshots and adds)."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 120.0) -> None:
        self.base = normalize_base_url(base_url)
        self.api_key = (api_key or "").strip()
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> ArrHttpClient:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout),
            headers={"X-Api-Key": self.api_key},
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _url(self, path: str) -> str:
        p = path if path.startswith("/") else f"/{path}"
        return f"{self.base}/api/v3{p}"

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.base or not self.api_key:
            raise RuntimeError("Arr HTTP client missing URL or API key")
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                headers={"X-Api-Key": self.api_key},
            )
        async with self._session.get(self._url(path), params=params) as resp:
            resp.raise_for_status()
            text = await resp.text()
            if not text:
                return None
            return json.loads(text)

    async def post_json(self, path: str, body: Any) -> Any:
        if not self.base or not self.api_key:
            raise RuntimeError("Arr HTTP client missing URL or API key")
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                headers={"X-Api-Key": self.api_key},
            )
        async with self._session.post(self._url(path), json=body) as resp:
            payload = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status} {path}: {payload[:2000]}")
            if not payload:
                return None
            return json.loads(payload)


async def snapshot_radarr(client: ArrHttpClient) -> tuple[list[LibraryMovie], list[dict[str, Any]], list[dict[str, Any]]]:
    movies_raw = await client.get_json("/movie")
    folders = await client.get_json("/rootfolder")
    profiles = await client.get_json("/qualityprofile")
    movies: list[LibraryMovie] = []
    if isinstance(movies_raw, list):
        for row in movies_raw:
            if isinstance(row, dict):
                parsed = movie_from_radarr(row)
                if parsed is not None:
                    movies.append(parsed)
    log_event(logger, "radarr_snapshot", movies=len(movies))
    return movies, folders if isinstance(folders, list) else [], profiles if isinstance(profiles, list) else []


async def snapshot_sonarr(client: ArrHttpClient) -> tuple[list[LibrarySeries], list[dict[str, Any]], list[dict[str, Any]]]:
    series_raw = await client.get_json("/series")
    folders = await client.get_json("/rootfolder")
    profiles = await client.get_json("/qualityprofile")
    series: list[LibrarySeries] = []
    if isinstance(series_raw, list):
        for row in series_raw:
            if isinstance(row, dict):
                parsed = series_from_sonarr(row)
                if parsed is not None:
                    series.append(parsed)
    log_event(logger, "sonarr_snapshot", series=len(series))
    return series, folders if isinstance(folders, list) else [], profiles if isinstance(profiles, list) else []


def add_movie_payload(
    *,
    title: str,
    tmdb_id: int,
    quality_profile_id: int,
    root_folder_path: str,
    year: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": title,
        "tmdbId": int(tmdb_id),
        "qualityProfileId": int(quality_profile_id),
        "rootFolderPath": root_folder_path,
        "monitored": True,
        "addOptions": {"searchForMovie": True},
    }
    if year:
        body["year"] = int(year)
    return body


def add_series_payload(
    lookup: dict[str, Any],
    *,
    quality_profile_id: int,
    root_folder_path: str,
    series_type: str = "standard",
) -> dict[str, Any]:
    body = dict(lookup)
    body["qualityProfileId"] = int(quality_profile_id)
    body["rootFolderPath"] = root_folder_path
    body["monitored"] = True
    body["seriesType"] = series_type
    body["addOptions"] = {"searchForMissingEpisodes": True, "monitor": "all"}
    return body
