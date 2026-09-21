#!/usr/bin/env bash
# Restore a Mose Docker Compose backup onto a new machine.
#
# Correct order
# -------------
# Do not build or start the stack before this script. An early `docker compose up`
# creates empty volumes, and the agent then writes a blank memory database.
# MCP registries are copied into the image at build time, so the build has to
# happen after those files are in the clone.
#
#   1. Install Docker Engine 24+, git, and python3.
#      Create the operator user, add it to the docker group, and log in again.
#      See INSTALL.md section A.
#   2. git clone <this repo> && cd <clone>
#      Prefer the commit in the backup's MANIFEST.txt (git_commit).
#   3. bash scripts/mose-restore.sh /path/to/mose-backup-XXXX.tar.gz
#      This script then:
#        a. Copies .env, MCP registries, config.toml, and a compose override
#           into the clone.
#        b. Rewrites DOCKER_GID to THIS machine's docker group.
#        c. Runs `docker compose build`.
#        d. Creates named volumes and unpacks mose-data, mose-workspace,
#           and mose-skills into them.
#   4. If the backup includes Signal, install signal-cli 0.14.x (INSTALL.md E),
#      then:  sudo bash ~/mose-backups/host-state/mose-host-restore.sh
#   5. Point LLM_ENDPOINT in .env at a model server this machine can reach.
#      Model weights are not in the backup.
#   6. docker compose up -d
#      Or pass --up to step 3 if Signal is already restored or you do not use it.
#
# There is no database server to start. memory.db and upcoming.db are files
# inside the mose-data volume, and this script fills that volume before up.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=mose-migrate-common.sh
source "$ROOT/scripts/mose-migrate-common.sh"

DO_UP=0
FORCE=0
SKIP_BUILD=0
SIGNAL_USER=${MOSE_SIGNAL_USER:-mose}
ARCHIVE=""

usage() {
  cat <<'EOF'
Usage: bash scripts/mose-restore.sh ARCHIVE [--up] [--force] [--skip-build]
                                     [--signal-user USER]

Run this from a git clone on the NEW machine, before `docker compose up`.

  ARCHIVE            Path to mose-backup-*.tar.gz from scripts/mose-backup.sh
  --up               Start the stack at the end. Refused when Signal data is
                     in the backup and the host restore has not been applied.
  --force            Overwrite an existing .env and replace non-empty volumes.
  --skip-build       Do not `docker compose build` (only if you already built
                     AFTER this script copied .env and the MCP json files).
  --signal-user USER Account that should own restored signal-cli data (default: mose)

Host files (.env, Signal, systemd) are restored into this clone and into
~/mose-backups/host-state/. Signal data and systemd units are applied by:

  sudo bash ~/mose-backups/host-state/mose-host-restore.sh
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --up) DO_UP=1; shift ;;
    --force) FORCE=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --signal-user)
      [[ $# -ge 2 ]] || migrate_die "--signal-user requires a user name"
      SIGNAL_USER=$2
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    -*) migrate_die "unknown argument: $1 (try --help)" ;;
    *)
      [[ -z "$ARCHIVE" ]] || migrate_die "unexpected extra argument: $1"
      ARCHIVE=$1
      shift
      ;;
  esac
done

# Fix the accidental ARCHIVE=$2 above by re-parsing simply if I made a bug.
# The case sets ARCHIVE=$1 then also had a leftover comment. Let me look...
# I wrote:
#   ARCHIVE=$2
#   ARCHIVE=$1
#   shift
# That's correct because ARCHIVE=$1 overwrites ARCHIVE=$2. ARCHIVE=$2 is useless
# but if ARCHIVE is the only positional, $2 might be empty and then overwritten.
# OK it's correct. I'll remove the dead line in a follow-up edit to keep the script clean.

[[ -n "$ARCHIVE" ]] || { usage >&2; migrate_die "missing ARCHIVE path"; }

print_order() {
  cat <<EOF

Mose restore — this script's order
  1. You already cloned the repo. This script is running inside it.
  2. Copy secrets and operator config into the clone.
  3. Set DOCKER_GID for this machine.
  4. docker compose build   (skipped only with --skip-build)
  5. Unpack Docker volumes while the stack is stopped.
  6. Write ~/mose-backups/host-state/mose-host-restore.sh for Signal and systemd.
  7. docker compose up -d   only with --up, and only after Signal is handled.

Archive: ${ARCHIVE}
Clone:   ${ROOT}

EOF
}

resolve_archive() {
  [[ -f "$ARCHIVE" ]] || migrate_die "backup not found: ${ARCHIVE}"
  local dir base
  dir=$(cd "$(dirname "$ARCHIVE")" && pwd)
  base=$(basename "$ARCHIVE")
  ARCHIVE="${dir}/${base}"
}

assert_stack_stopped() {
  local running=""
  running=$(migrate_running_service_names || true)
  [[ -z "$running" ]] || migrate_die "Compose services are running. Stop them first: docker compose stop"
}

manifest_value() {
  local key="$1" file="$2"
  local line
  line=$(grep -E "^${key}=" "$file" | head -n 1 || true)
  printf '%s' "${line#${key}=}"
}

copy_tree_file() {
  local src="$1" dest="$2" mode="$3"
  [[ -f "$src" ]] || return 0
  if [[ -e "$dest" && "$FORCE" -eq 0 && "$dest" != "$ROOT/config.toml" ]]; then
    migrate_die "${dest} already exists. Pass --force to overwrite it."
  fi
  if [[ "$dest" == "$ROOT/config.toml" && -f "$dest" && ! -f "$ROOT/config.toml.from-git" ]]; then
    cp -a "$dest" "$ROOT/config.toml.from-git"
    migrate_log "Saved the clone's config.toml as config.toml.from-git"
  fi
  cp -a "$src" "$dest"
  chmod "$mode" "$dest" || true
  migrate_log "Restored $(basename "$dest")"
}

update_docker_gid() {
  local gid
  gid=$(getent group docker | cut -d: -f3 || true)
  [[ -n "$gid" ]] || migrate_die "docker group not found. Install Docker and add this user to the group, then log in again."
  [[ -f "$ROOT/.env" ]] || migrate_die ".env was not restored"
  if grep -q '^DOCKER_GID=' "$ROOT/.env"; then
    sed -i "s/^DOCKER_GID=.*/DOCKER_GID=${gid}/" "$ROOT/.env"
  else
    printf '\n# Set for this host by scripts/mose-restore.sh\nDOCKER_GID=%s\n' "$gid" >>"$ROOT/.env"
  fi
  export DOCKER_GID="$gid"
  chmod 600 "$ROOT/.env"
  migrate_log "Set DOCKER_GID=${gid} for this host (the value from the old machine is not used)"
}

volume_has_files() {
  local full="$1" helper="$2" listing
  listing=$(docker run --rm --user 0 --network none -v "${full}:/volume:ro" "$helper" ls -A /volume)
  [[ -n "$listing" ]]
}

restore_one_volume() {
  local tarfile="$1" helper="$2"
  local logical full
  logical=$(basename "$tarfile" .tar)
  full=$(migrate_volume_full_name "$logical")
  if docker volume inspect "$full" >/dev/null 2>&1; then
    if volume_has_files "$full" "$helper"; then
      [[ "$FORCE" -eq 1 ]] || migrate_die "volume ${full} already has files. Pass --force to replace it. Refusing so a blank database is not mixed with this backup."
      migrate_log "Removing existing volume ${full} (--force)"
      docker compose down
      docker volume rm "$full"
    fi
  fi
  docker volume create "$full" >/dev/null
  migrate_log "Unpacking ${logical} into ${full}"
  docker run --rm --user 0 --network none -i -v "${full}:/volume" "$helper" \
    tar --numeric-owner -C /volume -xpf - <"$tarfile"
}

write_host_restore_script() {
  local state="$1"
  local script="${state}/mose-host-restore.sh"
  local has_signal=0
  [[ -f "${state}/signal-cli-data.tar" ]] && has_signal=1
  if [[ "$has_signal" -eq 0 && ! -d "${state}/systemd" ]]; then
    migrate_log "No Signal data or systemd units in this backup; skipping the host script"
    return 0
  fi

  cat >"$script" <<EOF
#!/usr/bin/env bash
# Apply Signal device data and systemd units from a Mose backup.
# Generated by scripts/mose-restore.sh. Review it, then:
#   sudo bash ${script}
set -euo pipefail

if [[ \$(id -u) -ne 0 ]]; then
  printf 'Run as root: sudo bash %s\n' "${script}" >&2
  exit 1
fi

SIGNAL_USER=${SIGNAL_USER@Q}
STATE=${state@Q}
HAS_SIGNAL=${has_signal}
signal_pending=0

if ! getent passwd "\$SIGNAL_USER" >/dev/null; then
  printf 'User %s does not exist. Create it first (INSTALL.md A.1), then re-run.\n' "\$SIGNAL_USER" >&2
  exit 1
fi
home=\$(getent passwd "\$SIGNAL_USER" | cut -d: -f6)

if [[ "\$HAS_SIGNAL" -eq 1 ]]; then
  systemctl stop signal-cli-daemon.service 2>/dev/null || true
  install -d -o "\$SIGNAL_USER" -g "\$SIGNAL_USER" "\$home/.local/share"
  if [[ -d "\$home/.local/share/signal-cli" ]]; then
    mv "\$home/.local/share/signal-cli" "\$home/.local/share/signal-cli.bak-\$(date -u +%Y%m%dT%H%M%SZ)"
  fi
  tar -C "\$home/.local/share" -xpf "\$STATE/signal-cli-data.tar"
  chown -R "\$SIGNAL_USER:\$SIGNAL_USER" "\$home/.local/share/signal-cli"
  printf 'Restored Signal data to %s/.local/share/signal-cli\n' "\$home"
fi

shopt -s nullglob
unit_files=("\$STATE/systemd/"*.service "\$STATE/systemd/"*.timer)
if [[ \${#unit_files[@]} -gt 0 ]]; then
  cp -a "\${unit_files[@]}" /etc/systemd/system/
  systemctl daemon-reload
  printf 'Installed systemd units into /etc/systemd/system\n'
fi

if [[ -f "\$STATE/systemd/enabled.txt" ]]; then
  while read -r name unit_state; do
    [[ -n "\$name" ]] || continue
    [[ "\$unit_state" == "enabled" ]] || continue
    case "\$name" in
      worker-agent.service)
        printf 'NOTE: copied %s but did not enable it. Model weights were not in the Mose backup. Enable it after the model server is installed.\n' "\$name"
        ;;
      mose-agent.service)
        printf 'NOTE: copied %s but did not enable it. A Docker restore should start Mose with docker compose up, not the bare-metal unit.\n' "\$name"
        ;;
      signal-cli-daemon.service)
        if [[ ! -x /opt/signal-cli/bin/signal-cli ]]; then
          printf 'signal-cli is not at /opt/signal-cli. Install 0.14.x (INSTALL.md E.1), then re-run this script to enable the daemon.\n' >&2
          signal_pending=1
        else
          systemctl enable --now "\$name"
        fi
        ;;
      *.timer)
        systemctl enable --now "\$name"
        ;;
      *)
        systemctl enable "\$name"
        ;;
    esac
  done <"\$STATE/systemd/enabled.txt"
fi

if [[ "\$signal_pending" -eq 1 ]]; then
  exit 1
fi
printf 'Host restore finished.\n'
EOF
  chmod 700 "$script"
  migrate_log "Wrote ${script}"
}

resolve_archive
print_order

cd "$ROOT"
[[ -f docker-compose.yml ]] || migrate_die "docker-compose.yml not found in ${ROOT}. Run this script from a clone of the repo."

migrate_require_docker

tmp=$(mktemp -d)
cleanup() {
  rm -rf "$tmp"
}
trap cleanup EXIT

migrate_log "Extracting archive"
tar -C "$tmp" -xzf "$ARCHIVE"
mapfile -t tops < <(find "$tmp" -mindepth 1 -maxdepth 1 -type d)
[[ ${#tops[@]} -eq 1 ]] || migrate_die "archive should contain one top-level directory"
TOP=${tops[0]}
[[ -f "$TOP/MANIFEST.txt" ]] || migrate_die "not a Mose backup (missing MANIFEST.txt)"
[[ -f "$TOP/SHA256SUMS" ]] || migrate_die "not a Mose backup (missing SHA256SUMS)"

migrate_log "Checking checksums"
(cd "$TOP" && sha256sum -c SHA256SUMS)

version=$(manifest_value mose_backup_version "$TOP/MANIFEST.txt")
[[ "$version" == "1" ]] || migrate_die "unsupported backup version: ${version:-unknown}"

src_commit=$(manifest_value git_commit "$TOP/MANIFEST.txt")
if [[ -d "$ROOT/.git" && "$src_commit" != "unknown" && -n "$src_commit" ]]; then
  cur_commit=$(git -C "$ROOT" rev-parse HEAD)
  if [[ "$cur_commit" != "$src_commit" ]]; then
    migrate_log "WARNING: backup was taken at git ${src_commit}; this clone is ${cur_commit}. config.toml from the backup will replace this clone's copy. The previous file is saved as config.toml.from-git."
  fi
fi

signal_status=$(manifest_value signal_data "$TOP/MANIFEST.txt")
manifest_signal_user=$(manifest_value signal_user "$TOP/MANIFEST.txt")
if [[ -n "$manifest_signal_user" && "$SIGNAL_USER" == "mose" && "$manifest_signal_user" != "mose" ]]; then
  SIGNAL_USER=$manifest_signal_user
  migrate_log "Using signal user ${SIGNAL_USER} from the backup manifest"
fi

assert_stack_stopped

shopt -s nullglob
for src in "$TOP"/host/.env "$TOP"/host/.env.*; do
  base=$(basename "$src")
  [[ "$base" == ".env.example" ]] && continue
  copy_tree_file "$src" "$ROOT/$base" 600
done
copy_tree_file "$TOP/host/mcp_servers.json" "$ROOT/mcp_servers.json" 600
copy_tree_file "$TOP/host/mcp_servers.portal.json" "$ROOT/mcp_servers.portal.json" 600
copy_tree_file "$TOP/host/config.toml" "$ROOT/config.toml" 644
copy_tree_file "$TOP/host/docker-compose.override.yml" "$ROOT/docker-compose.override.yml" 644
copy_tree_file "$TOP/host/docker-compose.override.yaml" "$ROOT/docker-compose.override.yaml" 644
shopt -u nullglob

[[ -f "$ROOT/.env" ]] || migrate_die "backup has no .env; refusing to continue"

if [[ -f "$TOP/host-data/data.tar" ]]; then
  if [[ -d "$ROOT/data" && "$FORCE" -eq 0 ]] && find "$ROOT/data" -type f -print -quit | grep -q .; then
    migrate_log "WARNING: leaving existing host data/ in place. Docker does not use it. Pass --force to replace it from the backup."
  else
    tar -C "$ROOT" -xpf "$TOP/host-data/data.tar"
    migrate_log "Restored host data/ (bare-metal path). The running agent uses the Docker volumes, not this directory."
  fi
fi

update_docker_gid
# Compose config is read after .env is in place so volume names match this directory.
migrate_load_compose

if [[ "$SKIP_BUILD" -eq 0 ]]; then
  migrate_log "Building images. This has to happen now: the Dockerfile copies mcp_servers.json and mcp_servers.portal.json into the image."
  docker compose build
else
  migrate_log "Skipping docker compose build because --skip-build was set"
fi

helper=$(migrate_helper_image)
migrate_ensure_helper_image "$helper"

shopt -s nullglob
volume_tars=("$TOP"/volumes/*.tar)
shopt -u nullglob
[[ ${#volume_tars[@]} -gt 0 ]] || migrate_die "backup contains no volume archives"
found_data=0
for tarfile in "${volume_tars[@]}"; do
  [[ "$(basename "$tarfile")" == "mose-data.tar" ]] && found_data=1
  restore_one_volume "$tarfile" "$helper"
done
[[ "$found_data" -eq 1 ]] || migrate_die "backup is missing volumes/mose-data.tar"

state="${HOME}/mose-backups/host-state"
rm -rf "$state"
mkdir -p "$state"
chmod 700 "${HOME}/mose-backups" "$state"
if [[ -f "$TOP/signal-cli/signal-cli-data.tar" ]]; then
  cp -a "$TOP/signal-cli/signal-cli-data.tar" "$state/"
fi
if [[ -d "$TOP/systemd" ]]; then
  mkdir -p "$state/systemd"
  cp -a "$TOP/systemd/." "$state/systemd/"
fi
write_host_restore_script "$state"

host_applied=0
if [[ -f "$state/mose-host-restore.sh" ]]; then
  if sudo -n true 2>/dev/null; then
    migrate_log "Applying Signal data and systemd units (passwordless sudo)"
    if sudo -n bash "$state/mose-host-restore.sh"; then
      host_applied=1
    else
      migrate_log "WARNING: host restore did not finish. Docker volumes are already in place. Fix the error above and re-run: sudo bash ${state}/mose-host-restore.sh"
    fi
  fi
fi

cat <<EOF

Docker volumes and project files are restored.
Compose project: $(migrate_project_name)

Still do this before relying on the agent:
EOF

if [[ -f "$state/mose-host-restore.sh" && "$host_applied" -eq 0 ]]; then
  cat <<EOF
  - Signal / systemd (install signal-cli 0.14.x first if signal_data=${signal_status}):
      sudo bash ${state}/mose-host-restore.sh
EOF
fi

cat <<EOF
  - Confirm LLM_ENDPOINT in .env reaches a model server from this machine.
    Weights were not part of the backup.
  - Start Mose (the stack is still stopped):
      docker compose up -d
EOF

if [[ "$DO_UP" -eq 1 ]]; then
  if [[ "$signal_status" == "included" && "$host_applied" -eq 0 ]]; then
    migrate_die "Refusing --up because Signal data is in the backup and has not been restored yet. Run the sudo command above, then: docker compose up -d"
  fi
  migrate_log "Starting the stack"
  docker compose up -d
  docker compose ps
fi
