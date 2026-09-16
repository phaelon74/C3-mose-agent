"""TMDB client and parsers for the upcoming-media catalog."""

from __future__ import annotations

import asyncio
import time
from datetime import date
from typing import Any

import aiohttp

from mose.observe import get_logger, log_event

logger = get_logger("upcoming_tmdb")

TMDB_API = "https://api.themoviedb.org/3"
ANIMATION_GENRE_ID = 16
# TMDB movie release types: 1 Premiere, 2 Theatrical limited, 3 Theatrical, 4 Digital, 5 Physical, 6 TV
THEATRICAL_TYPES = frozenset({2, 3})
DIGITAL_TYPES = frozenset({4})
PHYSICAL_TYPES = frozenset({5})


def catalog_years(*, today: date | None = None) -> tuple[int, int]:
    d = today or date.today()
    return d.year, d.year + 1


def is_anime(
    *,
    genre_ids: list[int],
    origin_country: list[str] | str | None,
    original_language: str | None,
    genre_names: list[str] | None = None,
) -> bool:
    ids = {int(g) for g in genre_ids if g is not None}
    names = {str(n).lower() for n in (genre_names or [])}
    animated = ANIMATION_GENRE_ID in ids or "animation" in names
    countries: list[str] = []
    if isinstance(origin_country, str):
        countries = [c.strip() for c in origin_country.split(",") if c.strip()]
    elif origin_country:
        countries = [str(c) for c in origin_country]
    jp = "JP" in countries or (original_language or "").lower() == "ja"
    return animated and jp


def _year_from_date(value: str | None) -> int | None:
    if not value or len(value) < 4:
        return None
    try:
        return int(value[:4])
    except ValueError:
        return None


def tvdb_id_from_external_ids(payload: Any) -> int | None:
    """Parse TMDB ``external_ids.tvdb_id`` (or a bare ``tvdb_id`` dict)."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get("tvdb_id")
    if raw is None:
        raw = payload.get("tvdbId")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def pick_tvdb_search_id(
    results: Any,
    *,
    title: str,
    year: int | None = None,
) -> int | None:
    """Pick a TheTVDB series id from a v4 search payload (or a list of rows)."""
    rows: list[dict[str, Any]] = []
    if isinstance(results, dict):
        data = results.get("data")
        if isinstance(data, list):
            rows = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            rows = [data]
    elif isinstance(results, list):
        rows = [r for r in results if isinstance(r, dict)]
    want = (title or "").strip().lower()
    parsed: list[tuple[int, str, int | None]] = []
    for r in rows:
        raw = r.get("tvdb_id")
        if raw is None:
            raw = r.get("id")
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        name = str(r.get("name") or r.get("name_translated") or "").strip().lower()
        y: int | None = None
        year_raw = r.get("year")
        if year_raw is not None:
            try:
                y = int(str(year_raw)[:4])
            except ValueError:
                y = None
        parsed.append((n, name, y))
    if not parsed:
        return None
    if year is not None:
        year_hits = [(n, name, y) for n, name, y in parsed if y == year]
        for n, name, _y in year_hits:
            if want and (name == want or want in name):
                return n
        if year_hits:
            return year_hits[0][0]
    for n, name, _y in parsed:
        if name == want:
            return n
    return parsed[0][0]


def extract_us_release_dates(release_dates_payload: Any) -> dict[str, str | None]:
    """Pick earliest US theatrical / digital / physical dates from TMDB release_dates."""
    results = []
    if isinstance(release_dates_payload, dict):
        results = release_dates_payload.get("results") or []
    elif isinstance(release_dates_payload, list):
        results = release_dates_payload
    us_entries: list[dict[str, Any]] = []
    for block in results:
        if not isinstance(block, dict):
            continue
        if str(block.get("iso_3166_1") or "").upper() != "US":
            continue
        for rel in block.get("release_dates") or []:
            if isinstance(rel, dict):
                us_entries.append(rel)
    theatrical: list[str] = []
    digital: list[str] = []
    physical: list[str] = []
    for rel in us_entries:
        raw = str(rel.get("release_date") or "")[:10]
        if len(raw) < 10:
            continue
        rtype = rel.get("type")
        try:
            rtype_i = int(rtype)
        except (TypeError, ValueError):
            continue
        if rtype_i in THEATRICAL_TYPES:
            theatrical.append(raw)
        elif rtype_i in DIGITAL_TYPES:
            digital.append(raw)
        elif rtype_i in PHYSICAL_TYPES:
            physical.append(raw)
    return {
        "us_theatrical": min(theatrical) if theatrical else None,
        "us_digital": min(digital) if digital else None,
        "us_physical": min(physical) if physical else None,
    }


def compute_next_season(
    seasons: list[dict[str, Any]],
    *,
    today: date,
    catalog_start: date,
    catalog_end: date,
) -> tuple[int | None, str | None]:
    """Earliest unaired in-window season, else most recent in-window season (skip specials)."""
    parsed: list[tuple[int, date, str]] = []
    for s in seasons:
        num = s.get("season_number")
        try:
            n = int(num)
        except (TypeError, ValueError):
            continue
        if n < 1:
            continue
        raw = str(s.get("air_date") or "")[:10]
        if len(raw) < 10:
            continue
        try:
            ad = date.fromisoformat(raw)
        except ValueError:
            continue
        if ad < catalog_start or ad > catalog_end:
            continue
        parsed.append((n, ad, raw))
    if not parsed:
        return None, None
    future = [(n, ad, raw) for n, ad, raw in parsed if ad >= today]
    if future:
        future.sort(key=lambda t: t[1])
        return future[0][0], future[0][2]
    parsed.sort(key=lambda t: t[1], reverse=True)
    return parsed[0][0], parsed[0][2]


def title_from_discover_movie(item: dict[str, Any]) -> dict[str, Any]:
    release = str(item.get("release_date") or "")[:10] or None
    return {
        "media_type": "movie",
        "tmdb_id": int(item["id"]),
        "kind": "movie",
        "title": item.get("title") or item.get("original_title") or f"TMDB {item.get('id')}",
        "original_title": item.get("original_title"),
        "overview": item.get("overview"),
        "popularity": float(item.get("popularity") or 0),
        "vote_count": int(item.get("vote_count") or 0),
        "vote_average": float(item.get("vote_average") or 0),
        "original_language": item.get("original_language"),
        "adult": bool(item.get("adult")),
        "release_date": release,
        "year": _year_from_date(release),
        "genres": list(item.get("genre_ids") or []),
        "origin_country": ",".join(item.get("origin_country") or []),
    }


def title_from_discover_tv(item: dict[str, Any]) -> dict[str, Any]:
    first = str(item.get("first_air_date") or "")[:10] or None
    genre_ids = [int(g) for g in (item.get("genre_ids") or []) if g is not None]
    origin = list(item.get("origin_country") or [])
    lang = item.get("original_language")
    kind = "anime" if is_anime(genre_ids=genre_ids, origin_country=origin, original_language=lang) else "tv"
    return {
        "media_type": "tv",
        "tmdb_id": int(item["id"]),
        "kind": kind,
        "title": item.get("name") or item.get("original_name") or f"TMDB {item.get('id')}",
        "original_title": item.get("original_name"),
        "overview": item.get("overview"),
        "popularity": float(item.get("popularity") or 0),
        "vote_count": int(item.get("vote_count") or 0),
        "vote_average": float(item.get("vote_average") or 0),
        "original_language": lang,
        "adult": bool(item.get("adult")),
        "first_air_date": first,
        "year": _year_from_date(first),
        "genres": genre_ids,
        "origin_country": ",".join(origin),
    }


def apply_movie_details(base: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    ext = details.get("external_ids") or {}
    if isinstance(ext, dict):
        if ext.get("imdb_id"):
            out["imdb_id"] = ext.get("imdb_id")
        tid = tvdb_id_from_external_ids(ext)
        if tid:
            out["tvdb_id"] = tid
    dates = extract_us_release_dates(details.get("release_dates"))
    out.update(dates)
    out["status"] = details.get("status") or out.get("status")
    out["overview"] = details.get("overview") or out.get("overview")
    rel = str(details.get("release_date") or out.get("release_date") or "")[:10] or None
    out["release_date"] = rel
    out["year"] = _year_from_date(rel) or out.get("year")
    if details.get("popularity") is not None:
        out["popularity"] = float(details["popularity"])
    if details.get("vote_count") is not None:
        out["vote_count"] = int(details["vote_count"])
    genres = details.get("genres") or []
    if genres and isinstance(genres, list) and isinstance(genres[0], dict):
        out["genres"] = [g.get("id") for g in genres if g.get("id") is not None]
    out["tmdb_updated_at"] = tmdb_updated_at_from_details(details) or out.get("tmdb_updated_at")
    return out


def tmdb_updated_at_from_details(details: dict[str, Any]) -> str | None:
    for key in ("updated_at", "last_updated"):
        raw = details.get(key)
        if raw:
            return str(raw)[:64]
    return None


def apply_tv_details(
    base: dict[str, Any],
    details: dict[str, Any],
    *,
    today: date,
    catalog_start: date,
    catalog_end: date,
) -> dict[str, Any]:
    out = dict(base)
    ext = details.get("external_ids") or {}
    if isinstance(ext, dict):
        if ext.get("imdb_id"):
            out["imdb_id"] = ext.get("imdb_id")
        tid = tvdb_id_from_external_ids(ext)
        if tid:
            out["tvdb_id"] = tid
    raw_origin = details.get("origin_country") or out.get("origin_country") or []
    if isinstance(raw_origin, str):
        origin = [c.strip() for c in raw_origin.split(",") if c.strip()]
    else:
        origin = [str(c) for c in raw_origin]
    out["origin_country"] = ",".join(origin)
    genres_raw = details.get("genres") or []
    genre_ids = list(out.get("genres") or [])
    genre_names: list[str] = []
    if genres_raw and isinstance(genres_raw, list) and genres_raw and isinstance(genres_raw[0], dict):
        genre_ids = [int(g["id"]) for g in genres_raw if g.get("id") is not None]
        genre_names = [str(g.get("name") or "") for g in genres_raw]
        out["genres"] = genre_ids
    lang = details.get("original_language") or out.get("original_language")
    out["original_language"] = lang
    out["kind"] = (
        "anime"
        if is_anime(
            genre_ids=genre_ids,
            origin_country=origin,
            original_language=lang,
            genre_names=genre_names,
        )
        else "tv"
    )
    first = str(details.get("first_air_date") or out.get("first_air_date") or "")[:10] or None
    out["first_air_date"] = first
    out["year"] = _year_from_date(first) or out.get("year")
    out["status"] = details.get("status") or out.get("status")
    if details.get("popularity") is not None:
        out["popularity"] = float(details["popularity"])
    if details.get("vote_count") is not None:
        out["vote_count"] = int(details["vote_count"])
    seasons = details.get("seasons") or []
    next_n, next_d = compute_next_season(
        seasons if isinstance(seasons, list) else [],
        today=today,
        catalog_start=catalog_start,
        catalog_end=catalog_end,
    )
    out["next_season_number"] = next_n
    out["next_season_air_date"] = next_d
    out["seasons"] = [
        {
            "season_number": s.get("season_number"),
            "air_date": str(s.get("air_date") or "")[:10] or None,
            "episode_count": s.get("episode_count"),
            "name": s.get("name"),
        }
        for s in (seasons if isinstance(seasons, list) else [])
        if isinstance(s, dict) and s.get("season_number") is not None
    ]
    jp_dates = [
        str(s.get("air_date") or "")[:10]
        for s in (seasons if isinstance(seasons, list) else [])
        if isinstance(s, dict) and str(s.get("air_date") or "")[:10]
    ]
    if "JP" in origin and jp_dates:
        out["jp_air_date"] = min(jp_dates)
    out["tmdb_updated_at"] = tmdb_updated_at_from_details(details) or out.get("tmdb_updated_at")
    return out


class TmdbClient:
    """Rate-limited TMDB v3 client (aiohttp). ~40 requests / 10 seconds."""

    def __init__(
        self,
        api_key: str,
        *,
        session: aiohttp.ClientSession | None = None,
        min_interval: float = 0.28,
    ) -> None:
        self.api_key = api_key.strip()
        self._session = session
        self._own_session = session is None
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def __aenter__(self) -> TmdbClient:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
            self._own_session = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.api_key:
            raise RuntimeError("TMDB_API_KEY is not set")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
            self._own_session = True
        q = dict(params or {})
        q["api_key"] = self.api_key
        url = f"{TMDB_API}{path}"
        if not path.startswith("/"):
            url = f"{TMDB_API}/{path}"
        backoff = 1.0
        for attempt in range(6):
            async with self._lock:
                wait = self._min_interval - (time.monotonic() - self._last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
                assert self._session is not None
                async with self._session.get(url, params=q) as resp:
                    self._last_request = time.monotonic()
                    if resp.status == 429:
                        retry = float(resp.headers.get("Retry-After") or backoff)
                        log_event(logger, "tmdb_rate_limited", retry_after=retry, attempt=attempt)
                        await asyncio.sleep(retry)
                        backoff = min(30.0, backoff * 2)
                        continue
                    if resp.status >= 500:
                        await asyncio.sleep(backoff)
                        backoff = min(30.0, backoff * 2)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
        raise RuntimeError(f"TMDB request failed after retries: {path}")

    async def discover_movies(self, *, date_gte: str, date_lte: str, start_page: int = 1) -> Any:
        return await self.get(
            "/discover/movie",
            {
                "include_adult": "false",
                "include_video": "false",
                "language": "en-US",
                "sort_by": "popularity.desc",
                "primary_release_date.gte": date_gte,
                "primary_release_date.lte": date_lte,
                "page": start_page,
            },
        )

    async def discover_tv(
        self,
        *,
        date_gte: str,
        date_lte: str,
        start_page: int = 1,
        date_field: str = "first_air_date",
    ) -> Any:
        params = {
            "include_adult": "false",
            "language": "en-US",
            "sort_by": "popularity.desc",
            f"{date_field}.gte": date_gte,
            f"{date_field}.lte": date_lte,
            "page": start_page,
        }
        return await self.get("/discover/tv", params)

    async def trending(self, media: str) -> Any:
        return await self.get(f"/trending/{media}/week")

    async def movie_details(self, tmdb_id: int) -> Any:
        return await self.get(
            f"/movie/{tmdb_id}",
            {"append_to_response": "release_dates,external_ids"},
        )

    async def tv_details(self, tmdb_id: int) -> Any:
        return await self.get(
            f"/tv/{tmdb_id}",
            {"append_to_response": "external_ids"},
        )

    async def tv_external_ids(self, tmdb_id: int) -> Any:
        return await self.get(f"/tv/{tmdb_id}/external_ids")


async def lookup_tvdb_id(
    session: aiohttp.ClientSession,
    *,
    api_key: str,
    title: str,
    year: int | None = None,
) -> int | None:
    """TheTVDB v4 search fallback when TMDB omits ``tvdb_id``."""
    key = (api_key or "").strip()
    if not key or not (title or "").strip():
        return None
    async with session.post(
        "https://api4.thetvdb.com/v4/login",
        json={"apikey": key},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status >= 400:
            return None
        login = await resp.json()
    token = None
    if isinstance(login, dict):
        data = login.get("data") or {}
        if isinstance(data, dict):
            token = data.get("token")
    if not token:
        return None
    params: dict[str, Any] = {"query": title.strip(), "type": "series"}
    async with session.get(
        "https://api4.thetvdb.com/v4/search",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status >= 400:
            return None
        payload = await resp.json()
    return pick_tvdb_search_id(payload, title=title, year=year)


async def fill_missing_tvdb_id(
    client: TmdbClient,
    merged: dict[str, Any],
    *,
    tvdb_api_key: str = "",
) -> dict[str, Any]:
    """Fill ``tvdb_id`` from a dedicated TMDB external_ids call, then optional TVDB search."""
    if merged.get("tvdb_id"):
        return merged
    tmdb_id = merged.get("tmdb_id")
    try:
        tmdb_id_i = int(tmdb_id)
    except (TypeError, ValueError):
        return merged
    try:
        ext = await client.tv_external_ids(tmdb_id_i)
        tid = tvdb_id_from_external_ids(ext)
        if tid:
            merged["tvdb_id"] = tid
            return merged
    except Exception:
        logger.exception("tmdb_external_ids_failed", extra={"tmdb_id": tmdb_id_i})
    key = (tvdb_api_key or "").strip()
    if not key or client._session is None:
        return merged
    try:
        tid = await lookup_tvdb_id(
            client._session,
            api_key=key,
            title=str(merged.get("title") or ""),
            year=int(merged["year"]) if merged.get("year") is not None else None,
        )
        if tid:
            merged["tvdb_id"] = tid
    except Exception:
        logger.exception("tvdb_lookup_failed", extra={"tmdb_id": tmdb_id_i})
    return merged


def paginated_results(payload: Any) -> tuple[list[dict[str, Any]], int, int]:
    if not isinstance(payload, dict):
        return [], 1, 1
    results = [r for r in (payload.get("results") or []) if isinstance(r, dict)]
    page = int(payload.get("page") or 1)
    total_pages = int(payload.get("total_pages") or 1)
    total_pages = min(total_pages, 500)
    return results, page, total_pages
