# Playbook: delete-search (Sonarr episode replace)

On-demand playbook for deleting wrong Sonarr episode files and triggering `EpisodeSearch` for specific episodes. Prefer this over calling mutating tools directly in chat — one admin approval bundles all deletes + search.

See also: `sonarr-replace-episodes`, `sonarr`, `_overview`.

## Playbook slug

`delete-search`

## When to invoke

User says e.g. "Run playbook delete-search for Landman season 1 episodes 4 and 5".

## Invoke flow (agent)

1. Read-only preflight in normal chat (no mutating tools):
   - `sonarr_get_series_lookup`, `sonarr_get_series`, `sonarr_get_episode`, `sonarr_get_episode_files`
   - Build `preflight_summary`: series id, episode row ids, file ids, `audioLanguages`, `hasFile`
2. If not in library or no files → reply to user; do not propose run.
3. `playbook_run_propose` with `invocation_params` (`series`, `season`, `episodes`), filled `user_prompt`, and `preflight_summary`.
4. Admin approves once → authorized run executes deletes + search without per-action prompts.

## Playbook definition (`playbook_propose`)

Use when `delete-search` is not yet in `playbook_list`.

**description:** Delete wrong Sonarr episode files and trigger episode search for replacements.

**user_prompt_template:** Resolve series by name, target season/episodes from invocation_params; confirm preflight; delete episode files; batch episode search; show queue.

**execution_plan.procedure:**

1. Resolve series in library (`sonarr_get_series_lookup` + `sonarr_get_series` by tvdbId).
2. Get episode row ids for target season/episodes (`sonarr_get_episode`).
3. Confirm files on disk (`sonarr_get_episode_files`); use file `id` for deletes.
4. Delete each target episode file (`sonarr_delete_episodefile`).
5. Trigger episode search for episode row ids (`sonarr_post_command_episode_search`).
6. Report queue (`sonarr_get_queue`).

**execution_plan.allowed_tools:**

- `mcp-portal__portal_codemode_search`
- `mcp-portal__portal_codemode_execute`
- `sonarr-diagnostics__sonarr_get_series_lookup`
- `sonarr-diagnostics__sonarr_get_series`
- `sonarr-diagnostics__sonarr_get_episode`
- `sonarr-diagnostics__sonarr_get_episode_files`
- `sonarr-diagnostics__sonarr_delete_episodefile`
- `sonarr-diagnostics__sonarr_post_command_episode_search`
- `sonarr-diagnostics__sonarr_get_queue`

## Example invocation_params

```json
{"series": "Landman", "season": 1, "episodes": [4, 5]}
```

## Example user_prompt (resolved at invoke time)

```
Playbook delete-search for Landman season 1 episodes 4 and 5.
Series id: 123. Episode row ids: [1204, 1205]. File ids: [48291, 48292].
Preflight audio: E04=French, E05=French (expected English).
Delete both files, run episode search for both episode row ids, then show the queue.
```
