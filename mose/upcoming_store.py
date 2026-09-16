"""SQLite catalog for upcoming movies/TV/anime and *arr library snapshots."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS catalog_titles (
    media_type TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    tvdb_id INTEGER,
    imdb_id TEXT,
    title TEXT NOT NULL,
    original_title TEXT,
    overview TEXT,
    status TEXT,
    popularity REAL NOT NULL DEFAULT 0,
    vote_count INTEGER NOT NULL DEFAULT 0,
    vote_average REAL NOT NULL DEFAULT 0,
    trending_rank INTEGER,
    original_language TEXT,
    origin_country TEXT,
    genres TEXT,
    adult INTEGER NOT NULL DEFAULT 0,
    us_theatrical TEXT,
    us_digital TEXT,
    us_physical TEXT,
    jp_air_date TEXT,
    first_air_date TEXT,
    release_date TEXT,
    next_season_number INTEGER,
    next_season_air_date TEXT,
    on_trending_week INTEGER NOT NULL DEFAULT 0,
    year INTEGER,
    details_fetched_at REAL,
    updated_at REAL NOT NULL,
    tmdb_updated_at TEXT,
    PRIMARY KEY (media_type, tmdb_id)
);
CREATE INDEX IF NOT EXISTS idx_catalog_kind ON catalog_titles(kind);
CREATE INDEX IF NOT EXISTS idx_catalog_tvdb ON catalog_titles(tvdb_id);

CREATE TABLE IF NOT EXISTS catalog_seasons (
    tmdb_id INTEGER NOT NULL,
    season_number INTEGER NOT NULL,
    air_date TEXT,
    episode_count INTEGER,
    name TEXT,
    PRIMARY KEY (tmdb_id, season_number)
);

CREATE TABLE IF NOT EXISTS library_movies (
    tmdb_id INTEGER PRIMARY KEY,
    radarr_id INTEGER,
    title TEXT,
    year INTEGER,
    has_file INTEGER,
    path TEXT
);

CREATE TABLE IF NOT EXISTS library_series (
    tvdb_id INTEGER PRIMARY KEY,
    sonarr_id INTEGER,
    title TEXT,
    year INTEGER,
    root_folder_path TEXT,
    series_type TEXT,
    path TEXT
);

CREATE TABLE IF NOT EXISTS arr_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,
    error TEXT,
    stats TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_runs_kind ON sync_runs(kind, started_at);

CREATE TABLE IF NOT EXISTS sync_cursors (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendation_runs (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL,
    report_path TEXT,
    status TEXT NOT NULL,
    counts TEXT
);

CREATE TABLE IF NOT EXISTS recommendation_items (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    line_number INTEGER NOT NULL,
    kind TEXT NOT NULL,
    tmdb_id INTEGER NOT NULL,
    tvdb_id INTEGER,
    title TEXT NOT NULL,
    year INTEGER,
    score REAL,
    payload TEXT,
    UNIQUE (run_id, line_number),
    FOREIGN KEY (run_id) REFERENCES recommendation_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rec_items_run ON recommendation_items(run_id);
"""


@dataclass
class CatalogTitle:
    media_type: str
    tmdb_id: int
    kind: str
    title: str
    tvdb_id: int | None = None
    imdb_id: str | None = None
    original_title: str | None = None
    overview: str | None = None
    status: str | None = None
    popularity: float = 0.0
    vote_count: int = 0
    vote_average: float = 0.0
    trending_rank: int | None = None
    original_language: str | None = None
    origin_country: str | None = None
    genres: list[Any] = field(default_factory=list)
    adult: bool = False
    us_theatrical: str | None = None
    us_digital: str | None = None
    us_physical: str | None = None
    jp_air_date: str | None = None
    first_air_date: str | None = None
    release_date: str | None = None
    next_season_number: int | None = None
    next_season_air_date: str | None = None
    on_trending_week: bool = False
    year: int | None = None
    details_fetched_at: float | None = None
    updated_at: float = 0.0
    tmdb_updated_at: str | None = None


@dataclass
class LibraryMovie:
    tmdb_id: int
    radarr_id: int | None
    title: str
    year: int | None
    has_file: bool
    path: str | None


@dataclass
class LibrarySeries:
    tvdb_id: int
    sonarr_id: int | None
    title: str
    year: int | None
    root_folder_path: str | None
    series_type: str | None
    path: str | None


@dataclass
class RecommendationItem:
    line_number: int
    kind: str
    tmdb_id: int
    title: str
    tvdb_id: int | None = None
    year: int | None = None
    score: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)


class UpcomingStore:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path)
        self.db = sqlite3.connect(self.db_path)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA_SQL)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def upsert_title(self, title: CatalogTitle) -> None:
        now = title.updated_at or time.time()
        self.db.execute(
            """
            INSERT INTO catalog_titles (
                media_type, tmdb_id, kind, tvdb_id, imdb_id, title, original_title,
                overview, status, popularity, vote_count, vote_average, trending_rank,
                original_language, origin_country, genres, adult, us_theatrical,
                us_digital, us_physical, jp_air_date, first_air_date, release_date,
                next_season_number, next_season_air_date, on_trending_week, year,
                details_fetched_at, updated_at, tmdb_updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(media_type, tmdb_id) DO UPDATE SET
                kind=excluded.kind,
                tvdb_id=COALESCE(excluded.tvdb_id, catalog_titles.tvdb_id),
                imdb_id=COALESCE(excluded.imdb_id, catalog_titles.imdb_id),
                title=excluded.title,
                original_title=COALESCE(excluded.original_title, catalog_titles.original_title),
                overview=COALESCE(excluded.overview, catalog_titles.overview),
                status=COALESCE(excluded.status, catalog_titles.status),
                popularity=excluded.popularity,
                vote_count=excluded.vote_count,
                vote_average=excluded.vote_average,
                trending_rank=catalog_titles.trending_rank,
                original_language=COALESCE(excluded.original_language, catalog_titles.original_language),
                origin_country=COALESCE(excluded.origin_country, catalog_titles.origin_country),
                genres=excluded.genres,
                adult=excluded.adult,
                us_theatrical=COALESCE(excluded.us_theatrical, catalog_titles.us_theatrical),
                us_digital=COALESCE(excluded.us_digital, catalog_titles.us_digital),
                us_physical=COALESCE(excluded.us_physical, catalog_titles.us_physical),
                jp_air_date=COALESCE(excluded.jp_air_date, catalog_titles.jp_air_date),
                first_air_date=COALESCE(excluded.first_air_date, catalog_titles.first_air_date),
                release_date=COALESCE(excluded.release_date, catalog_titles.release_date),
                next_season_number=COALESCE(excluded.next_season_number, catalog_titles.next_season_number),
                next_season_air_date=COALESCE(excluded.next_season_air_date, catalog_titles.next_season_air_date),
                on_trending_week=catalog_titles.on_trending_week,
                year=COALESCE(excluded.year, catalog_titles.year),
                details_fetched_at=COALESCE(excluded.details_fetched_at, catalog_titles.details_fetched_at),
                updated_at=excluded.updated_at,
                tmdb_updated_at=COALESCE(excluded.tmdb_updated_at, catalog_titles.tmdb_updated_at)
            """,
            (
                title.media_type,
                title.tmdb_id,
                title.kind,
                title.tvdb_id,
                title.imdb_id,
                title.title,
                title.original_title,
                title.overview,
                title.status,
                title.popularity,
                title.vote_count,
                title.vote_average,
                title.trending_rank,
                title.original_language,
                title.origin_country,
                json.dumps(title.genres),
                1 if title.adult else 0,
                title.us_theatrical,
                title.us_digital,
                title.us_physical,
                title.jp_air_date,
                title.first_air_date,
                title.release_date,
                title.next_season_number,
                title.next_season_air_date,
                1 if title.on_trending_week else 0,
                title.year,
                title.details_fetched_at,
                now,
                title.tmdb_updated_at,
            ),
        )
        self.db.commit()

    def get_title(self, media_type: str, tmdb_id: int) -> CatalogTitle | None:
        row = self.db.execute(
            "SELECT * FROM catalog_titles WHERE media_type = ? AND tmdb_id = ?",
            (media_type, tmdb_id),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_title(row)

    def _row_to_title(self, row: tuple) -> CatalogTitle:
        (
            media_type, tmdb_id, kind, tvdb_id, imdb_id, title, original_title,
            overview, status, popularity, vote_count, vote_average, trending_rank,
            original_language, origin_country, genres, adult, us_theatrical,
            us_digital, us_physical, jp_air_date, first_air_date, release_date,
            next_season_number, next_season_air_date, on_trending_week, year,
            details_fetched_at, updated_at, tmdb_updated_at,
        ) = row
        try:
            parsed_genres = json.loads(genres) if genres else []
        except (TypeError, json.JSONDecodeError):
            parsed_genres = []
        return CatalogTitle(
            media_type=media_type,
            tmdb_id=int(tmdb_id),
            kind=kind,
            tvdb_id=int(tvdb_id) if tvdb_id is not None else None,
            imdb_id=imdb_id,
            title=title,
            original_title=original_title,
            overview=overview,
            status=status,
            popularity=float(popularity or 0),
            vote_count=int(vote_count or 0),
            vote_average=float(vote_average or 0),
            trending_rank=int(trending_rank) if trending_rank is not None else None,
            original_language=original_language,
            origin_country=origin_country,
            genres=parsed_genres if isinstance(parsed_genres, list) else [],
            adult=bool(adult),
            us_theatrical=us_theatrical,
            us_digital=us_digital,
            us_physical=us_physical,
            jp_air_date=jp_air_date,
            first_air_date=first_air_date,
            release_date=release_date,
            next_season_number=int(next_season_number) if next_season_number is not None else None,
            next_season_air_date=next_season_air_date,
            on_trending_week=bool(on_trending_week),
            year=int(year) if year is not None else None,
            details_fetched_at=details_fetched_at,
            updated_at=float(updated_at or 0),
            tmdb_updated_at=tmdb_updated_at,
        )

    def list_titles(self, *, kind: str | None = None) -> list[CatalogTitle]:
        if kind:
            rows = self.db.execute(
                "SELECT * FROM catalog_titles WHERE kind = ?",
                (kind,),
            ).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM catalog_titles").fetchall()
        return [self._row_to_title(r) for r in rows]

    def clear_trending_flags(self) -> None:
        self.db.execute("UPDATE catalog_titles SET on_trending_week = 0, trending_rank = NULL")
        self.db.commit()

    def set_trending(self, media_type: str, tmdb_id: int, rank: int) -> None:
        self.db.execute(
            "UPDATE catalog_titles SET on_trending_week = 1, trending_rank = ? "
            "WHERE media_type = ? AND tmdb_id = ?",
            (rank, media_type, tmdb_id),
        )
        self.db.commit()

    def replace_seasons(self, tmdb_id: int, seasons: list[dict[str, Any]]) -> None:
        self.db.execute("DELETE FROM catalog_seasons WHERE tmdb_id = ?", (tmdb_id,))
        for s in seasons:
            self.db.execute(
                "INSERT INTO catalog_seasons (tmdb_id, season_number, air_date, episode_count, name) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    tmdb_id,
                    int(s["season_number"]),
                    s.get("air_date"),
                    s.get("episode_count"),
                    s.get("name"),
                ),
            )
        self.db.commit()

    def list_seasons(self, tmdb_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT season_number, air_date, episode_count, name FROM catalog_seasons "
            "WHERE tmdb_id = ? ORDER BY season_number",
            (tmdb_id,),
        ).fetchall()
        return [
            {
                "season_number": r[0],
                "air_date": r[1],
                "episode_count": r[2],
                "name": r[3],
            }
            for r in rows
        ]

    def replace_library_movies(self, movies: list[LibraryMovie]) -> None:
        self.db.execute("DELETE FROM library_movies")
        self.db.executemany(
            "INSERT INTO library_movies (tmdb_id, radarr_id, title, year, has_file, path) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (m.tmdb_id, m.radarr_id, m.title, m.year, 1 if m.has_file else 0, m.path)
                for m in movies
            ],
        )
        self.db.commit()

    def replace_library_series(self, series: list[LibrarySeries]) -> None:
        self.db.execute("DELETE FROM library_series")
        self.db.executemany(
            "INSERT INTO library_series "
            "(tvdb_id, sonarr_id, title, year, root_folder_path, series_type, path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (s.tvdb_id, s.sonarr_id, s.title, s.year, s.root_folder_path, s.series_type, s.path)
                for s in series
            ],
        )
        self.db.commit()

    def library_movie_ids(self) -> set[int]:
        rows = self.db.execute("SELECT tmdb_id FROM library_movies").fetchall()
        return {int(r[0]) for r in rows if r[0] is not None}

    def library_series_tvdb_ids(self) -> set[int]:
        rows = self.db.execute("SELECT tvdb_id FROM library_series").fetchall()
        return {int(r[0]) for r in rows if r[0] is not None}

    def set_arr_config(self, data: dict[str, Any]) -> None:
        self.db.execute("DELETE FROM arr_config")
        for k, v in data.items():
            self.db.execute(
                "INSERT INTO arr_config (key, value) VALUES (?, ?)",
                (k, json.dumps(v)),
            )
        self.db.commit()

    def get_arr_config(self) -> dict[str, Any]:
        rows = self.db.execute("SELECT key, value FROM arr_config").fetchall()
        out: dict[str, Any] = {}
        for k, v in rows:
            try:
                out[k] = json.loads(v)
            except (TypeError, json.JSONDecodeError):
                out[k] = v
        return out

    def begin_sync_run(self, kind: str) -> int:
        cur = self.db.execute(
            "INSERT INTO sync_runs (kind, started_at, status) VALUES (?, ?, 'running')",
            (kind, time.time()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def finish_sync_run(
        self,
        run_id: int,
        *,
        status: str,
        error: str | None = None,
        stats: dict[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE sync_runs SET finished_at = ?, status = ?, error = ?, stats = ? WHERE id = ?",
            (time.time(), status, error, json.dumps(stats or {}), run_id),
        )
        self.db.commit()

    def last_successful_sync(self, kind: str) -> float | None:
        row = self.db.execute(
            "SELECT finished_at FROM sync_runs WHERE kind = ? AND status = 'ok' "
            "ORDER BY finished_at DESC LIMIT 1",
            (kind,),
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def consecutive_failures(self, kind: str) -> int:
        rows = self.db.execute(
            "SELECT status FROM sync_runs WHERE kind = ? AND status != 'running' "
            "ORDER BY started_at DESC LIMIT 20",
            (kind,),
        ).fetchall()
        n = 0
        for (status,) in rows:
            if status == "failed":
                n += 1
            else:
                break
        return n

    def last_sync_error(self, kind: str) -> tuple[int | None, str | None]:
        row = self.db.execute(
            "SELECT id, error FROM sync_runs WHERE kind = ? AND status = 'failed' "
            "ORDER BY started_at DESC LIMIT 1",
            (kind,),
        ).fetchone()
        if row is None:
            return None, None
        return int(row[0]), row[1]

    def get_cursor(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM sync_cursors WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def set_cursor(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO sync_cursors (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.db.commit()

    def clear_cursor(self, key: str) -> None:
        self.db.execute("DELETE FROM sync_cursors WHERE key = ?", (key,))
        self.db.commit()

    def save_recommendation_run(
        self,
        *,
        slug: str,
        report_path: str,
        items: list[RecommendationItem],
        counts: dict[str, int],
    ) -> int:
        self.db.execute("DELETE FROM recommendation_runs WHERE slug = ?", (slug,))
        cur = self.db.execute(
            "INSERT INTO recommendation_runs (slug, created_at, report_path, status, counts) "
            "VALUES (?, ?, ?, 'proposed', ?)",
            (slug, time.time(), report_path, json.dumps(counts)),
        )
        run_id = int(cur.lastrowid)
        self.db.executemany(
            "INSERT INTO recommendation_items "
            "(run_id, line_number, kind, tmdb_id, tvdb_id, title, year, score, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    it.line_number,
                    it.kind,
                    it.tmdb_id,
                    it.tvdb_id,
                    it.title,
                    it.year,
                    it.score,
                    json.dumps(it.payload),
                )
                for it in items
            ],
        )
        self.db.commit()
        return run_id

    def get_recommendation_run(self, slug: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT id, slug, created_at, report_path, status, counts FROM recommendation_runs WHERE slug = ?",
            (slug,),
        ).fetchone()
        if row is None:
            return None
        try:
            counts = json.loads(row[5]) if row[5] else {}
        except (TypeError, json.JSONDecodeError):
            counts = {}
        return {
            "id": row[0],
            "slug": row[1],
            "created_at": row[2],
            "report_path": row[3],
            "status": row[4],
            "counts": counts,
        }

    def list_recommendation_items(self, run_id: int) -> list[RecommendationItem]:
        rows = self.db.execute(
            "SELECT line_number, kind, tmdb_id, tvdb_id, title, year, score, payload "
            "FROM recommendation_items WHERE run_id = ? ORDER BY line_number",
            (run_id,),
        ).fetchall()
        items: list[RecommendationItem] = []
        for r in rows:
            try:
                payload = json.loads(r[7]) if r[7] else {}
            except (TypeError, json.JSONDecodeError):
                payload = {}
            items.append(
                RecommendationItem(
                    line_number=int(r[0]),
                    kind=r[1],
                    tmdb_id=int(r[2]),
                    tvdb_id=int(r[3]) if r[3] is not None else None,
                    title=r[4],
                    year=int(r[5]) if r[5] is not None else None,
                    score=float(r[6] or 0),
                    payload=payload if isinstance(payload, dict) else {},
                )
            )
        return items

    def set_recommendation_status(self, slug: str, status: str) -> None:
        self.db.execute(
            "UPDATE recommendation_runs SET status = ? WHERE slug = ?",
            (status, slug),
        )
        self.db.commit()

    def prune(self, *, catalog_cutoff_date: str, recommendation_cutoff_ts: float) -> dict[str, int]:
        """Drop old catalog rows and recommendation runs."""
        cur_t = self.db.execute(
            """
            DELETE FROM catalog_titles WHERE
              COALESCE(us_digital, us_theatrical, next_season_air_date, first_air_date, jp_air_date, release_date, '')
              != ''
              AND COALESCE(us_digital, us_theatrical, next_season_air_date, first_air_date, jp_air_date, release_date)
                  < ?
            """,
            (catalog_cutoff_date,),
        )
        titles = cur_t.rowcount
        cur_s = self.db.execute(
            "DELETE FROM catalog_seasons WHERE tmdb_id NOT IN "
            "(SELECT tmdb_id FROM catalog_titles WHERE media_type = 'tv')"
        )
        seasons = cur_s.rowcount
        old_runs = self.db.execute(
            "SELECT id FROM recommendation_runs WHERE created_at < ?",
            (recommendation_cutoff_ts,),
        ).fetchall()
        ids = [r[0] for r in old_runs]
        items = 0
        if ids:
            q = ",".join("?" * len(ids))
            cur_i = self.db.execute(
                f"DELETE FROM recommendation_items WHERE run_id IN ({q})",
                ids,
            )
            items = cur_i.rowcount
            self.db.execute(
                f"DELETE FROM recommendation_runs WHERE id IN ({q})",
                ids,
            )
        self.db.commit()
        return {"titles": titles, "seasons": seasons, "recommendation_items": items, "recommendation_runs": len(ids)}
