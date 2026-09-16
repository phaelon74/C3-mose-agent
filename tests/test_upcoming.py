"""Tests for upcoming movies/TV/anime catalog."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from mose.config import UpcomingConfig, load_config
from mose.mcp_write_policy import classify_mcp_tool
from mose.memory import MemoryConfig, MemoryManager
from mose.tools import scheduled_execution_bypasses_approval
from mose.upcoming import (
    UPCOMING_APPROVAL_KIND,
    build_recommendation,
    canonical_upcoming_slug,
    compute_score,
    execute_upcoming_adds,
    format_score_breakdown,
    in_recommendation_window,
    iso_week_slug,
    parse_line_numbers,
    passes_rec_filters,
    render_markdown,
    select_recommendations,
    should_run_daily,
    should_run_weekly,
)
from mose.upcoming_arr import (
    compact_movie_row,
    movie_from_radarr,
    paginate_index,
    pick_root_folder,
    resolve_arr_targets,
    series_from_sonarr,
)
from mose.upcoming_decision import format_upcoming_recovery_message, handle_upcoming_decision, init_upcoming_decision_runtime
from mose.upcoming_store import CatalogTitle, LibraryMovie, LibrarySeries, RecommendationItem, UpcomingStore
from mose.upcoming_tmdb import (
    apply_tv_details,
    compute_next_season,
    extract_us_release_dates,
    is_anime,
    pick_tvdb_search_id,
    title_from_discover_tv,
    tmdb_updated_at_from_details,
    tvdb_id_from_external_ids,
)


def _cfg(**kwargs) -> UpcomingConfig:
    base = UpcomingConfig(
        min_popularity=1.0,
        min_vote_count=1,
        cap_movies=40,
        cap_tv=40,
        cap_anime=20,
        recommendation_horizon_days=180,
        timezone="UTC",
    )
    for k, v in kwargs.items():
        setattr(base, k, v)
    return base


def _title(**kwargs) -> CatalogTitle:
    defaults = dict(
        media_type="movie",
        tmdb_id=1,
        kind="movie",
        title="Example",
        popularity=20.0,
        vote_count=100,
        original_language="en",
        adult=False,
        us_theatrical="2026-06-01",
        us_digital="2026-09-01",
        year=2026,
    )
    defaults.update(kwargs)
    return CatalogTitle(**defaults)


class TestTmdbParsers:
    def test_us_release_dates(self):
        payload = {
            "results": [
                {
                    "iso_3166_1": "US",
                    "release_dates": [
                        {"type": 3, "release_date": "2026-05-20T00:00:00.000Z"},
                        {"type": 4, "release_date": "2026-08-01T00:00:00.000Z"},
                        {"type": 5, "release_date": "2026-10-01T00:00:00.000Z"},
                    ],
                }
            ]
        }
        dates = extract_us_release_dates(payload)
        assert dates["us_theatrical"] == "2026-05-20"
        assert dates["us_digital"] == "2026-08-01"
        assert dates["us_physical"] == "2026-10-01"

    def test_next_season_prefers_unaired(self):
        seasons = [
            {"season_number": 0, "air_date": "2026-01-01"},
            {"season_number": 1, "air_date": "2025-01-01"},
            {"season_number": 2, "air_date": "2026-09-01"},
            {"season_number": 3, "air_date": "2026-12-01"},
        ]
        n, d = compute_next_season(
            seasons,
            today=date(2026, 4, 1),
            catalog_start=date(2026, 1, 1),
            catalog_end=date(2027, 12, 31),
        )
        assert n == 2
        assert d == "2026-09-01"

    def test_anime_wins_classification(self):
        assert is_anime(genre_ids=[16], origin_country=["JP"], original_language="ja")
        parsed = title_from_discover_tv(
            {
                "id": 9,
                "name": "New Anime",
                "genre_ids": [16],
                "origin_country": ["JP"],
                "original_language": "ja",
                "popularity": 40,
                "vote_count": 10,
            }
        )
        assert parsed["kind"] == "anime"

    def test_english_animation_is_tv_not_anime(self):
        parsed = title_from_discover_tv(
            {
                "id": 10,
                "name": "Western Cartoon",
                "genre_ids": [16],
                "origin_country": ["US"],
                "original_language": "en",
                "popularity": 40,
                "vote_count": 10,
            }
        )
        assert parsed["kind"] == "tv"

    def test_apply_tv_details_anime(self):
        merged = apply_tv_details(
            {"media_type": "tv", "tmdb_id": 1, "kind": "tv", "title": "X", "genres": [16]},
            {
                "origin_country": ["JP"],
                "original_language": "ja",
                "genres": [{"id": 16, "name": "Animation"}],
                "external_ids": {"tvdb_id": 55},
                "first_air_date": "2026-04-01",
                "seasons": [{"season_number": 1, "air_date": "2026-04-01", "episode_count": 12}],
            },
            today=date(2026, 1, 1),
            catalog_start=date(2026, 1, 1),
            catalog_end=date(2027, 12, 31),
        )
        assert merged["kind"] == "anime"
        assert merged["tvdb_id"] == 55
        assert merged["next_season_number"] == 1

    def test_tvdb_id_from_external_ids(self):
        assert tvdb_id_from_external_ids({"tvdb_id": 77}) == 77
        assert tvdb_id_from_external_ids({"tvdb_id": None}) is None
        assert tvdb_id_from_external_ids(None) is None

    def test_tmdb_updated_at_from_details(self):
        assert tmdb_updated_at_from_details({"updated_at": "2026-01-02T00:00:00.000Z"}) == "2026-01-02T00:00:00.000Z"

    def test_pick_tvdb_search_prefers_year_and_title(self):
        payload = {
            "data": [
                {"name": "Other", "year": "2024", "id": 1},
                {"name": "New Show", "year": "2026", "tvdb_id": 88},
            ]
        }
        assert pick_tvdb_search_id(payload, title="New Show", year=2026) == 88

    @pytest.mark.asyncio
    async def test_fill_missing_tvdb_id_from_tmdb(self):
        from mose.upcoming_tmdb import fill_missing_tvdb_id

        class _Client:
            _session = None

            async def tv_external_ids(self, tmdb_id: int):
                assert tmdb_id == 9
                return {"tvdb_id": 321}

        merged = {"tmdb_id": 9, "title": "X"}
        out = await fill_missing_tvdb_id(_Client(), merged)  # type: ignore[arg-type]
        assert out["tvdb_id"] == 321


class TestScoringAndFilters:
    def test_score_trending_and_imminent(self):
        t = _title(popularity=10, on_trending_week=True, trending_rank=1, us_theatrical="2026-04-10")
        score = compute_score(t, today=date(2026, 4, 1))
        # 10 + 50 + (20-1) + 25
        assert score == pytest.approx(104.0)
        assert "pop 10" in format_score_breakdown(t, today=date(2026, 4, 1))
        assert "+50 trend" in format_score_breakdown(t, today=date(2026, 4, 1))

    def test_horizon_excludes_far_future(self):
        t = _title(us_theatrical="2027-12-01", us_digital=None)
        assert not in_recommendation_window(t, today=date(2026, 3, 1), horizon_days=180)

    def test_horizon_any_qualifying_date(self):
        movie = _title(us_theatrical="2025-01-01", us_digital="2026-06-01")
        assert in_recommendation_window(movie, today=date(2026, 3, 1), horizon_days=180)
        show = _title(
            media_type="tv",
            kind="tv",
            tvdb_id=9,
            first_air_date="2015-01-01",
            next_season_air_date="2026-09-01",
        )
        assert in_recommendation_window(show, today=date(2026, 4, 1), horizon_days=180)

    def test_english_movie_filter(self):
        cfg = _cfg()
        t = _title(original_language="fr")
        assert not passes_rec_filters(t, cfg)

    def test_missing_tvdb_skipped(self):
        cfg = _cfg()
        t = _title(media_type="tv", kind="tv", tvdb_id=None, first_air_date="2026-05-01")
        assert not passes_rec_filters(t, cfg)


class TestSelectRecommendations:
    def test_excludes_radarr_and_sonarr_and_new_seasons(self):
        cfg = _cfg(cap_movies=40, cap_tv=40, cap_anime=20)
        titles = [
            _title(tmdb_id=1, title="In Radarr", us_theatrical="2026-05-01"),
            _title(tmdb_id=2, title="New Movie", us_theatrical="2026-05-01", popularity=50),
            _title(
                media_type="tv",
                kind="tv",
                tmdb_id=3,
                tvdb_id=100,
                title="In Sonarr",
                first_air_date="2026-05-01",
            ),
            _title(
                media_type="tv",
                kind="tv",
                tmdb_id=4,
                tvdb_id=101,
                title="New Show",
                first_air_date="2026-05-01",
                popularity=40,
            ),
            _title(
                media_type="tv",
                kind="anime",
                tmdb_id=5,
                tvdb_id=200,
                title="New Anime",
                first_air_date="2026-05-01",
                jp_air_date="2026-05-01",
                original_language="ja",
                popularity=30,
            ),
        ]
        items = select_recommendations(
            titles,
            cfg=cfg,
            library_movie_ids={1},
            library_series_tvdb_ids={100},
            today=date(2026, 4, 1),
        )
        titles_out = {i.title for i in items}
        assert "In Radarr" not in titles_out
        assert "In Sonarr" not in titles_out
        assert "New Movie" in titles_out
        assert "New Show" in titles_out
        assert "New Anime" in titles_out

    def test_fixed_quotas_no_backfill(self):
        cfg = _cfg(cap_movies=40, cap_tv=40, cap_anime=20)
        titles = [
            _title(tmdb_id=i, title=f"M{i}", popularity=100 - i, us_theatrical="2026-05-01")
            for i in range(5)
        ]
        titles += [
            _title(
                media_type="tv",
                kind="anime",
                tmdb_id=1000 + i,
                tvdb_id=3000 + i,
                title=f"A{i}",
                original_language="ja",
                first_air_date="2026-05-01",
                jp_air_date="2026-05-01",
                popularity=50,
            )
            for i in range(12)
        ]
        items = select_recommendations(
            titles,
            cfg=cfg,
            library_movie_ids=set(),
            library_series_tvdb_ids=set(),
            today=date(2026, 4, 1),
        )
        assert sum(1 for i in items if i.kind == "anime") == 12
        assert sum(1 for i in items if i.kind == "movie") == 5
        assert sum(1 for i in items if i.kind == "tv") == 0
        assert len(items) == 17

    def test_anime_not_in_tv_bucket(self):
        cfg = _cfg()
        t = _title(
            media_type="tv",
            kind="anime",
            tmdb_id=9,
            tvdb_id=9,
            title="Only Anime",
            original_language="ja",
            first_air_date="2026-05-01",
            jp_air_date="2026-05-01",
        )
        items = select_recommendations(
            [t],
            cfg=cfg,
            library_movie_ids=set(),
            library_series_tvdb_ids=set(),
            today=date(2026, 4, 1),
        )
        assert len(items) == 1
        assert items[0].kind == "anime"

    def test_trending_outside_horizon_excluded(self):
        cfg = _cfg(recommendation_horizon_days=180)
        t = _title(
            tmdb_id=8,
            title="Far",
            on_trending_week=True,
            trending_rank=1,
            us_theatrical="2027-12-01",
            us_digital=None,
            popularity=999,
        )
        items = select_recommendations(
            [t],
            cfg=cfg,
            library_movie_ids=set(),
            library_series_tvdb_ids=set(),
            today=date(2026, 3, 1),
        )
        assert items == []


class TestStoreAndMarkdown:
    def test_upsert_and_library_snapshot(self, tmp_path: Path):
        store = UpcomingStore(tmp_path / "u.db")
        store.upsert_title(_title(tmdb_id=42, title="One"))
        store.upsert_title(_title(tmdb_id=42, title="One Updated", us_digital="2026-07-01"))
        got = store.get_title("movie", 42)
        assert got is not None
        assert got.title == "One Updated"
        assert got.us_digital == "2026-07-01"
        store.replace_library_movies(
            [LibraryMovie(tmdb_id=42, radarr_id=1, title="One", year=2026, has_file=True, path="/m")]
        )
        store.replace_library_series(
            [LibrarySeries(tvdb_id=7, sonarr_id=2, title="S", year=2026, root_folder_path="/TV", series_type="standard", path="/s")]
        )
        assert 42 in store.library_movie_ids()
        assert 7 in store.library_series_tvdb_ids()
        store.close()

    def test_markdown_line_numbers_and_parse(self):
        items = [
            RecommendationItem(
                line_number=1,
                kind="movie",
                tmdb_id=1,
                title="A",
                year=2026,
                score=10,
                payload={"score_breakdown": "pop 20 +50 trend"},
            ),
            RecommendationItem(line_number=2, kind="tv", tmdb_id=2, tvdb_id=5, title="B", year=2026, score=9, payload={}),
        ]
        md = render_markdown(items, slug="upcoming-2026-W12", generated_at="t", sync_at="s")
        assert "| 1 | A |" in md
        assert "10.0 (pop 20 +50 trend)" in md
        assert "| 2 | B |" in md
        assert parse_line_numbers("1,4,7") == {1, 4, 7}
        assert parse_line_numbers("1-3,8") == {1, 2, 3, 8}
        assert parse_line_numbers(None) is None

    def test_build_recommendation_writes_file(self, tmp_path: Path):
        store = UpcomingStore(tmp_path / "u.db")
        store.upsert_title(
            _title(tmdb_id=1, title="Hello", popularity=80, us_theatrical="2026-05-01")
        )
        cfg = _cfg()
        when = datetime(2026, 4, 6, 12, 0, tzinfo=ZoneInfo("UTC"))  # Monday
        slug, path, items, counts = build_recommendation(
            store, cfg, log_dir=tmp_path / "logs", when=when
        )
        assert slug.startswith("upcoming-2026-W")
        assert path.is_file()
        assert counts["movie"] == 1
        assert items[0].line_number == 1
        store.close()

    @pytest.mark.asyncio
    async def test_arr_snapshot_clears_when_unconfigured(self, tmp_path: Path):
        from mose.upcoming import ingest_arr

        store = UpcomingStore(tmp_path / "u.db")
        store.replace_library_movies(
            [LibraryMovie(tmdb_id=1, radarr_id=1, title="M", year=2026, has_file=True, path="/m")]
        )
        store.replace_library_series(
            [LibrarySeries(tvdb_id=2, sonarr_id=2, title="S", year=2026, root_folder_path="/TV", series_type="standard", path="/s")]
        )
        cfg = _cfg()
        cfg.radarr_url = ""
        cfg.sonarr_url = ""
        stats = await ingest_arr(store, cfg)
        assert stats["movies"] == 0
        assert stats["series"] == 0
        assert store.library_movie_ids() == set()
        assert store.library_series_tvdb_ids() == set()
        store.close()

    def test_retention_prune(self, tmp_path: Path):
        store = UpcomingStore(tmp_path / "u.db")
        store.upsert_title(_title(tmdb_id=1, title="Old", us_theatrical="2020-01-01", us_digital="2020-01-01"))
        n = store.prune(catalog_cutoff_date="2024-01-01", recommendation_cutoff_ts=0)
        assert n["titles"] >= 1
        store.close()


class TestArrHelpers:
    def test_compact_and_paginate_large(self):
        movies = [{"tmdbId": i, "id": i, "title": f"M{i}", "year": 2026, "hasFile": False, "path": f"/p/{i}"} for i in range(10050)]
        compact = [compact_movie_row(m) for m in movies]
        page = paginate_index(compact, page=2, page_size=400)
        assert page["total"] == 10050
        assert len(page["items"]) == 400
        assert page["items"][0]["tmdbId"] == 400

    def test_large_library_snapshot_no_truncation(self, tmp_path: Path):
        store = UpcomingStore(tmp_path / "u.db")
        movies = [
            LibraryMovie(tmdb_id=i, radarr_id=i, title=f"M{i}", year=2026, has_file=False, path=f"/p/{i}")
            for i in range(1, 10051)
        ]
        series = [
            LibrarySeries(
                tvdb_id=i,
                sonarr_id=i,
                title=f"S{i}",
                year=2026,
                root_folder_path="/TV",
                series_type="standard",
                path=f"/s/{i}",
            )
            for i in range(1, 10051)
        ]
        store.replace_library_movies(movies)
        store.replace_library_series(series)
        assert len(store.library_movie_ids()) == 10050
        assert len(store.library_series_tvdb_ids()) == 10050
        store.close()

    def test_root_folder_match(self):
        folders = [{"path": "/data/TV"}, {"path": "/data/Anime"}]
        resolved = resolve_arr_targets(
            radarr_folders=[{"path": "/data/Movies"}],
            radarr_profiles=[{"id": 4}],
            sonarr_folders=folders,
            sonarr_profiles=[{"id": 2}],
            sonarr_tv_root_match="/TV",
            sonarr_anime_root_match="/Anime",
        )
        assert resolved["sonarr_tv_root_path"].endswith("TV")
        assert resolved["sonarr_anime_root_path"].endswith("Anime")
        assert resolved["radarr_quality_profile_id"] == 4

    def test_explicit_root_wins(self):
        assert pick_root_folder([{"path": "/a"}], explicit="/custom") == "/custom"

    def test_library_parsers_skip_bad_ids(self):
        assert movie_from_radarr({"title": "x"}) is None
        assert series_from_sonarr({"title": "x"}) is None
        m = movie_from_radarr({"tmdbId": 9, "id": 1, "title": "T", "year": 2026, "hasFile": True})
        assert m is not None and m.tmdb_id == 9


class TestBundledApprove:
    @pytest.mark.asyncio
    async def test_one_approval_adds_without_second_prompt(self, tmp_path: Path):
        store = UpcomingStore(tmp_path / "u.db")
        cfg = _cfg()
        items = [
            RecommendationItem(line_number=1, kind="movie", tmdb_id=1, title="A", year=2026, score=1, payload={}),
            RecommendationItem(line_number=2, kind="tv", tmdb_id=2, tvdb_id=8, title="B", year=2026, score=1, payload={}),
        ]
        called = []

        async def fake_add(item, _cfg, _arr):
            assert scheduled_execution_bypasses_approval("mcp-portal__portal_codemode_execute")
            called.append(item.line_number)
            return "ok"

        results = await execute_upcoming_adds(items, cfg, {}, add_http=fake_add)
        assert all(r["ok"] for r in results)
        assert called == [1, 2]
        store.close()

    @pytest.mark.asyncio
    async def test_handle_decision_subset(self, tmp_path: Path):
        from mose.config import Config

        mem = MemoryManager(MemoryConfig(db_path=str(tmp_path / "m.db"), embedding_dimensions=384))
        store = UpcomingStore(tmp_path / "u.db")
        cfg = Config()
        cfg.upcoming = _cfg()
        items = [
            RecommendationItem(line_number=1, kind="movie", tmdb_id=1, title="A", score=1, payload={}),
            RecommendationItem(line_number=2, kind="movie", tmdb_id=2, title="B", score=1, payload={}),
        ]
        store.save_recommendation_run(slug="upcoming-2026-W12", report_path="x.md", items=items, counts={"movie": 2})
        mem.save_pending_approval(
            slug="upcoming-2026-W12",
            kind=UPCOMING_APPROVAL_KIND,
            recipient="admin",
            proposal_path="x.md",
            payload={"recommendation_run_id": store.get_recommendation_run("upcoming-2026-W12")["id"]},
            expires_at=9e12,
        )
        added = []

        async def fake_add(item, _cfg, _arr):
            added.append(item.line_number)
            return "ok"

        init_upcoming_decision_runtime(memory=mem, store=store, config=cfg, execute_codemode=None)
        from mose import upcoming as up

        orig = up.add_item_http
        up.add_item_http = fake_add  # type: ignore[assignment]
        try:
            ok = await handle_upcoming_decision("upcoming-2026-W12", approved=True, line_spec="2")
        finally:
            up.add_item_http = orig
        assert ok is True
        assert added == [2]
        rec = mem.get_pending_approval("upcoming-2026-W12")
        assert rec is not None and rec.status == "approved"
        digest = format_upcoming_recovery_message(mem)
        # already approved — not in pending digest
        assert "upcoming-2026-W12" not in digest
        store.close()
        mem.close()

    def test_recovery_lists_pending(self, tmp_path: Path):
        mem = MemoryManager(MemoryConfig(db_path=str(tmp_path / "m.db"), embedding_dimensions=384))
        mem.save_pending_approval(
            slug="upcoming-2026-W01",
            kind=UPCOMING_APPROVAL_KIND,
            recipient="gid",
            proposal_path="p",
            payload={"counts": {"movie": 3, "tv": 1, "anime": 0}},
            expires_at=9e12,
        )
        text = format_upcoming_recovery_message(mem, recipient="gid")
        assert "upcoming-2026-W01" in text
        mem.close()


class TestScheduleGates:
    def test_iso_week_slug(self):
        slug = iso_week_slug(datetime(2026, 4, 6, tzinfo=ZoneInfo("UTC")))
        assert slug.startswith("upcoming-2026-W")
        assert canonical_upcoming_slug("upcoming-2026-w12") == "upcoming-2026-W12"
        assert canonical_upcoming_slug("upcoming-2026-W12") == "upcoming-2026-W12"
        from mose.signal_bot import _parse_approval_reply

        assert _parse_approval_reply("approve upcoming-2026-w12") == ("upcoming-2026-W12", "approve")
        assert _parse_approval_reply("approve upcoming-2026-W12") == ("upcoming-2026-W12", "approve")
        assert _parse_approval_reply("approve upcoming-2026-w12 1 4 7")[0] == "upcoming-2026-W12"

    def test_should_run_weekly_once(self, tmp_path: Path):
        store = UpcomingStore(tmp_path / "u.db")
        cfg = _cfg(weekly_weekday="monday", weekly_hour=9)
        monday = datetime(2026, 4, 6, 10, 0, tzinfo=ZoneInfo("UTC"))
        assert should_run_weekly(store, cfg, monday)
        store.save_recommendation_run(slug=iso_week_slug(monday), report_path="x", items=[], counts={})
        assert not should_run_weekly(store, cfg, monday)
        store.close()

    def test_should_run_daily_skips_same_day(self, tmp_path: Path):
        store = UpcomingStore(tmp_path / "u.db")
        cfg = _cfg(daily_hour=5)
        rid = store.begin_sync_run("daily")
        store.finish_sync_run(rid, status="ok")
        now = datetime.now(ZoneInfo("UTC")).replace(hour=10)
        assert should_run_daily(store, cfg, now) is False
        store.close()


class TestPolicyAndConfig:
    def test_index_tools_are_reads(self):
        assert classify_mcp_tool("radarr-diagnostics", "radarr_library_index") == "read"
        assert classify_mcp_tool("sonarr-diagnostics", "sonarr_library_index") == "read"
        assert classify_mcp_tool("radarr-diagnostics", "radarr_get_rootfolder") == "read"
        assert classify_mcp_tool("sonarr-diagnostics", "sonarr_get_qualityprofile") == "read"

    def test_tmdb_and_arr_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        missing = tmp_path / "no.toml"
        monkeypatch.setenv("TMDB_API_KEY", "abc")
        monkeypatch.setenv("RADARR_URL", "http://radarr:7878")
        monkeypatch.setenv("RADARR_API_KEY", "rk")
        monkeypatch.setenv("SONARR_URL", "http://sonarr:8989")
        monkeypatch.setenv("SONARR_API_KEY", "sk")
        monkeypatch.setenv("UPCOMING_ENABLED", "true")
        cfg = load_config(missing)
        assert cfg.upcoming.tmdb_api_key == "abc"
        assert cfg.upcoming.radarr_url.endswith("7878")
        assert cfg.upcoming.sonarr_api_key == "sk"
        assert cfg.upcoming.enabled is True
        assert Path(cfg.upcoming.db_path).is_absolute()
