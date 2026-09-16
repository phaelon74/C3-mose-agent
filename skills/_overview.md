# Cloud3 SRE Role

Mose runs in a **sandbox** (agent container or restricted host). Integrated backends (Plex, Sonarr, Radarr, NZBGet, paper_db) live on **other systems** with credentials only inside MCP sidecars — not in the agent shell.

## How to reach systems

| Class | How |
|-------|-----|
| **Integrated backends** (Plex, Sonarr, Radarr, NZBGet, paper_db) | **Code Mode only** — `mcp-portal__portal_codemode_search` then `mcp-portal__portal_codemode_execute` |
| **This host** (service status, journal, local `docker ps`/`logs`, workspace files) | Allowlisted **`bash`** |
| **State changes on this host** (restart, apt, ufw, destructive) | **`sre_execute`** (human approval) |
| **Not integrated** (OPNsense, TrueNAS, Proxmox API, DrivePool WinRM) | **No API access from the sandbox** — do not `curl` or embed `$API_KEY` in bash; ask the operator or use `sre_execute` on an approved jump host |

## Code Mode workflow (every integrated backend)

1. **`mcp-portal__portal_codemode_search`** — `query` describes what you need (e.g. `"plex active sessions"`, `"sonarr queue"`).
2. **`mcp-portal__portal_codemode_execute`** — TypeScript calling `mcp.<server_key>.<tool>(args)` and **`console.log(JSON.stringify(...))`** the facts you report.
3. Read stdout only. If empty, log the raw object and fix field paths — do not invent data.
4. Mutating MCP tools require **admin approval** (Signal/Discord) per action — unless running via an **approved playbook** or **scheduled task** allowlist (`playbook_run_propose` / scheduled task). See `playbook-delete-search`.

### MCP server keys (compose name → TypeScript)

| Sidecar | `mcp.` key |
|---------|------------|
| plex-ops-admin | `plex_ops_admin` |
| plex-stack-automation | `plex_stack_automation` |
| sonarr-diagnostics | `sonarr_diagnostics` |
| radarr-diagnostics | `radarr_diagnostics` |
| nzbget-diagnostics | `nzbget_diagnostics` |
| paper_db | `paper_db` |

**Never** use `bash`, `curl`, `wget`, or `sre_execute` to call Plex / Sonarr / Radarr / NZBGet / paper_db HTTP APIs. Those commands are blocked and will fail without credentials.

## Local host troubleshooting (when relevant)

For “is the daemon up on **this** machine?” only:

- `systemctl status <unit>` — allowlisted bash
- `journalctl -u <unit> -n 50 --no-pager`
- `docker ps` / `docker logs <mose-sidecar>` — see `docker.md`

That is **not** a substitute for Plex/Sonarr/Radarr/NZBGet **application** status — use Code Mode for API truth.

## General approach

1. Code Mode (or local bash for host-only checks)
2. Logs and resources on the host you can see
3. `sre_execute` only when something must change and approval is acceptable

## Credentials

API keys exist only in MCP sidecar environments. Do not reference `$PLEX_TOKEN`, `$SONARR_API_KEY`, etc. in bash — the sandbox does not have them.

## Queue purge skills

| Skill | Use |
|-------|-----|
| **purge-queue-samples** | List and remove Sonarr/Radarr queue rows that are sample-only downloads (Code Mode; `load_skill` for full runbook). |
| **purge-queue-empty** | List and remove Sonarr/Radarr queue rows with zero/empty downloads (Code Mode; `load_skill` for full runbook). |
| **sonarr-replace-episodes** | Delete wrong episode files and trigger `EpisodeSearch` for specific episodes (wrong language, bad quality). |
| **upcoming-media** | Weekly TMDB catalog vs local Radarr/Sonarr snapshots; approve the Markdown list to add titles. |
