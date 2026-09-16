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

## How it works

1. **Daily sync** (default 05:00 local): TMDB discover + trending, then one `GET /movie` and one `GET /series` via **agent-side HTTP** (`RADARR_*` / `SONARR_*` on the agent process).
2. **Weekly recommend** (default Monday 09:00): SQL compare, rank by popularity + trending bonuses, write `data/logs/upcoming-YYYY-Www.md`, attach it in Signal admin.
3. **Approve** uses a bundled allowlist (no per-title MCP write prompts). Movies → Radarr; TV → Sonarr TV folder; anime → Sonarr anime folder.

Admin replies:

```
approve upcoming-2026-W12
approve upcoming-2026-W12 1,4,7
reject upcoming-2026-W12
```

CLI: `python -m mose --upcoming-sync`, `python -m mose --upcoming-recommend`, `python -m mose --upcoming-attach-test <file.md>`.

## Config

See `[upcoming]` in `config.toml` and `TMDB_API_KEY` / `RADARR_*` / `SONARR_*` env vars. Root folders auto-discover by path match (`/TV` vs `/Anime`).
