# Upcoming media catalog

Mose maintains a **local TMDB catalog** of movies, TV, and anime for the current and next calendar year, snapshots Radarr/Sonarr libraries daily, and posts a weekly trending recommendation list (40 movies / 40 TV / 20 anime) to the Signal admin group.

This job is **native Python**, not an LLM scheduled task. Comparison is always against `data/upcoming.db`, not live *arr dumps.

## When to use

- Admin received `upcoming-YYYY-Www.md` and wants to add titles (`approve upcoming-YYYY-Www` or a numbered subset).
- Operator asks what is coming out this year that is **not** in Radarr/Sonarr.
- Debugging why a title was or was not recommended.

## Do not

- Live-dump `radarr_get_movie({})` / `sonarr_get_series({})` to answer “what should we add?”
- Recommend a **new season** of a show already in Sonarr — that show is already managed.
- Put Japanese animation in the TV bucket — anime is its own category (Sonarr anime root folder).

- Route every movie to `Kids Movies` regardless of genre — the weekly job
  buckets by TMDB genres and release year (horror → Scary Movies, Family →
  Kids Movies, otherwise the year/decade folder). Never route automatically
  into `Movies/osr`.

## How it works

1. **Daily sync** (default 05:00 local): TMDB discover + trending, then one `GET /movie` and one `GET /series` via **agent-side HTTP** (`RADARR_*` / `SONARR_*` on the agent process).
2. **Weekly recommend** (default Monday 09:00): SQL compare, rank by popularity + trending bonuses, write `data/logs/upcoming-YYYY-Www.md`, attach it in Signal admin.
3. **Approve** uses a bundled allowlist (no per-title MCP write prompts). Movies → Radarr with per-title bucket routing; TV → Sonarr TV folder; anime → Sonarr anime folder.

Admin replies:

```
approve upcoming-2026-W12
approve upcoming-2026-W12 1,4,7
reject upcoming-2026-W12
```

CLI: `python -m mose --upcoming-sync`, `python -m mose --upcoming-recommend`, `python -m mose --upcoming-attach-test <file.md>`.

## Config

See `[upcoming]` in `config.toml` and `TMDB_API_KEY` / `RADARR_*` / `SONARR_*` env vars. Root folders auto-discover by path match (`/TV` vs `/Anime`; movie buckets via `radarr_kids_root_match` / `radarr_horror_root_match`).

## Movie bucket routing

At add time (Code Mode script and HTTP fallback), each movie resolves its
Radarr `rootFolderPath` from the root folders Radarr reports (snapshotted at
the daily sync into `arr_config.radarr_movie_routing`):

1. TMDB **Horror** (27) → `Scary Movies` (wins Family/Horror ties).
2. TMDB **Family** (10751) → `Kids Movies`.
3. Release year has an exact folder (`/Movies/2010`…`/Movies/2026`) → that folder.
4. Decade folder covers the year (`60s-70s`→1960s-1970s, `80s`→1980s, `90s`→1990s, `00s`→2000s).
5. Otherwise → the main `/Movies` root (catch-all for pre-1960, future years, missing year).

`osr` is never chosen automatically. The weekly report's **Folder** column and
the per-item approval prompt show the resolved bucket before you approve.
Routing is re-resolved live at approval time, so it always reflects the
current Radarr root-folder config.
