# Installation Guide

Complete setup instructions for the Mose SRE/DevOps agent on a **Linux Docker
host**. Windows hosts are intentionally not supported — the agent is developed
and deployed on Linux only.

Two deployment shapes are supported:

| Shape | When to pick it |
|---|---|
| **Docker Compose (recommended)** | Production. The agent runs in a container, shell tools `docker exec` into an isolated sandbox container, a non-root user is enforced, and the host Docker socket is the only privileged mount. |
| **Bare metal + systemd** | Homelab on the same box that hosts vLLM / TabbyAPI. The agent runs as a dedicated non-root `mose` user under systemd. |

Both shapes rely on the same source tree and the same `config.toml`. Pick the
shape you want, follow its section, then come back to the shared sections at
the bottom (memory/MCP/Signal/skill review).

---

## Prerequisites (common)

| Requirement | Minimum |
|---|---|
| OS | Ubuntu 22.04+, Debian 12+, or equivalent systemd-based Linux |
| Python | 3.11+ (for bare-metal install) |
| Docker Engine | 24+ (for Docker Compose install) |
| An OpenAI-compatible LLM | TabbyAPI, vLLM, or llama.cpp server reachable from the host |
| RAM | 32 GB+ recommended |
| Disk | 30 GB free (model weights + venv + runtime data) |

For GPU-accelerated local LLM serving (e.g. vLLM in the `worker-agent.service`
unit), you additionally need recent NVIDIA drivers and a matching CUDA
toolkit. Verify your stack before proceeding:

```bash
nvidia-smi
nvcc --version
```

---

## A. Docker Compose deployment (recommended)

### A.1 Create the operator user

The whole deployment — build, run, and `docker compose` calls — should happen
as a **non-root** user that is a member of the host's `docker` group. This
user does not need sudo to run the agent.

```bash
sudo useradd -m -s /bin/bash mose
sudo usermod -aG docker mose
sudo -u mose -i            # switch to the mose user for the rest of this section
```

### A.2 Clone the repository

```bash
cd ~
git clone https://github.com/phaelon74/C3-mose-agent.git mose-agent
cd mose-agent
```

### A.3 Configure environment variables

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set the interface(s) you want. For a Signal-first SRE
deployment, set:

```bash
# LLM (TabbyAPI, vLLM, llama.cpp server — any OpenAI-compatible API)
LLM_ENDPOINT=http://host.docker.internal:5000/v1
LLM_MODEL=your-served-model-name
LLM_MAX_TOKENS=16384
LLM_TEMPERATURE=1.0
LLM_OMIT_TEMPERATURE=false   # true = never send temperature (engine default)
LLM_CONTEXT_WINDOW=98304
LLM_API_KEY=your-tabby-or-vllm-bearer-token   # empty string allowed for local vLLM
LLM_PROVIDER=openai_compat   # openai_compat | tabby | vllm | bedrock

# Signal (see Section E): linked account + two group base64 ids from listGroups
SIGNAL_PHONE=+15551234567
SIGNAL_ENGAGEMENT_GROUP_ID=<base64-from-listGroups>
SIGNAL_ADMIN_GROUP_ID=<base64-from-listGroups>

# Or: Discord, if you prefer that interface (no skill-proposal UX in Discord)
DISCORD_TOKEN=your-discord-bot-token
```

`host.docker.internal` resolves to the host inside the container thanks to the
`extra_hosts` entry in `docker-compose.yml`. If your LLM server is on a
separate machine, set `LLM_ENDPOINT` to that IP/URL directly.

### A.4 Build the image with the correct `docker` GID

The agent runs inside the container as a non-root user (`mose`, uid 1000).
For it to use the host Docker socket (required for `TERMINAL_BACKEND=docker`)
the in-container `docker` group must match the host's `docker` group id:

```bash
export DOCKER_GID=$(getent group docker | cut -d: -f3)
docker compose build --build-arg DOCKER_GID=$DOCKER_GID
```

**Persist it so you do not have to export every session.** Pick one approach
(both work with `docker compose build`; Compose substitutes `${DOCKER_GID}`
from the environment or from the project `.env` file).

**Option 1 — project `.env` (recommended):** Docker Compose automatically
reads a file named `.env` in the same directory as `docker-compose.yml` for
variable interpolation. You already created `.env` in A.3; append the numeric
GID once (replace nothing — the command fills in the current value):

```bash
echo "DOCKER_GID=$(getent group docker | cut -d: -f3)" >> .env
```

Verify the line looks like `DOCKER_GID=991` (your number will differ). After
that, plain `docker compose build` passes the correct build arg. If the host
`docker` group is ever recreated with a new GID, update this line and rebuild.

**Option 2 — shell profile:** If you prefer an environment variable in every
interactive shell, append one line to your shell’s rc file, then open a new
shell or reload the file (`source ~/.bashrc`, `exec fish`, etc.).

Bash (`~/.bashrc` or `~/.profile`) or Zsh (`~/.zshrc`):

```bash
grep -q 'DOCKER_GID' ~/.bashrc || echo 'export DOCKER_GID=$(getent group docker | cut -d: -f3)' >> ~/.bashrc
```

Adjust the path if you use Zsh (`~/.zshrc`) instead of Bash.

Fish (`~/.config/fish/config.fish`): add this line (Fish uses parentheses for
command substitution, not `$()`):

```fish
set -gx DOCKER_GID (getent group docker | cut -d: -f3)
```

Append it once, for example:

```bash
mkdir -p ~/.config/fish
grep -q 'set -gx DOCKER_GID' ~/.config/fish/config.fish 2>/dev/null || \
  echo 'set -gx DOCKER_GID (getent group docker | cut -d: -f3)' >> ~/.config/fish/config.fish
```

**Not sufficient:** A random `.env` somewhere on disk is ignored unless you
`export` its variables yourself or place the values in the **repository**
`.env` as in option 1. `docker compose` does not read `~/.env` by default.

### A.5 Launch the sandbox and the agent

```bash
docker compose build mose-sandbox mose-agent
docker compose up -d mose-sandbox mose-agent
docker compose logs -f mose-agent
```

`mose-agent` waits until `mose-sandbox` passes its healthcheck (`/workspace`
exists) before starting, so a cold boot does not race into failed `docker exec`
calls.

On the first run, the agent creates its SQLite memory database under the
`mose-data` named volume and starts either the Signal or Discord bot
depending on what you set in `.env`.

#### A.5.1 Rebuild the shell sandbox after Dockerfile changes

The sandbox image is built from [`docker/sandbox/Dockerfile`](docker/sandbox/Dockerfile)
(SRE/network tooling is installed at **image** build time; the container runs
with `read_only: true`, so runtime `apt-get` is not available).

```bash
docker compose build mose-sandbox
docker compose up -d --force-recreate mose-sandbox mose-agent
```

#### A.5.2 Workspace path mapping (agent vs sandbox)

The same named volume `mose-workspace` is mounted at:

- **`/app/data/workspace`** inside `mose-agent` (file tools and the resolved
  workspace path on the agent)
- **`/workspace`** inside `mose-sandbox` (where `bash` / `sre_execute` run)

The agent maps agent-side paths under the workspace to `/workspace/...` inside
the sandbox automatically. You should set `[terminal] workspace_mount =
"/workspace"` in `config.toml` to match compose (this is the default).

#### A.5.3 Reaching LAN segments (e.g. `10.4.251.0/24`)

**Default (bridge + SNAT):** `mose-sandbox` stays on the compose bridge
(`mose-net`, typically `172.18.0.0/16`). Outbound traffic to hosts on your LAN
is forwarded and masqueraded by the Docker host. If the host has a route to
that subnet (for example the host is `10.4.251.x/24` on `enp39s0`), the sandbox
can reach every node on `10.4.251.0/24`; targets see the **host’s** IP as the
source (e.g. `10.4.251.206`), not an address inside the bridge CIDR.

Host sanity checks if something does not respond:

```bash
sysctl net.ipv4.ip_forward          # should be 1
sudo iptables -t nat -S POSTROUTING | grep MASQUERADE
ip route get 10.4.251.254           # should use the host interface on that subnet
```

Quick checks **from the host** after the stack is up:

```bash
docker compose exec mose-sandbox ping -c 2 10.4.251.254
docker compose exec mose-sandbox curl -sS -m 5 -o /dev/null -w '%{http_code}\n' http://10.4.251.254/ || true
docker compose exec mose-sandbox ss -ltn
```

**Optional (macvlan):** If managed nodes must see a **dedicated** source IP for
the sandbox (firewall ACLs, inbound SSH to the sandbox, etc.), attach
`mose-sandbox` to a `macvlan` network on your LAN parent interface and assign
an IP from an unused slice of `10.4.251.0/24`. Caveats: reserve the range in
DHCP/IPAM; the host usually cannot talk to its own macvlan child without an
extra shim interface; Wi-Fi parents often do not support macvlan.

**Last resort:** `network_mode: host` on the sandbox removes network isolation;
only use if bridge + SNAT and macvlan are both unsuitable.

**Capabilities:** Compose adds `cap_add: [NET_RAW]` so `ping` and `traceroute`
work reliably with `cap_drop: ALL`. For `tcpdump` or similar, consider a
separate compose profile that adds `NET_ADMIN` (wider privilege).

### A.6 Verify the isolation

```bash
# Agent runs as the mose user (uid 1000)
docker compose exec mose-agent id
#   uid=1000(mose) gid=1000(mose) groups=1000(mose),<docker-gid>(docker)

# Shell tools execute inside the sandbox container, NOT the agent container
docker compose exec mose-agent python -c "from mose.terminal import get_backend; print(type(get_backend()).__name__)"
#   DockerTerminalBackend
```

### A.7 Periodic skill review (Docker)

See Section F for the scheduling options — the Docker Compose file exposes a
`mose-skill-review` one-shot service that you invoke from a systemd timer on
the host:

```bash
docker compose --profile review run --rm mose-skill-review
```

---

## B. Bare-metal + systemd deployment

Use this shape on the same host that runs your LLM server when you prefer a
fully native install.

### B.1 Create the service user

```bash
sudo useradd -m -s /bin/bash mose
sudo usermod -aG video,render mose     # GPU access for local LLM serving
```

No sudo rights are granted. The SRE agent reads whatever its user account can
read; any state-changing action must go through `sre_execute`, which requires
human approval.

### B.2 Clone the repository

```bash
sudo -u mose -i
cd ~
git clone https://github.com/phaelon74/C3-mose-agent.git mose-agent
cd mose-agent
```

### B.3 Create the Python virtualenv

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

### B.4 Generate a reproducible lockfile with `uv` (strongly recommended)

`uv` produces a `uv.lock` that pins every transitive dependency. Install it
once, then lock the repo:

```bash
pip install uv
uv lock                 # generates uv.lock from pyproject.toml
uv sync                 # installs exactly what the lockfile pins
```

Commit `uv.lock` to the repo so every host installs the same versions.
Refresh the lockfile whenever you change `pyproject.toml`:

```bash
uv lock --upgrade       # bump deps within the declared version ranges
```

### B.5 Install vLLM (optional, only if you run it on this host)

```bash
# CUDA 13 wheels — adjust to match your driver / CUDA toolkit
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu130
pip install vllm
```

Model weights (`QuantTrio/Qwen3.5-27B-AWQ`) are downloaded automatically on
first launch.

### B.6 Create runtime directories

```bash
mkdir -p data/{logs,workspace,tool_outputs}
```

### B.7 Configure the agent

```bash
cp .env.example .env
chmod 600 .env
```

Fill in the `LLM_*` variables (at minimum `LLM_ENDPOINT` and `LLM_MODEL`),
`LLM_API_KEY` if applicable, `SIGNAL_PHONE`, `SIGNAL_ENGAGEMENT_GROUP_ID`,
`SIGNAL_ADMIN_GROUP_ID`, and any other
interface tokens you need. Adjust `config.toml` for non-LLM options if the
defaults don't match your hardware.

### B.8 Install systemd units

```bash
sudo cp worker-agent.service        /etc/systemd/system/
sudo cp mose-agent.service          /etc/systemd/system/
sudo cp mose-skill-review.service   /etc/systemd/system/
sudo cp mose-skill-review.timer     /etc/systemd/system/
sudo cp signal-cli-daemon.service   /etc/systemd/system/   # optional, Signal only

sudo systemctl daemon-reload
sudo systemctl enable --now worker-agent
sudo systemctl enable --now mose-agent
sudo systemctl enable --now mose-skill-review.timer
```

Check status and logs:

```bash
systemctl status mose-agent
journalctl -u mose-agent -f
systemctl list-timers 'mose-*'
journalctl -u mose-skill-review -n 200
```

---

## C. Configuration reference

LLM-related settings are set via **environment variables** (`LLM_*` in `.env`;
see section A.3). Other values live in `config.toml`. The most relevant
non-LLM sections for an SRE deployment:

```toml
[terminal]
# "local" runs bash on the same machine as the agent.
# "docker" runs bash inside mose-sandbox via docker exec (recommended).
backend = "docker"
container = "mose-sandbox"
workspace_mount = "/workspace"   # must match docker-compose volume mount

[signal]
# Group ids are normally set via SIGNAL_ENGAGEMENT_GROUP_ID / SIGNAL_ADMIN_GROUP_ID in .env
daemon_host = "127.0.0.1"
daemon_port = 7583
proposal_timeout_seconds = 43200   # 12 hours

[learning]
# Skill proposals are written to skills/pending/ and require explicit human
# approval (via Signal) before the skill body is generated. Nothing is ever
# auto-built or auto-deleted.
enabled = true
min_tools_used = 3
skill_loading_mode = "full"              # full | level_0

# Periodic skill-quality review
skill_review_failure_threshold = 0.3     # flag skills failing >= 30% of runs
review_interval_hours = 168              # weekly
review_min_samples = 5                   # require >= 5 uses before flagging
review_log_dir = "data/logs"

# Startup grace window for approved-but-unbuilt skills (crashed mid-draft).
# The admin sees a warning and the build auto-proceeds after this delay
# unless they reply "stop <slug>" / "cancel <slug>".
build_grace_window_seconds = 900         # 15 minutes
```

Environment variables that override the file:
`DISCORD_TOKEN`, `SIGNAL_PHONE`, `SIGNAL_ENGAGEMENT_GROUP_ID`, `SIGNAL_ADMIN_GROUP_ID`,
`LLM_ENDPOINT`, `LLM_MODEL`, `LLM_MAX_TOKENS`, `LLM_TEMPERATURE`,
`LLM_OMIT_TEMPERATURE`, `LLM_CONTEXT_WINDOW`, `LLM_API_KEY`, `LLM_PROVIDER`,
`MEMORY_DB_PATH`, `LOG_DIR`, `TERMINAL_BACKEND`.

---

## D. MCP servers — Plex / Sonarr / Radarr (optional)

MCP servers expose extra tools to the agent. The LLM calls them only through
native tools `list_available_tools` and `use_tool` (see `mose/agent.py`).

### D.0 Registry file

```bash
cp mcp_servers.example.json mcp_servers.json
```

For no MCP servers at all, an empty config is fine:

```json
{ "servers": {} }
```

If you use the Plex sidecars below, keep `mcp_servers.json` **free of secrets**:
URLs and tokens live in the MCP container environment (`.env` → `docker-compose.yml`),
and each image runs `/usr/local/bin/mcp-entrypoint`, which reads those variables.

### D.1 Gather Plex credentials (`PLEX_URL`, `PLEX_TOKEN`)

1. **`PLEX_URL`** — Full base URL including scheme, host, and port, for example
   `http://10.4.251.10:32400`. No trailing path. Plex, Sonarr, and Radarr may each
   live on **different IPs and ports**; set each URL independently.
2. **`PLEX_TOKEN`**
   - **Primary:** Plex Web → **Settings → Account** → enable **Show Advanced** →
     copy **Plex Token**.
   - **Alternate:** Open
     `http://YOUR_PLEX_IP:32400/web/index.html#!/settings/account` while signed in,
     or inspect a Plex Web request for the `X-Plex-Token` query parameter.
3. **Permissions:** The token acts as that Plex user against the server (not a
   fine-scoped OAuth scope). Prefer a **dedicated** Plex account for automation and
   restrict library access under **Users & Sharing**. Keep the token in MCP env
   only; rotate via **Sign out of all devices** if leaked.
4. **Reachability check** (from the host, after `plex-ops-admin` is up; ensure
   `PLEX_URL` / `PLEX_TOKEN` are exported in your shell or rely on values inside the container):

```bash
docker compose exec plex-ops-admin sh -lc \
  'curl -sS -m 5 "${PLEX_URL}/identity?X-Plex-Token=${PLEX_TOKEN}" | head -c 200'
```

### D.2 Gather Sonarr credentials (`SONARR_URL`, `SONARR_API_KEY`)

1. **`SONARR_URL`** — e.g. `http://10.4.251.11:8989`. Do **not** append `/api/v3`;
   the MCP client adds API paths. Sonarr uses **API v3**.
2. **`SONARR_API_KEY`** — Sonarr Web UI → **Settings → General** → **Security** →
   **API Key** (enable **Show Advanced** if needed). Regenerate if unknown.
3. **Permissions:** Sonarr exposes one **full-access** API key; read and write API
   routes share it. Network isolation plus Mose’s `use_tool` approval policy (section D.6)
   limit damage.
4. **Check:**

```bash
docker compose exec plex-stack-automation sh -lc \
  'curl -sS -m 10 -H "X-Api-Key: ${SONARR_API_KEY}" "${SONARR_URL}/api/v3/system/status"'
```

### D.3 Gather Radarr credentials (`RADARR_URL`, `RADARR_API_KEY`)

Same pattern as Sonarr:

1. **`RADARR_URL`** — e.g. `http://10.4.251.12:7878` (no `/api/v3` suffix).
2. **`RADARR_API_KEY`** — **Settings → General → Security → API Key**.
3. **Check:**

```bash
docker compose exec plex-stack-automation sh -lc \
  'curl -sS -m 10 -H "X-Api-Key: ${RADARR_API_KEY}" "${RADARR_URL}/api/v3/system/status"'
```

Very large Radarr libraries (20k+ movies) may make the first `radarr_get_movies` MCP
call take on the order of **30 seconds**; that is expected for niavasha’s server.

### D.4 Optional Trakt (niavasha only)

Create a Trakt OAuth application with redirect URI `urn:ietf:wg:oauth:2.0:oob`. Put
`TRAKT_CLIENT_ID` and `TRAKT_CLIENT_SECRET` in `.env`. After bring-up, complete OAuth
via MCP tools such as `trakt_authenticate`. Trakt sync / scrobble tools are treated as
**writes** and require the same admin approval as other mutating MCP calls.

### D.5 Map credentials to containers

| Variable | `plex-ops-admin` | `plex-stack-automation` |
|----------|------------------|-------------------------|
| `PLEX_URL`, `PLEX_TOKEN` | yes | yes |
| `SONARR_*`, `RADARR_*` | no | yes |
| `TRAKT_*` | no | yes (optional) |

Compose injects these from the project `.env` (chmod `600`, gitignored). **mose-agent**
does not receive Plex/Sonarr/Radarr secrets; it only runs `docker exec -i` into the
sidecars and speaks MCP over stdio.

### D.6 Bring up MCP sidecars and networking

Build and start the idle MCP containers on `mose-net` (no published ports — only
other containers on the same compose network can reach them):

```bash
docker compose build plex-ops-admin plex-stack-automation
docker compose up -d plex-ops-admin plex-stack-automation
```

Then start or restart the agent as usual (section A.5). The agent image includes the
**docker CLI** so it can stdio-bridge into those containers using `mcp_servers.json`
entries like the ones in `mcp_servers.example.json`. `mose-agent` declares
`depends_on` for both sidecars, so `docker compose up -d mose-agent` will also
bring them up first — their containers must exist before the agent initializes
MCP or those tools will silently disappear until the next agent restart.

**To disable the Plex MCP integration entirely:** comment out the
`plex-ops-admin` and `plex-stack-automation` services **and** the two matching
`depends_on` lines under `mose-agent:` in `docker-compose.yml`, and remove the
same two entries from `mcp_servers.json`.

**Routing:** MCP containers use the default bridge (`mose-net`). Outbound connections
to each upstream IP on `10.4.251.0/24` are forwarded and SNATed by the Docker host,
same model as section A.5.3. There is **no shared “media host”** assumption — only
per-service URLs in `.env`.

**Policy — reads vs writes:** For server names `plex-ops-admin` and
`plex-stack-automation`, tools on the **read allowlist** in `mose/mcp_write_policy.py`
run immediately. Every other tool on those servers requires the **same human approval**
flow as `sre_execute` (Signal **admin** group with `SIGNAL_ADMIN_GROUP_ID`, 60 second
timeout). If no approval callback is configured (e.g. half-configured CLI), mutating
`use_tool` calls are **denied**. Other MCP servers (e.g. `paper_db`) are not gated by
this policy.

**Which server when:** [vladimir-tutin/plex-mcp-server](https://github.com/vladimir-tutin/plex-mcp-server)
(`plex-ops-admin`) covers broad Plex operations including playback and server maintenance.
[niavasha/plex-mcp-server](https://github.com/niavasha/plex-mcp-server)
(`plex-stack-automation`) adds Sonarr/Radarr and analytics-style Plex tools.

---

## E. Signal bot setup

### E.1 Install signal-cli

```bash
sudo mkdir -p /opt/signal-cli
sudo tar xf signal-cli-0.14.x-Linux.tar.gz -C /opt/signal-cli --strip-components=1
```

### E.2 Link your account (as the mose user)

```bash
sudo -u mose /opt/signal-cli/bin/signal-cli link -n "Mose Agent"
```

Scan the QR with your phone's Signal app (Settings → Linked Devices).

### E.3 Daemon service

Edit `signal-cli-daemon.service` and replace `+YOUR_PHONE_NUMBER` with the
phone number you just linked, then install:

```bash
sudo cp signal-cli-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now signal-cli-daemon
```

### E.4 Create two Signal groups and collect group ids

Mose listens and sends **only** on two Signal groups (no DM delivery):

1. **Engagement group** — day-to-day chat with the agent; tool status lines
   post here.
2. **Admin group** — skill proposals, reminders, restart recovery, skill-review
   summaries, and `sre_execute` approval prompts. Approve/reject replies must
   be sent **in this group** (the sender’s phone number is not used for routing).

Add the **linked device** (the account you registered with `signal-cli link`) as
a member of **both** groups from a primary Signal client.

Obtain each group’s **base64 `id`** (not the invite link):

```bash
# With the daemon running (E.3), one line per request; example using bash TCP:
{ echo '{"jsonrpc":"2.0","method":"listGroups","id":1}'; sleep 1; } | nc -q1 127.0.0.1 7583
```

Or from a shell on the host as `mose`:

```bash
signal-cli -a +15551234567 listGroups --json
```

Copy the `id` field for each group into `.env`.

### E.5 Wire the agent

Set all three variables in `.env`. If `SIGNAL_PHONE` is set but either group id
is missing, the agent exits with an error (half-configured Signal is rejected).

```bash
SIGNAL_PHONE=+15551234567
SIGNAL_ENGAGEMENT_GROUP_ID=<base64 id from listGroups>
SIGNAL_ADMIN_GROUP_ID=<base64 id from listGroups>
```

**Migration:** older installs stored `pending_approvals.recipient` as an E.164
phone. After switching to groups, those rows no longer match the admin group id.
Either wait for them to expire (default proposal timeout is 12 hours), or run:
`UPDATE pending_approvals SET recipient='<SIGNAL_ADMIN_GROUP_ID>' WHERE status='pending';`

The admin group receives:

- **Skill proposals** — the agent asks "may I build this skill?" and stores a
  durable `pending_approvals` row in SQLite with a default **12 hour** expiry.
  The admin replies at their convenience with:

  ```
  approve <slug>         # or: yes <slug>  / y <slug>
  reject <slug>          # or: no <slug>   / n <slug>
  yes                    # works only when exactly one proposal is pending
  ```

  Replies are processed via `handle_skill_decision`, which atomically flips
  the DB row and (on approval) asks the LLM for the full skill body.
- **Skill review summaries** — produced by the periodic review job, with a
  pointer to the full Markdown report under `data/logs/skill-review-*.md`.

The agent never builds, edits, or deletes a skill without an explicit
approval reply in the **admin group**.

### Durability across restarts

Pending approvals are persisted in the `pending_approvals` table in
`data/memory.db`, so they survive agent restarts, `docker compose build`,
systemd redeploys, and crashes. On every startup the agent runs a
**recovery pass** and delivers a single consolidated notice to the admin
covering everything that was outstanding:

- **Still pending** — items that are still within their timeout. The
  notice lists each slug/title/expiry and reminds the admin of the reply
  syntax (`approve <slug>` / `reject <slug>`). These require a decision.
- **Approved but not yet built** — orphans from a crash that happened
  *after* an approval was recorded but *before* the LLM finished drafting
  the skill body. The recovery notice warns the admin that a build is
  queued and will auto-start after the configured grace window
  (`learning.build_grace_window_seconds`, default **15 minutes**). The
  admin can abort it with `stop <slug>` or `cancel <slug>` on Signal, or
  `python -m mose --decide <slug> cancel` on the command line. Cancelling
  moves the proposal JSON to `skills/rejected/` with reason
  `user_cancelled_build`. If the agent crashes again inside the grace
  window, the next startup presents the same orphan with a fresh 15-min
  window — always giving the admin a chance to stop it.
- **Expired while I was down** — items whose `expires_at` fell in the
  past while the agent was offline. Their proposal files are moved to
  `skills/rejected/` with reason `timeout_across_restart` and the DB row
  is marked `expired`. These are listed **for awareness only** — no
  reply is needed.

If all three lists are empty the agent stays silent (no notification
noise). The recovery notice fires exactly once per startup, after the
Signal bot has connected, so the admin sees the full picture in one
message. In CLI mode the same three sections are printed to stdout on
startup.

An operator can also drive decisions manually without waiting for Signal:

```bash
# Apply a decision directly (pending proposals only)
python -m mose --decide my-skill approve        # or reject

# Abort an approved-but-unbuilt skill during its grace window
python -m mose --decide my-skill cancel         # stop / abort / halt also work

# Trigger the sweep by hand (handy after a long outage)
python -m mose --sweep-approvals
```

---

## F. Periodic skill review

There are two overlapping mechanisms and you can enable either or both:

1. **Built-in background task.** When the agent runs, it schedules its own
   in-process review every `review_interval_hours` (default 168 = weekly)
   after a `review_startup_delay_seconds` cushion. The review writes a
   Markdown report to `data/logs/skill-review-YYYY-MM-DD.md` and, if a
   review callback is configured (Signal), sends a summary to the admin.
2. **Systemd timer** (`mose-skill-review.timer`). Runs the one-shot
   `mose-skill-review.service` on a cron-like schedule (Mondays 03:30 by
   default). Use this when you want reviews to happen on a predictable
   wall-clock schedule regardless of when the agent restarts.

### Manual run

```bash
# Bare metal
sudo -u mose /home/mose/mose-agent/.venv/bin/python -m mose --skill-review

# Docker Compose
docker compose --profile review run --rm mose-skill-review
```

Skip Signal notification with `--skill-review-no-notify`:

```bash
python -m mose --skill-review --skill-review-no-notify
```

### Report contents

Each review emits a structured JSON log event (`skill_review_completed`) and
writes a Markdown report with:

- Total skills, number of candidates, threshold used
- A table of every skill (usage count, failure rate)
- For each candidate (usage ≥ `review_min_samples` AND failure rate ≥
  `skill_review_failure_threshold`): a recommended action
  (**rewrite / disable / keep / delete**), the LLM's reason, and optional
  suggested changes.

Nothing is applied automatically. The report and the Signal summary are the
agent's only outputs — an operator must review and action them.

---

## G. Operational security notes

- **Non-root everywhere.** The `mose` user has no sudo rights on bare metal;
  inside the container it runs as uid 1000. The only elevated capability is
  access to the Docker socket via the `docker` group, and only so the agent
  can `exec` into the sandbox container. Replace with a socket proxy for
  stricter deployments.
- **Sandboxed shell.** The `bash` tool is an **allowlist** (read-only,
  diagnostic commands only). Anything that changes state must go through
  `sre_execute`, which requires explicit human approval via Signal or CLI.
- **Plex MCP sidecars.** For `plex-ops-admin` and `plex-stack-automation`, mutating
  `use_tool` calls use the **same approval channel** as `sre_execute` (Signal admin
  group when Signal is configured). Read-only tools on the allowlist in
  `mose/mcp_write_policy.py` run without a prompt. Unknown tools default to **deny
  until approved**. The agent container mounts the Docker socket to run
  `docker exec -i` into the MCP sidecars — the same privilege model as shell
  sandboxing; do not publish MCP service ports.
- **Command policy.** `mose/bash_policy.py` centralises both the allowlist
  and the "always blocked" denylist (`rm -rf /`, `mkfs`, etc.) as a defense
  in depth layer on top of `sre_execute`.
- **Skill learning is human-in-the-loop.** The learning loop drafts
  proposals but *never* writes a skill body without Signal approval. The
  periodic review surfaces skills that are misbehaving but *never*
  modifies them.
- **Secrets.** `.env` should be `chmod 600`. `mcp_servers.json` is
  gitignored. `data/memory.db` is gitignored and user-readable only.
- **Network exposure.** vLLM binds `0.0.0.0:8001` by default — restrict
  with a host firewall if the box is network-reachable.

---

## H. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `TOMLDecodeError` / `Invalid statement (at line 1, column 1)` during `docker compose build` | `pyproject.toml` in the build context is not real TOML (Git LFS pointer, UTF-16, UTF-8 BOM edge case, empty file) | On the host: `head -n3 pyproject.toml` must start with `[project]`. If you see `version https://git-lfs.github.com`, run `git lfs pull`. Re-save as UTF-8 without BOM if edited on Windows. The image runs `docker/check_pyproject.py` before `pip` to surface this. |
| `permission denied while trying to connect to /var/run/docker.sock` inside the container | `DOCKER_GID` build arg didn't match the host group | Rebuild: `DOCKER_GID=$(getent group docker | cut -d: -f3) docker compose build` |
| `CUDA error: no kernel image` | Driver/CUDA version mismatch | Update NVIDIA driver and reinstall vLLM for the matching CUDA toolkit |
| `Connection refused` on port 8001 | `worker-agent` service not running | `sudo systemctl start worker-agent` and inspect `journalctl -u worker-agent` |
| `ModuleNotFoundError` | venv not activated / lockfile not synced | `source .venv/bin/activate && uv sync` (or `pip install -e ".[dev]"`) |
| Empty LLM responses | Model still loading | Wait for the LLM server to report ready |
| Signal bot won't connect | `signal-cli-daemon` down or not linked | `systemctl status signal-cli-daemon`; re-link if needed |
| Skill proposals never arrive on Signal | Group ids wrong or bot not in admin group | Set `SIGNAL_ADMIN_GROUP_ID`, add the linked device to that group, restart the agent |
| Skill review timer never fires | Timer not enabled | `sudo systemctl enable --now mose-skill-review.timer && systemctl list-timers 'mose-*'` |
| Mutating Plex MCP `use_tool` always denied | No approval callback (CLI / missing Signal admin) | Configure `SIGNAL_ADMIN_GROUP_ID` and run with Signal (section E), or use CLI / Discord where approval is wired |
| Admin never sees MCP approval prompt | Wrong admin group id or linked device not in admin group | Fix `SIGNAL_ADMIN_GROUP_ID`, add the linked device to the admin group, restart the agent |
| `docker exec` MCP connection fails | Sidecars not running or wrong container name in `mcp_servers.json` | `docker compose ps`; names must match `mose-plex-ops-admin` / `mose-plex-stack-automation` |

---

## I. Directory reference

```
~/mose-agent/                           # /home/mose/mose-agent
├── .env                                # Secrets (chmod 600, gitignored)
├── .env.example                        # Template for .env
├── config.toml                         # Agent configuration
├── mcp_servers.json                    # MCP server registry (gitignored)
├── mcp_servers.example.json            # Template for mcp_servers.json
├── pyproject.toml                      # Python package definition
├── uv.lock                             # Pinned dependency versions (committed)
├── docker-compose.yml                  # Docker Compose deployment
├── docker/plex-mcp-ops-admin/          # vladimir-tutin Plex MCP sidecar image
├── docker/plex-mcp-stack-automation/   # niavasha Plex+ARR MCP sidecar image
├── Dockerfile                          # Agent image (non-root, docker GID aware)
├── mose-agent.service                  # systemd unit — the agent
├── mose-skill-review.service           # systemd unit — one-shot skill review
├── mose-skill-review.timer             # systemd unit — weekly review schedule
├── worker-agent.service                # systemd unit — vLLM LLM server
├── signal-cli-daemon.service           # systemd unit — signal-cli JSON-RPC
├── mose/                               # Source code
├── skills/                             # Approved skills (loaded into the system prompt)
│   ├── pending/                        # Drafted proposals awaiting human approval
│   └── rejected/                       # Audit trail of declined proposals
├── tests/                              # Test suite
└── data/                               # Runtime data (gitignored, create manually)
    ├── memory.db                       # SQLite database
    ├── logs/                           # mose-YYYY-MM-DD.jsonl + skill-review-*.md
    ├── workspace/                      # Tool sandbox
    └── tool_outputs/                   # Persisted large outputs
```
