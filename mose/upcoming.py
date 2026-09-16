"""Upcoming media catalog: TMDB ingest, local compare, weekly recommendations."""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from mose.config import Config, UpcomingConfig
from mose.observe import get_logger, log_event, log_duration
from mose.upcoming_arr import (
    ArrHttpClient,
    add_movie_payload,
    add_series_payload,
    resolve_arr_targets,
    snapshot_radarr,
    snapshot_sonarr,
)
from mose.upcoming_store import CatalogTitle, RecommendationItem, UpcomingStore
from mose.upcoming_tmdb import (
    TmdbClient,
    apply_movie_details,
    apply_tv_details,
    catalog_years,
    fill_missing_tvdb_id,
    paginated_results,
    title_from_discover_movie,
    title_from_discover_tv,
)

logger = get_logger("upcoming")

UPCOMING_APPROVAL_KIND = "upcoming_add"
DETAILS_STALE_SECONDS = 7 * 86400
ADD_ALLOWED_TOOLS = frozenset({
    "mcp-portal__portal_codemode_execute",
    "mcp-portal__portal_codemode_search",
})

UpcomingNotifyFn = Callable[..., Awaitable[None] | None]


def _tz(cfg: UpcomingConfig) -> ZoneInfo:
    name = (cfg.timezone or "America/Chicago").strip() or "America/Chicago"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("America/Chicago")


def iso_week_slug(when: datetime) -> str:
    iso = when.isocalendar()
    return f"upcoming-{iso.year}-W{iso.week:02d}"


def canonical_upcoming_slug(slug: str) -> str:
    """Normalize ``upcoming-YYYY-Www`` so approve replies survive case folding."""
    s = (slug or "").strip()
    m = re.fullmatch(r"upcoming-(\d{4})-w(\d{1,2})", s, flags=re.IGNORECASE)
    if not m:
        return s
    return f"upcoming-{m.group(1)}-W{int(m.group(2)):02d}"


def parse_iso_date(value: str | None) -> date | None:
    if not value or len(str(value)) < 10:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def dict_to_title(d: dict[str, Any], *, now: float | None = None) -> CatalogTitle:
    return CatalogTitle(
        media_type=str(d["media_type"]),
        tmdb_id=int(d["tmdb_id"]),
        kind=str(d["kind"]),
        title=str(d.get("title") or ""),
        tvdb_id=int(d["tvdb_id"]) if d.get("tvdb_id") is not None else None,
        imdb_id=d.get("imdb_id"),
        original_title=d.get("original_title"),
        overview=d.get("overview"),
        status=d.get("status"),
        popularity=float(d.get("popularity") or 0),
        vote_count=int(d.get("vote_count") or 0),
        vote_average=float(d.get("vote_average") or 0),
        original_language=d.get("original_language"),
        origin_country=d.get("origin_country"),
        genres=list(d.get("genres") or []),
        adult=bool(d.get("adult")),
        us_theatrical=d.get("us_theatrical"),
        us_digital=d.get("us_digital"),
        us_physical=d.get("us_physical"),
        jp_air_date=d.get("jp_air_date"),
        first_air_date=d.get("first_air_date"),
        release_date=d.get("release_date"),
        next_season_number=int(d["next_season_number"]) if d.get("next_season_number") is not None else None,
        next_season_air_date=d.get("next_season_air_date"),
        year=int(d["year"]) if d.get("year") is not None else None,
        details_fetched_at=d.get("details_fetched_at"),
        updated_at=now if now is not None else time.time(),
        tmdb_updated_at=d.get("tmdb_updated_at"),
    )


def candidate_dates(title: CatalogTitle) -> list[date]:
    if title.kind == "movie":
        raw = [title.us_digital, title.us_theatrical]
    else:
        raw = [title.next_season_air_date, title.first_air_date, title.jp_air_date]
    return [d for d in (parse_iso_date(v) for v in raw) if d is not None]


def relevant_date(title: CatalogTitle, *, today: date | None = None) -> date | None:
    """Soonest useful date: closest to ``today`` when given, else the earliest."""
    found = candidate_dates(title)
    if not found:
        return None
    if today is None:
        return min(found)
    return min(found, key=lambda d: abs((d - today).days))


def format_score_breakdown(title: CatalogTitle, *, today: date, imminent_days: int = 30) -> str:
    parts = [f"pop {title.popularity:.0f}"]
    if title.on_trending_week:
        parts.append("+50 trend")
    if title.trending_rank is not None and title.trending_rank <= 20:
        parts.append(f"+{20 - title.trending_rank} top20")
    if any(abs((d - today).days) <= imminent_days for d in candidate_dates(title)):
        parts.append("+25 soon")
    return " ".join(parts)


def compute_score(title: CatalogTitle, *, today: date, imminent_days: int = 30) -> float:
    score = float(title.popularity or 0)
    if title.on_trending_week:
        score += 50.0
    if title.trending_rank is not None and title.trending_rank <= 20:
        score += float(20 - title.trending_rank)
    if any(abs((d - today).days) <= imminent_days for d in candidate_dates(title)):
        score += 25.0
    return score


def in_recommendation_window(
    title: CatalogTitle,
    *,
    today: date,
    horizon_days: int,
    past_grace_days: int = 14,
) -> bool:
    found = candidate_dates(title)
    if not found:
        return False
    start = today - timedelta(days=past_grace_days)
    end = today + timedelta(days=horizon_days)
    return any(start <= d <= end for d in found)


def passes_rec_filters(title: CatalogTitle, cfg: UpcomingConfig) -> bool:
    if title.adult:
        return False
    if (title.popularity or 0) < cfg.min_popularity:
        return False
    if (title.vote_count or 0) < cfg.min_vote_count:
        return False
    if title.kind == "movie":
        if (title.original_language or "").lower() != "en":
            return False
        if not title.us_theatrical and not title.us_digital:
            return False
    elif title.kind == "tv":
        if (title.original_language or "").lower() != "en":
            return False
        if title.tvdb_id is None:
            return False
    elif title.kind == "anime":
        if title.tvdb_id is None:
            return False
        if not title.jp_air_date and not title.first_air_date and not title.next_season_air_date:
            return False
    else:
        return False
    return True


def select_recommendations(
    titles: list[CatalogTitle],
    *,
    cfg: UpcomingConfig,
    library_movie_ids: set[int],
    library_series_tvdb_ids: set[int],
    today: date,
) -> list[RecommendationItem]:
    """Rank and cap 40/40/20 with no cross-category backfill."""
    buckets: dict[str, list[tuple[float, int, str, CatalogTitle]]] = {
        "movie": [],
        "tv": [],
        "anime": [],
    }
    for t in titles:
        if t.kind not in buckets:
            continue
        if not passes_rec_filters(t, cfg):
            continue
        if not in_recommendation_window(t, today=today, horizon_days=cfg.recommendation_horizon_days):
            continue
        if t.kind == "movie" and t.tmdb_id in library_movie_ids:
            continue
        if t.kind in ("tv", "anime") and t.tvdb_id is not None and t.tvdb_id in library_series_tvdb_ids:
            continue
        score = compute_score(t, today=today)
        buckets[t.kind].append((score, t.vote_count, t.title or "", t))
    caps = {"movie": cfg.cap_movies, "tv": cfg.cap_tv, "anime": cfg.cap_anime}
    items: list[RecommendationItem] = []
    line = 1
    for kind in ("movie", "tv", "anime"):
        ranked = sorted(buckets[kind], key=lambda x: (-x[0], -x[1], x[2].lower()))
        for score, _votes, _title, t in ranked[: caps[kind]]:
            rel = relevant_date(t, today=today)
            items.append(
                RecommendationItem(
                    line_number=line,
                    kind=kind,
                    tmdb_id=t.tmdb_id,
                    tvdb_id=t.tvdb_id,
                    title=t.title,
                    year=t.year,
                    score=score,
                    payload={
                        "popularity": t.popularity,
                        "vote_count": t.vote_count,
                        "us_theatrical": t.us_theatrical,
                        "us_digital": t.us_digital,
                        "next_season_air_date": t.next_season_air_date,
                        "first_air_date": t.first_air_date,
                        "jp_air_date": t.jp_air_date,
                        "on_trending_week": t.on_trending_week,
                        "trending_rank": t.trending_rank,
                        "imdb_id": t.imdb_id,
                        "relevant_date": rel.isoformat() if rel else None,
                        "original_language": t.original_language,
                        "score_breakdown": format_score_breakdown(t, today=today),
                    },
                )
            )
            line += 1
    return items


def parse_line_numbers(spec: str | None) -> set[int] | None:
    """Parse ``1,4,7`` or ``1-3,8``. None spec means all. Empty invalid set is empty."""
    if spec is None or not str(spec).strip():
        return None
    out: set[int] = set()
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            for n in range(min(lo, hi), max(lo, hi) + 1):
                out.add(n)
        else:
            try:
                out.add(int(part))
            except ValueError:
                continue
    return out


def render_markdown(
    items: list[RecommendationItem],
    *,
    slug: str,
    generated_at: str,
    sync_at: str | None,
    today: date | None = None,
) -> str:
    counts = {"movie": 0, "tv": 0, "anime": 0}
    for it in items:
        counts[it.kind] = counts.get(it.kind, 0) + 1
    lines = [
        f"# Upcoming recommendations — {slug}",
        "",
        f"Generated: {generated_at}",
        f"Last library sync: {sync_at or 'unknown'}",
        "",
        f"Counts: **{counts['movie']} movies**, **{counts['tv']} TV**, **{counts['anime']} anime** "
        f"(fixed quotas, no backfill).",
        "",
        f"Approve all: `approve {slug}`",
        f"Approve subset: `approve {slug} 1,4,7`",
        f"Reject: `reject {slug}`",
        "",
    ]
    sections = (
        ("movie", "Movies"),
        ("tv", "TV"),
        ("anime", "Anime"),
    )
    for kind, heading in sections:
        subset = [it for it in items if it.kind == kind]
        lines.append(f"## {heading} ({len(subset)})")
        lines.append("")
        if not subset:
            lines.append("_No eligible titles this week._")
            lines.append("")
            continue
        lines.append(
            "| # | Title | Year | Dates | Popularity | Score | IDs |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for it in subset:
            p = it.payload or {}
            if kind == "movie":
                dates = f"US theatrical {p.get('us_theatrical') or '—'}; digital {p.get('us_digital') or '—'}"
            else:
                dates = (
                    f"next {p.get('next_season_air_date') or '—'}; "
                    f"first {p.get('first_air_date') or '—'}"
                )
            ids = f"tmdb {it.tmdb_id}"
            if it.tvdb_id:
                ids += f"; tvdb {it.tvdb_id}"
            trend = " trending" if p.get("on_trending_week") else ""
            breakdown = p.get("score_breakdown") or ""
            score_cell = f"{it.score:.1f} ({breakdown})" if breakdown else f"{it.score:.1f}"
            lines.append(
                f"| {it.line_number} | {it.title} | {it.year or '—'} | {dates} | "
                f"{p.get('popularity', '')}{trend} | {score_cell} | {ids} |"
            )
        lines.append("")
    return "\n".join(lines)


def needs_details(existing: CatalogTitle | None, *, now: float, force: bool = False) -> bool:
    if force:
        return True
    if existing is None:
        return True
    fetched = existing.details_fetched_at or 0
    if now - fetched > DETAILS_STALE_SECONDS:
        return True
    if existing.media_type == "movie" and not existing.us_theatrical and not existing.us_digital:
        return True
    if existing.media_type == "tv" and existing.tvdb_id is None:
        return True
    return False


async def _discover_all_pages(
    fetcher,
    *,
    cursor_key: str,
    store: UpcomingStore,
    start_page: int = 1,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = max(1, start_page)
    total_pages = page
    while page <= total_pages:
        payload = await fetcher(page)
        batch, _, total_pages = paginated_results(payload)
        items.extend(batch)
        store.set_cursor(cursor_key, str(page))
        log_event(logger, "tmdb_discover_page", cursor=cursor_key, page=page, total_pages=total_pages)
        page += 1
    store.clear_cursor(cursor_key)
    return items


async def refresh_trending_catalog(
    store: UpcomingStore,
    client: TmdbClient,
    cfg: UpcomingConfig,
) -> dict[str, int]:
    """Trending week merge + detail refresh for trending/stale rows (weekly extra pass)."""
    today = datetime.now(_tz(cfg)).date()
    y0, y1 = catalog_years(today=today)
    catalog_start = date(y0, 1, 1)
    catalog_end = date(y1, 12, 31)
    now = time.time()
    stats = {"trending": 0, "details": 0}
    store.clear_trending_flags()
    trending_force: set[tuple[str, int]] = set()
    for media in ("movie", "tv"):
        payload = await client.trending(media)
        results, _, _ = paginated_results(payload)
        for rank, raw in enumerate(results, start=1):
            parsed = title_from_discover_movie(raw) if media == "movie" else title_from_discover_tv(raw)
            store.upsert_title(dict_to_title(parsed, now=now))
            store.set_trending(parsed["media_type"], parsed["tmdb_id"], rank)
            trending_force.add((parsed["media_type"], parsed["tmdb_id"]))
            stats["trending"] += 1
    stats["details"] = await _refresh_title_details(
        store,
        client,
        cfg,
        today=today,
        catalog_start=catalog_start,
        catalog_end=catalog_end,
        now=now,
        trending_force=trending_force,
    )
    return stats


async def _refresh_title_details(
    store: UpcomingStore,
    client: TmdbClient,
    cfg: UpcomingConfig,
    *,
    today: date,
    catalog_start: date,
    catalog_end: date,
    now: float,
    trending_force: set[tuple[str, int]],
) -> int:
    refreshed = 0
    for t in store.list_titles():
        force = (t.media_type, t.tmdb_id) in trending_force
        if not needs_details(t, now=now, force=force):
            continue
        try:
            if t.media_type == "movie":
                details = await client.movie_details(t.tmdb_id)
                merged = apply_movie_details(
                    {
                        "media_type": "movie",
                        "tmdb_id": t.tmdb_id,
                        "kind": "movie",
                        "title": t.title,
                        "popularity": t.popularity,
                        "vote_count": t.vote_count,
                        "original_language": t.original_language,
                        "adult": t.adult,
                        "genres": t.genres,
                    },
                    details if isinstance(details, dict) else {},
                )
                merged["details_fetched_at"] = now
                store.upsert_title(dict_to_title(merged, now=now))
            else:
                details = await client.tv_details(t.tmdb_id)
                merged = apply_tv_details(
                    {
                        "media_type": "tv",
                        "tmdb_id": t.tmdb_id,
                        "kind": t.kind,
                        "title": t.title,
                        "popularity": t.popularity,
                        "vote_count": t.vote_count,
                        "original_language": t.original_language,
                        "adult": t.adult,
                        "genres": t.genres,
                        "origin_country": t.origin_country,
                        "first_air_date": t.first_air_date,
                    },
                    details if isinstance(details, dict) else {},
                    today=today,
                    catalog_start=catalog_start,
                    catalog_end=catalog_end,
                )
                seasons = merged.pop("seasons", [])
                if merged.get("tvdb_id") is None:
                    merged = await fill_missing_tvdb_id(
                        client, merged, tvdb_api_key=cfg.tvdb_api_key
                    )
                merged["details_fetched_at"] = now
                store.upsert_title(dict_to_title(merged, now=now))
                if seasons:
                    store.replace_seasons(
                        t.tmdb_id,
                        [s for s in seasons if s.get("season_number") is not None],
                    )
            refreshed += 1
        except Exception:
            logger.exception("tmdb_details_failed", extra={"media": t.media_type, "tmdb_id": t.tmdb_id})
    return refreshed


async def ingest_tmdb(store: UpcomingStore, client: TmdbClient, cfg: UpcomingConfig) -> dict[str, int]:
    today = datetime.now(_tz(cfg)).date()
    y0, y1 = catalog_years(today=today)
    date_gte = f"{y0}-01-01"
    date_lte = f"{y1}-12-31"
    catalog_start = date(y0, 1, 1)
    catalog_end = date(y1, 12, 31)
    now = time.time()
    stats = {"movies_discovered": 0, "tv_discovered": 0, "details": 0, "trending": 0}

    movie_start = int(store.get_cursor("discover_movie") or "1")
    movies = await _discover_all_pages(
        lambda p: client.discover_movies(date_gte=date_gte, date_lte=date_lte, start_page=p),
        cursor_key="discover_movie",
        store=store,
        start_page=movie_start,
    )
    for raw in movies:
        d = title_from_discover_movie(raw)
        store.upsert_title(dict_to_title(d, now=now))
    stats["movies_discovered"] = len(movies)

    tv_map: dict[int, dict[str, Any]] = {}
    for field, cursor in (("first_air_date", "discover_tv_first"), ("air_date", "discover_tv_air")):
        start = int(store.get_cursor(cursor) or "1")
        rows = await _discover_all_pages(
            lambda p, f=field: client.discover_tv(
                date_gte=date_gte, date_lte=date_lte, start_page=p, date_field=f
            ),
            cursor_key=cursor,
            store=store,
            start_page=start,
        )
        for raw in rows:
            parsed = title_from_discover_tv(raw)
            tv_map[parsed["tmdb_id"]] = parsed
    for d in tv_map.values():
        store.upsert_title(dict_to_title(d, now=now))
    stats["tv_discovered"] = len(tv_map)

    store.clear_trending_flags()
    trending_force: set[tuple[str, int]] = set()
    for media in ("movie", "tv"):
        payload = await client.trending(media)
        results, _, _ = paginated_results(payload)
        for rank, raw in enumerate(results, start=1):
            parsed = title_from_discover_movie(raw) if media == "movie" else title_from_discover_tv(raw)
            store.upsert_title(dict_to_title(parsed, now=now))
            store.set_trending(parsed["media_type"], parsed["tmdb_id"], rank)
            trending_force.add((parsed["media_type"], parsed["tmdb_id"]))
            stats["trending"] += 1

    stats["details"] = await _refresh_title_details(
        store,
        client,
        cfg,
        today=today,
        catalog_start=catalog_start,
        catalog_end=catalog_end,
        now=now,
        trending_force=trending_force,
    )
    return stats


async def ingest_arr(store: UpcomingStore, cfg: UpcomingConfig) -> dict[str, int]:
    stats = {"movies": 0, "series": 0}
    radarr_folders: list[dict[str, Any]] = []
    radarr_profiles: list[dict[str, Any]] = []
    sonarr_folders: list[dict[str, Any]] = []
    sonarr_profiles: list[dict[str, Any]] = []
    if cfg.radarr_url and cfg.radarr_api_key:
        async with ArrHttpClient(cfg.radarr_url, cfg.radarr_api_key) as client:
            movies, radarr_folders, radarr_profiles = await snapshot_radarr(client)
            store.replace_library_movies(movies)
            stats["movies"] = len(movies)
    else:
        store.replace_library_movies([])
    if cfg.sonarr_url and cfg.sonarr_api_key:
        async with ArrHttpClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
            series, sonarr_folders, sonarr_profiles = await snapshot_sonarr(client)
            store.replace_library_series(series)
            stats["series"] = len(series)
    else:
        store.replace_library_series([])
    resolved = resolve_arr_targets(
        radarr_folders=radarr_folders,
        radarr_profiles=radarr_profiles,
        sonarr_folders=sonarr_folders,
        sonarr_profiles=sonarr_profiles,
        radarr_root_path=cfg.radarr_root_path,
        sonarr_tv_root_path=cfg.sonarr_tv_root_path,
        sonarr_anime_root_path=cfg.sonarr_anime_root_path,
        sonarr_tv_root_match=cfg.sonarr_tv_root_match,
        sonarr_anime_root_match=cfg.sonarr_anime_root_match,
        radarr_quality_profile_id=cfg.radarr_quality_profile_id,
        sonarr_tv_quality_profile_id=cfg.sonarr_tv_quality_profile_id,
        sonarr_anime_quality_profile_id=cfg.sonarr_anime_quality_profile_id,
    )
    store.set_arr_config(resolved)
    return stats


def prune_store(store: UpcomingStore, cfg: UpcomingConfig, *, today: date | None = None) -> dict[str, int]:
    today = today or date.today()
    year = today.year - cfg.catalog_retention_years
    try:
        cutoff = date(year, today.month, today.day)
    except ValueError:
        cutoff = date(year, today.month, 28)
    rec_cut = time.time() - cfg.recommendation_retention_days * 86400
    return store.prune(catalog_cutoff_date=cutoff.isoformat(), recommendation_cutoff_ts=rec_cut)


async def run_daily_sync(
    store: UpcomingStore,
    cfg: UpcomingConfig,
    *,
    tmdb: TmdbClient | None = None,
) -> dict[str, Any]:
    run_id = store.begin_sync_run("daily")
    stats: dict[str, Any] = {}
    try:
        with log_duration(logger, "upcoming_daily_sync"):
            if tmdb is None and cfg.tmdb_api_key:
                async with TmdbClient(cfg.tmdb_api_key) as client:
                    stats["tmdb"] = await ingest_tmdb(store, client, cfg)
            elif tmdb is not None:
                stats["tmdb"] = await ingest_tmdb(store, tmdb, cfg)
            elif not cfg.tmdb_api_key:
                log_event(logger, "upcoming_tmdb_skipped_no_key")
            stats["arr"] = await ingest_arr(store, cfg)
            stats["prune"] = prune_store(store, cfg)
        store.finish_sync_run(run_id, status="ok", stats=stats)
        _clear_failure_alert("daily")
        log_event(logger, "upcoming_daily_sync_ok", run_id=run_id, **{k: str(v)[:200] for k, v in stats.items()})
        return {"status": "ok", "run_id": run_id, "stats": stats}
    except Exception as e:
        logger.exception("upcoming_daily_sync_failed")
        store.finish_sync_run(run_id, status="failed", error=str(e)[:4000], stats=stats)
        return {"status": "failed", "run_id": run_id, "error": str(e), "stats": stats}


def build_recommendation(
    store: UpcomingStore,
    cfg: UpcomingConfig,
    *,
    log_dir: str | Path,
    when: datetime | None = None,
) -> tuple[str, Path, list[RecommendationItem], dict[str, int]]:
    tz = _tz(cfg)
    when = when or datetime.now(tz)
    today = when.date()
    slug = iso_week_slug(when)
    items = select_recommendations(
        store.list_titles(),
        cfg=cfg,
        library_movie_ids=store.library_movie_ids(),
        library_series_tvdb_ids=store.library_series_tvdb_ids(),
        today=today,
    )
    counts = {
        "movie": sum(1 for i in items if i.kind == "movie"),
        "tv": sum(1 for i in items if i.kind == "tv"),
        "anime": sum(1 for i in items if i.kind == "anime"),
        "total": len(items),
    }
    last_sync = store.last_successful_sync("daily")
    sync_at = None
    if last_sync:
        sync_at = datetime.fromtimestamp(last_sync, tz=timezone.utc).isoformat(timespec="minutes")
    md = render_markdown(
        items,
        slug=slug,
        generated_at=when.isoformat(timespec="minutes"),
        sync_at=sync_at,
        today=today,
    )
    path = Path(log_dir) / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    store.save_recommendation_run(slug=slug, report_path=str(path), items=items, counts=counts)
    return slug, path, items, counts


_notify: UpcomingNotifyFn | None = None
_failure_notify: UpcomingNotifyFn | None = None
_failure_alerted: set[str] = set()


def init_upcoming_notify(callback: UpcomingNotifyFn | None) -> None:
    global _notify
    _notify = callback


def init_upcoming_failure_notify(callback: UpcomingNotifyFn | None) -> None:
    global _failure_notify
    _failure_notify = callback


async def _call_notify(fn: UpcomingNotifyFn | None, *args: Any) -> None:
    if fn is None:
        return
    ret = fn(*args)
    if hasattr(ret, "__await__"):
        await ret


def _clear_failure_alert(kind: str) -> None:
    _failure_alerted.discard(kind)


async def maybe_alert_failures(store: UpcomingStore, cfg: UpcomingConfig, kind: str) -> None:
    n = store.consecutive_failures(kind)
    if n < max(1, cfg.failure_threshold):
        return
    if kind in _failure_alerted:
        return
    run_id, error = store.last_sync_error(kind)
    _failure_alerted.add(kind)
    await _call_notify(
        _failure_notify,
        kind,
        f"Upcoming {kind} failed {n} times in a row (run id {run_id}). Last error: {error}",
        None,
    )


async def run_weekly_recommend(
    store: UpcomingStore,
    cfg: UpcomingConfig,
    *,
    log_dir: str | Path,
    memory: Any | None = None,
    recipient: str = "",
    tmdb: TmdbClient | None = None,
    when: datetime | None = None,
) -> dict[str, Any]:
    last = store.last_successful_sync("daily")
    now = time.time()
    stale = last is None or (now - last) > cfg.sync_max_age_hours * 3600
    week_id = store.begin_sync_run("weekly")
    try:
        if stale:
            result = await run_daily_sync(store, cfg, tmdb=tmdb)
            if result.get("status") != "ok":
                store.finish_sync_run(
                    week_id, status="failed", error="daily sync required before weekly recommend failed"
                )
                await maybe_alert_failures(store, cfg, "daily")
                await maybe_alert_failures(store, cfg, "weekly")
                return {"status": "failed", "error": "daily sync required before weekly recommend failed"}
        elif cfg.tmdb_api_key:
            if tmdb is None:
                async with TmdbClient(cfg.tmdb_api_key) as client:
                    await refresh_trending_catalog(store, client, cfg)
            else:
                await refresh_trending_catalog(store, tmdb, cfg)
        tz = _tz(cfg)
        when = when or datetime.now(tz)
        slug, path, items, counts = build_recommendation(store, cfg, log_dir=log_dir, when=when)
        run = store.get_recommendation_run(slug)
        run_id = run["id"] if run else 0
        if memory is not None and counts.get("total", 0) > 0:
            expires = time.time() + 7 * 86400
            memory.save_pending_approval(
                slug=slug,
                kind=UPCOMING_APPROVAL_KIND,
                recipient=recipient or "signal:admin",
                proposal_path=str(path),
                payload={
                    "recommendation_run_id": run_id,
                    "report_path": str(path),
                    "counts": counts,
                    "allowed_tools": sorted(ADD_ALLOWED_TOOLS),
                },
                expires_at=expires,
            )
        summary = (
            f"Upcoming recommendations {slug}: "
            f"{counts['movie']} movies, {counts['tv']} TV, {counts['anime']} anime "
            f"({counts['total']} total).\n"
            f"Reply `approve {slug}` or `approve {slug} 1,4,7` or `reject {slug}`."
        )
        await _call_notify(_notify, summary, str(path), str(path))
        store.finish_sync_run(week_id, status="ok", stats=counts)
        _clear_failure_alert("weekly")
        log_event(logger, "upcoming_weekly_ok", slug=slug, **counts)
        return {"status": "ok", "slug": slug, "path": str(path), "counts": counts, "items": items}
    except Exception as e:
        logger.exception("upcoming_weekly_failed")
        store.finish_sync_run(week_id, status="failed", error=str(e)[:4000])
        await maybe_alert_failures(store, cfg, "weekly")
        return {"status": "failed", "error": str(e)}


def codemode_add_movie(item: RecommendationItem, arr_cfg: dict[str, Any]) -> str:
    root = json_dumps(arr_cfg.get("radarr_root_path") or "")
    qid = int(arr_cfg.get("radarr_quality_profile_id") or 0)
    return (
        "const r = await mcp.plex_stack_automation.radarr_add_movie({\n"
        f"  tmdbId: {int(item.tmdb_id)},\n"
        f"  qualityProfileId: {qid},\n"
        f"  rootFolderPath: {root},\n"
        "  monitored: true\n"
        "});\n"
        "console.log(JSON.stringify(r));\n"
    )


def json_dumps(value: Any) -> str:
    return json.dumps(value)


def codemode_add_series(item: RecommendationItem, arr_cfg: dict[str, Any]) -> str:
    if item.kind == "anime":
        root = arr_cfg.get("sonarr_anime_root_path") or ""
        qid = int(arr_cfg.get("sonarr_anime_quality_profile_id") or 0)
        stype = "anime"
    else:
        root = arr_cfg.get("sonarr_tv_root_path") or ""
        qid = int(arr_cfg.get("sonarr_tv_quality_profile_id") or 0)
        stype = "standard"
    return (
        f"const lookup = await mcp.sonarr_diagnostics.sonarr_get_series_lookup({{ term: `tvdb:{int(item.tvdb_id)}` }});\n"
        "const row = Array.isArray(lookup) ? lookup[0] : lookup;\n"
        "const r = await mcp.plex_stack_automation.sonarr_add_series({\n"
        f"  tvdbId: {int(item.tvdb_id or 0)},\n"
        f"  qualityProfileId: {qid},\n"
        f"  rootFolderPath: {json_dumps(root)},\n"
        f"  seriesType: {json_dumps(stype)},\n"
        "  monitored: true\n"
        "});\n"
        "console.log(JSON.stringify({lookup: row, add: r}));\n"
    )


async def add_item_http(item: RecommendationItem, cfg: UpcomingConfig, arr_cfg: dict[str, Any]) -> str:
    if item.kind == "movie":
        qid = int(arr_cfg.get("radarr_quality_profile_id") or 0)
        root = str(arr_cfg.get("radarr_root_path") or "")
        if not qid or not root:
            raise RuntimeError("Radarr root folder or quality profile is not resolved")
        async with ArrHttpClient(cfg.radarr_url, cfg.radarr_api_key) as client:
            body = add_movie_payload(
                title=item.title,
                tmdb_id=item.tmdb_id,
                quality_profile_id=qid,
                root_folder_path=root,
                year=item.year,
            )
            result = await client.post_json("/movie", body)
            return str(result)[:1500]
    qid_key = "sonarr_anime_quality_profile_id" if item.kind == "anime" else "sonarr_tv_quality_profile_id"
    root_key = "sonarr_anime_root_path" if item.kind == "anime" else "sonarr_tv_root_path"
    qid = int(arr_cfg.get(qid_key) or 0)
    root = str(arr_cfg.get(root_key) or "")
    if not qid or not root or not item.tvdb_id:
        raise RuntimeError("Sonarr root/profile/tvdb missing for add")
    async with ArrHttpClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
        lookup = await client.get_json("/series/lookup", {"term": f"tvdb:{item.tvdb_id}"})
        row = lookup[0] if isinstance(lookup, list) and lookup else lookup
        if not isinstance(row, dict):
            raise RuntimeError(f"Sonarr lookup failed for tvdb:{item.tvdb_id}")
        body = add_series_payload(
            row,
            quality_profile_id=qid,
            root_folder_path=root,
            series_type="anime" if item.kind == "anime" else "standard",
        )
        result = await client.post_json("/series", body)
        return str(result)[:1500]


async def execute_upcoming_adds(
    items: list[RecommendationItem],
    cfg: UpcomingConfig,
    arr_cfg: dict[str, Any],
    *,
    execute_codemode: Callable[[str, int], Awaitable[tuple[str, bool]]] | None = None,
    add_http: Callable[..., Awaitable[str]] | None = None,
) -> list[dict[str, Any]]:
    from mose.tools import enter_scheduled_execution, exit_scheduled_execution

    token = enter_scheduled_execution("upcoming-add", ADD_ALLOWED_TOOLS)
    results: list[dict[str, Any]] = []
    try:
        for it in items:
            try:
                if execute_codemode is not None:
                    try:
                        code = (
                            codemode_add_movie(it, arr_cfg)
                            if it.kind == "movie"
                            else codemode_add_series(it, arr_cfg)
                        )
                        text, is_err = await execute_codemode(code, 60)
                        if is_err:
                            raise RuntimeError(text[:1500])
                        results.append(
                            {"line": it.line_number, "title": it.title, "ok": True, "detail": text[:500]}
                        )
                        continue
                    except Exception:
                        logger.exception(
                            "upcoming_codemode_add_failed_fallback_http",
                            extra={"line": it.line_number},
                        )
                fn = add_http or add_item_http
                detail = await fn(it, cfg, arr_cfg)
                results.append({"line": it.line_number, "title": it.title, "ok": True, "detail": detail[:500]})
            except Exception as e:
                logger.exception("upcoming_add_item_failed", extra={"line": it.line_number, "title": it.title})
                results.append({"line": it.line_number, "title": it.title, "ok": False, "detail": str(e)[:500]})
    finally:
        exit_scheduled_execution(token)
    return results


def weekday_name(cfg: UpcomingConfig) -> str:
    return (cfg.weekly_weekday or "monday").strip().lower()


def should_run_daily(store: UpcomingStore, cfg: UpcomingConfig, now: datetime) -> bool:
    if now.hour < int(cfg.daily_hour):
        return False
    last = store.last_successful_sync("daily")
    if last is None:
        return True
    last_dt = datetime.fromtimestamp(last, tz=now.tzinfo)
    if last_dt.date() == now.date():
        return False
    return (now.timestamp() - last) >= 20 * 3600


def should_run_weekly(store: UpcomingStore, cfg: UpcomingConfig, now: datetime) -> bool:
    if now.strftime("%A").lower() != weekday_name(cfg):
        return False
    if now.hour < int(cfg.weekly_hour):
        return False
    slug = iso_week_slug(now)
    existing = store.get_recommendation_run(slug)
    return existing is None


class UpcomingLoop:
    def __init__(self, config: Config, store: UpcomingStore, *, memory: Any | None = None) -> None:
        self.config = config
        self.store = store
        self.memory = memory
        self._task: asyncio.Task[Any] | None = None

    def start(self) -> None:
        cfg = self.config.upcoming
        if not cfg.enabled:
            return
        if self._task is not None and not self._task.done():
            return

        async def _loop() -> None:
            delay = max(0, int(cfg.startup_delay_seconds))
            interval = max(15, int(cfg.reconcile_interval_seconds))
            try:
                if delay:
                    await asyncio.sleep(delay)
                while True:
                    try:
                        await self.reconcile()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("upcoming_reconcile_failed")
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                log_event(logger, "upcoming_loop_cancelled")
                raise

        self._task = asyncio.create_task(_loop(), name="upcoming-loop")
        log_event(logger, "upcoming_loop_started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    async def reconcile(self) -> None:
        cfg = self.config.upcoming
        if not cfg.enabled:
            return
        now = datetime.now(_tz(cfg))
        if should_run_daily(self.store, cfg, now):
            result = await run_daily_sync(self.store, cfg)
            if result.get("status") != "ok":
                await maybe_alert_failures(self.store, cfg, "daily")
        if should_run_weekly(self.store, cfg, now):
            result = await run_weekly_recommend(
                self.store,
                cfg,
                log_dir=self.config.observe.log_dir,
                memory=self.memory,
                recipient=(self.config.signal.admin_group_id or "").strip(),
            )
            if result.get("status") != "ok":
                await maybe_alert_failures(self.store, cfg, "weekly")
