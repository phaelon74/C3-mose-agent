# Sonarr — Replace Episodes (delete-then-search)

Use when specific episodes have the **wrong audio language**, bad quality, samples, or otherwise need a fresh grab.

**Preferred:** invoke playbook `delete-search` via `playbook_run_propose` (one bundled admin approval). See `load_skill playbook-delete-search`.

Reach Sonarr **only** through Code Mode (`mcp.sonarr_diagnostics.*`). See `load_skill sonarr` for library lookup and audio-language checks.

## When to use

- User confirms episodes are wrong (e.g. French instead of English) and wants re-downloads
- Replace specific episode numbers without re-adding the series

## Procedure

### 1. Resolve series in library

```ts
const lookup = await mcp.sonarr_diagnostics.sonarr_get_series_lookup({ term: "Dutton Ranch" });
const tvdbId = lookup[0]?.tvdbId;
const lib = await mcp.sonarr_diagnostics.sonarr_get_series({ tvdbId });
if (!Array.isArray(lib) || lib.length === 0) {
  console.log("NOT_IN_LIBRARY", tvdbId);
} else {
  console.log("SERIES", lib[0].id, lib[0].title, lib[0].path);
}
const seriesId = lib[0]?.id;
const files = await mcp.sonarr_diagnostics.sonarr_get_episode_files({ seriesId });
console.log("EPISODE_FILES", files.length);
```

**Never** conclude "not in library" from lookup alone — always call `sonarr_get_series({ tvdbId })`.
**Never** report zero files from lookup `statistics` — verify with `sonarr_get_episode_files` or `sonarr_get_episode` `hasFile`.

### 2. Get episode row IDs for target episodes

```ts
const SERIES_ID = 123; // from step 1
const TARGET_EPS = [4, 5];
const episodes = await mcp.sonarr_diagnostics.sonarr_get_episode({
  seriesId: SERIES_ID,
  seasonNumber: 1,
});
const targets = episodes.filter(e => TARGET_EPS.includes(e.episodeNumber));
console.log(JSON.stringify(targets.map(e => ({
  id: e.id,
  episodeNumber: e.episodeNumber,
  episodeFileId: e.episodeFileId,
  hasFile: e.hasFile,
})), null, 2));
```

Use Sonarr episode row `id` values for `sonarr_post_command_episode_search` — **not** `episodeNumber`.

### 3. Optional — confirm audio language before delete

```ts
const files = await mcp.sonarr_diagnostics.sonarr_get_episode_files({ seriesId: SERIES_ID });
const bad = files.filter(f => TARGET_EPS.some(n => f.episodeNumbers?.includes(n)));
console.log(JSON.stringify(bad.map(f => ({
  episode: f.episodeNumbers,
  fileId: f.id,
  audioLanguages: f.mediaInfo?.audioLanguages,
  path: f.path,
})), null, 2));
```

### 4. Delete episode files (approval required)

```ts
// One call per file id from sonarr_get_episode_files (field `id`, not episodeFileId on episode row)
// await mcp.sonarr_diagnostics.sonarr_delete_episodefile({ id: FILE_ID });
```

### 5. Trigger episode search (approval required)

```ts
// const r = await mcp.sonarr_diagnostics.sonarr_post_command_episode_search({
//   episodeIds: [EP_ROW_ID_1, EP_ROW_ID_2],
// });
// console.log(JSON.stringify(r, null, 2));
```

### 6. Confirm queue

```ts
const queue = await mcp.sonarr_diagnostics.sonarr_get_queue({});
console.log(JSON.stringify(queue, null, 2));
```

## Caveats

- Deleting the episode file removes the on-disk media; Plex will show the episode as unavailable until a new file imports.
- `EpisodeSearch` targets **specific episode row ids** only — a parameterless search-all-missing is intentionally not exposed.
- If search returns nothing, check indexers, quality profile, and language filters in Sonarr — that is outside this runbook.

## Related skills

- **sonarr** — library lookup, audio language, decision tree
- **purge-queue-samples** / **purge-queue-empty** — clean bad queue rows after failed grabs
