#!/usr/bin/env bash
# Back up a Docker Compose Mose deployment for a move to another machine.
#
# Run on the SOURCE host, from anywhere:
#   bash scripts/mose-backup.sh
#   bash scripts/mose-backup.sh --output /var/backups
#
# What is archived (mode 600; it contains API keys and the Signal identity):
#   - .env and any other .env.* next to docker-compose.yml (not .env.example)
#   - mcp_servers.json, mcp_servers.portal.json, config.toml
#   - docker-compose.override.yml if you have one
#   - named volumes for this compose project (mose-data, mose-workspace, mose-skills)
#   - host data/ if that directory has files (bare-metal leftover; Docker does not use it)
#   - signal-cli data for --signal-user (default mose): ~/.local/share/signal-cli
#   - installed systemd units: mose-*, signal-cli-daemon, worker-agent
#
# What is not archived:
#   - the git checkout (clone the repo on the new host)
#   - Docker images (the restore script builds them)
#   - LLM / vLLM / TabbyAPI weights (worker-agent). Point LLM_ENDPOINT at a server.
#   - /var/run/docker.sock
#   - extra host bind mounts. They are listed in MANIFEST.txt and left in place.
#
# Database: there is no database server to start. memory.db and upcoming.db are
# SQLite files inside the mose-data volume. This script stops the Mose containers
# so those files are not written mid-copy. The Docker daemon stays up; that is
# what makes the volumes readable. Containers that were running are started
# again at the end unless you pass --leave-down.
#
# mose-workspace is a separate volume mounted on top of /app/data/workspace.
# Both volumes are archived. Copying mose-data alone drops the workspace.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=mose-migrate-common.sh
source "$ROOT/scripts/mose-migrate-common.sh"

LEAVE_DOWN=0
OUT_DIR=${MOSE_BACKUP_DIR:-"$HOME/mose-backups"}
SIGNAL_USER=${MOSE_SIGNAL_USER:-mose}

usage() {
  cat <<'EOF'
Usage: bash scripts/mose-backup.sh [--output DIR] [--signal-user USER] [--leave-down]

  --output DIR       Directory for the .tar.gz (default: ~/mose-backups)
  --signal-user USER Host account that runs signal-cli (default: mose)
  --leave-down       Do not start containers or signal-cli again after the copy

The archive is written mode 600. Copy it to the new machine, clone this repo,
then run scripts/mose-restore.sh. Do not start the stack on the new machine
before that restore finishes.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --leave-down) LEAVE_DOWN=1; shift ;;
    --output)
      [[ $# -ge 2 ]] || migrate_die "--output requires a directory"
      OUT_DIR=$2
      shift 2
      ;;
    --signal-user)
      [[ $# -ge 2 ]] || migrate_die "--signal-user requires a user name"
      SIGNAL_USER=$2
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) migrate_die "unknown argument: $1 (try --help)" ;;
  esac
done

cd "$ROOT"
[[ -f docker-compose.yml ]] || migrate_die "docker-compose.yml not found in ${ROOT}"

migrate_require_docker
migrate_load_compose

umask 077
mkdir -p "$OUT_DIR"
chmod 700 "$OUT_DIR"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
PARENT="${OUT_DIR}/.partial-${STAMP}"
STAGE_NAME="mose-backup-${STAMP}"
STAGE="${PARENT}/${STAGE_NAME}"
mkdir -p "$STAGE"

RUNNING_SERVICES=()
TIMERS_STOPPED=()
STACK_STOPPED=0
SIGNAL_STOPPED=0
SUCCESS=0

restart_what_we_stopped() {
  local keep_down=0 unit
  if [[ "$SUCCESS" -eq 1 && "$LEAVE_DOWN" -eq 1 ]]; then
    keep_down=1
  fi
  if [[ "$keep_down" -eq 0 && "$STACK_STOPPED" -eq 1 && ${#RUNNING_SERVICES[@]} -gt 0 ]]; then
    migrate_log "Starting Compose services again: ${RUNNING_SERVICES[*]}"
    docker compose start "${RUNNING_SERVICES[@]}" || migrate_log "WARNING: could not restart Compose services"
    STACK_STOPPED=0
  fi
  if [[ "$keep_down" -eq 0 && "$SIGNAL_STOPPED" -eq 1 ]]; then
    migrate_log "Starting signal-cli-daemon"
    sudo -n systemctl start signal-cli-daemon || migrate_log "WARNING: could not start signal-cli-daemon"
    SIGNAL_STOPPED=0
  fi
  if [[ "$keep_down" -eq 0 && ${#TIMERS_STOPPED[@]} -gt 0 ]]; then
    for unit in "${TIMERS_STOPPED[@]}"; do
      sudo -n systemctl start "$unit" || migrate_log "WARNING: could not start ${unit}"
    done
    TIMERS_STOPPED=()
  fi
  if [[ "$SUCCESS" -eq 0 ]]; then
    migrate_log "Backup did not finish. A partial directory may still be at ${PARENT}"
  fi
}
trap restart_what_we_stopped EXIT

capture_running_services() {
  local tmp svc filtered=()
  tmp=$(mktemp)
  migrate_running_service_names >"$tmp" || true
  mapfile -t RUNNING_SERVICES < "$tmp"
  rm -f "$tmp"
  for svc in "${RUNNING_SERVICES[@]+"${RUNNING_SERVICES[@]}"}"; do
    [[ -n "$svc" ]] && filtered+=("$svc")
  done
  RUNNING_SERVICES=("${filtered[@]+"${filtered[@]}"}")
}

stop_mose_timers() {
  local unit
  command -v systemctl >/dev/null 2>&1 || return 0
  # Stop timers first so a scheduled review or catalog sync cannot start a
  # writer while the volumes are being copied. Oneshots that are already
  # running are stopped too; they are not started again (the timer is).
  for unit in mose-skill-review.service mose-upcoming-sync.service mose-docker-prune.service; do
    systemctl is-active --quiet "$unit" 2>/dev/null || continue
    if sudo -n systemctl stop "$unit"; then
      migrate_log "Stopped running ${unit}"
    else
      migrate_log "WARNING: ${unit} is running and sudo could not stop it"
    fi
  done
  for unit in mose-skill-review.timer mose-upcoming-sync.timer mose-docker-prune.timer; do
    systemctl is-active --quiet "$unit" 2>/dev/null || continue
    if sudo -n systemctl stop "$unit"; then
      TIMERS_STOPPED+=("$unit")
      migrate_log "Paused ${unit} for the duration of the backup"
    else
      migrate_log "WARNING: ${unit} is active and sudo could not stop it. It might start a job mid-copy."
    fi
  done
}

stop_signal_if_active() {
  command -v systemctl >/dev/null 2>&1 || return 0
  systemctl cat signal-cli-daemon.service >/dev/null 2>&1 || return 0
  systemctl is-active --quiet signal-cli-daemon || return 0
  if sudo -n systemctl stop signal-cli-daemon; then
    SIGNAL_STOPPED=1
    migrate_log "Stopped signal-cli-daemon so its account database is not copied mid-write"
  else
    migrate_log "WARNING: signal-cli-daemon is running and sudo could not stop it. The Signal copy may be inconsistent. Re-run with passwordless sudo, or stop it yourself and run this script again."
  fi
}

copy_host_files() {
  local f base
  mkdir -p "$STAGE/host"
  shopt -s nullglob
  for f in "$ROOT"/.env "$ROOT"/.env.*; do
    base=$(basename "$f")
    [[ "$base" == ".env.example" ]] && continue
    cp -a "$f" "$STAGE/host/$base"
    chmod 600 "$STAGE/host/$base" || true
  done
  for f in mcp_servers.json mcp_servers.portal.json config.toml \
           docker-compose.override.yml docker-compose.override.yaml; do
    if [[ -f "$ROOT/$f" ]]; then
      cp -a "$ROOT/$f" "$STAGE/host/$f"
    fi
  done
  shopt -u nullglob
  [[ -f "$STAGE/host/.env" ]] || migrate_die "no .env next to docker-compose.yml; refusing to back up without secrets"
}

copy_host_data_dir() {
  [[ -d "$ROOT/data" ]] || return 0
  if ! find "$ROOT/data" -type f -print -quit | grep -q .; then
    return 0
  fi
  mkdir -p "$STAGE/host-data"
  migrate_log "Archiving host data/ (Docker uses named volumes; this is only the on-disk directory)"
  tar -C "$ROOT" -cf "$STAGE/host-data/data.tar" data
}

archive_signal() {
  local home dir
  mkdir -p "$STAGE/signal-cli"
  home=$(getent passwd "$SIGNAL_USER" 2>/dev/null | cut -d: -f6 || true)
  if [[ -z "$home" ]]; then
    printf 'missing-user\n' >"$STAGE/signal-cli/STATUS"
    migrate_log "WARNING: user ${SIGNAL_USER} does not exist; Signal data not copied"
    return 0
  fi
  dir="${home}/.local/share/signal-cli"
  if [[ ! -d "$dir" ]]; then
    printf 'missing-dir\n' >"$STAGE/signal-cli/STATUS"
    migrate_log "No ${dir}; Signal data not copied (fine if this host does not use Signal)"
    return 0
  fi
  if [[ -r "$dir" && -x "$dir" ]]; then
    tar -C "${home}/.local/share" -cf "$STAGE/signal-cli/signal-cli-data.tar" signal-cli
    printf 'included\n' >"$STAGE/signal-cli/STATUS"
    migrate_log "Archived Signal data from ${dir}"
    return 0
  fi
  if sudo -n tar -C "${home}/.local/share" -cf "$STAGE/signal-cli/signal-cli-data.tar" signal-cli; then
    sudo -n chown "$(id -u):$(id -g)" "$STAGE/signal-cli/signal-cli-data.tar"
    printf 'included\n' >"$STAGE/signal-cli/STATUS"
    migrate_log "Archived Signal data from ${dir} via sudo"
    return 0
  fi
  printf 'unreadable\n' >"$STAGE/signal-cli/STATUS"
  migrate_log "WARNING: cannot read ${dir}. Re-run as ${SIGNAL_USER} or with passwordless sudo so the linked Signal device is included."
}

copy_systemd_units() {
  local unit base state
  mkdir -p "$STAGE/systemd"
  : >"$STAGE/systemd/enabled.txt"
  shopt -s nullglob
  for unit in /etc/systemd/system/mose-*.service /etc/systemd/system/mose-*.timer \
              /etc/systemd/system/signal-cli-daemon.service \
              /etc/systemd/system/worker-agent.service; do
    [[ -f "$unit" ]] || continue
    if ! cp -a "$unit" "$STAGE/systemd/"; then
      migrate_log "WARNING: could not copy ${unit}"
      continue
    fi
    base=$(basename "$unit")
    state=$(systemctl is-enabled "$base" 2>/dev/null || true)
    printf '%s %s\n' "$base" "${state:-unknown}" >>"$STAGE/systemd/enabled.txt"
  done
  shopt -u nullglob
}

archive_volume() {
  local logical="$1"
  local full helper outfile
  if ! full=$(migrate_volume_full_name "$logical"); then
    migrate_log "WARNING: ${logical} is not a volume in this compose file; skipping"
    return 1
  fi
  if ! docker volume inspect "$full" >/dev/null 2>&1; then
    migrate_log "WARNING: volume ${full} does not exist yet; skipping ${logical}"
    return 1
  fi
  helper=$(migrate_helper_image)
  migrate_ensure_helper_image "$helper"
  outfile="$STAGE/volumes/${logical}.tar"
  migrate_log "Archiving volume ${logical} (${full})"
  if ! docker run --rm --user 0 --network none -v "${full}:/volume:ro" "$helper" \
      tar --format=pax -C /volume -cf - . >"$outfile"; then
    rm -f "$outfile"
    migrate_die "failed to archive volume ${logical}"
  fi
  [[ -s "$outfile" ]] || migrate_die "archive of ${logical} is empty"
}

write_manifest() {
  local commit branch dirty project gid signal_status binds
  commit=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || printf 'unknown')
  branch=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || printf 'unknown')
  dirty=clean
  if [[ -d "$ROOT/.git" ]]; then
    if ! git -C "$ROOT" diff --quiet >/dev/null 2>&1; then
      dirty=dirty
    fi
    if ! git -C "$ROOT" diff --cached --quiet >/dev/null 2>&1; then
      dirty=dirty
    fi
  else
    dirty=not-a-git-checkout
  fi
  project=$(migrate_project_name)
  gid=$(getent group docker | cut -d: -f3 || true)
  signal_status=$(cat "$STAGE/signal-cli/STATUS" 2>/dev/null || printf 'unknown')
  binds=$(migrate_extra_bind_mounts || true)
  {
    printf 'mose_backup_version=1\n'
    printf 'created_at=%s\n' "$STAMP"
    printf 'hostname=%s\n' "$(hostname)"
    printf 'repo_root=%s\n' "$ROOT"
    printf 'git_commit=%s\n' "$commit"
    printf 'git_branch=%s\n' "$branch"
    printf 'git_dirty=%s\n' "$dirty"
    printf 'compose_project=%s\n' "$project"
    printf 'docker_gid_source_host=%s\n' "${gid:-unknown}"
    printf 'signal_user=%s\n' "$SIGNAL_USER"
    printf 'signal_data=%s\n' "$signal_status"
    printf 'volumes='
    local first=1 logical
    for logical in "${LOGICAL[@]+"${LOGICAL[@]}"}"; do
      if [[ -f "$STAGE/volumes/${logical}.tar" ]]; then
        [[ $first -eq 1 ]] || printf ','
        printf '%s' "$logical"
        first=0
      fi
    done
    printf '\n'
    printf 'running_services=%s\n' "${RUNNING_SERVICES[*]-}"
    printf 'bind_mounts_not_archived<<END\n'
    if [[ -n "$binds" ]]; then
      printf '%s\n' "$binds"
    else
      printf 'none\n'
    fi
    printf 'END\n'
    printf 'excluded=llm-weights docker-images git-checkout docker.sock\n'
  } >"$STAGE/MANIFEST.txt"
}

write_restore_instructions() {
  cat >"$STAGE/RESTORE.txt" <<'EOF'
Mose restore order (new machine)
================================

Do this in this order. Starting the stack first creates empty Docker volumes
and the agent writes a blank memory database.

  1. Install Docker Engine 24+ and git. Create the operator user and add it
     to the docker group (INSTALL.md A.1). Log in again so the group applies.
  2. Install python3 (the restore script reads `docker compose config` with it).
  3. git clone the repo and cd into the clone. Prefer the commit recorded as
     git_commit in MANIFEST.txt.
  4. Copy this archive onto the machine, then from the clone run:

       bash scripts/mose-restore.sh /path/to/mose-backup-XXXX.tar.gz

     That script, and only that script, does the next steps:
       a. Copies .env, MCP registries, and config.toml into the clone.
       b. Rewrites DOCKER_GID to this machine (the old value is wrong here).
       c. docker compose build
          (mcp_servers.json and mcp_servers.portal.json are baked into the
          image by the Dockerfile, so the build has to happen AFTER the copy.)
       d. Creates the named volumes and unpacks them.
          mose-data     -> memory.db, upcoming.db, logs, tool outputs
          mose-workspace-> files the sandbox sees at /workspace
          mose-skills   -> approved skills, pending, rejected
  5. If MANIFEST.txt says signal_data=included, install signal-cli 0.14.x
     (INSTALL.md section E), then:

       sudo bash ~/mose-backups/host-state/mose-host-restore.sh

  6. Check LLM_ENDPOINT in .env. Model weights were not in this archive.
  7. Start Mose:

       docker compose up -d

     Pass --up to mose-restore.sh only if Signal is already restored or unused.

There is no database server to start before the unpack. SQLite lives in the
mose-data volume, and the restore script writes that volume before compose up.
EOF
}

write_checksums() {
  (
    cd "$STAGE"
    find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum
  ) >"$STAGE/SHA256SUMS"
}

capture_running_services
stop_mose_timers
if [[ ${#RUNNING_SERVICES[@]} -gt 0 ]]; then
  migrate_log "Stopping ${RUNNING_SERVICES[*]} so SQLite (memory.db, upcoming.db) is not written during the copy."
  migrate_log "There is no separate database server. The Docker daemon stays up; only the Mose containers stop."
  docker compose stop -t 30 "${RUNNING_SERVICES[@]}"
  STACK_STOPPED=1
  still=$(migrate_running_service_names || true)
  if [[ -n "$still" ]]; then
    migrate_die "Compose services are still running after stop; backup aborted so the database is not copied mid-write"
  fi
else
  migrate_log "No Compose services are running. Volumes are read in place."
fi

stop_signal_if_active

copy_host_files
copy_host_data_dir
archive_signal
copy_systemd_units

mkdir -p "$STAGE/volumes"
mapfile -t LOGICAL < <(docker compose config --volumes | sed '/^$/d')
if [[ ${#LOGICAL[@]} -eq 0 ]]; then
  LOGICAL=(mose-data mose-workspace mose-skills)
fi
for logical in "${LOGICAL[@]}"; do
  archive_volume "$logical" || true
done
[[ -f "$STAGE/volumes/mose-data.tar" ]] || migrate_die "mose-data was not archived. Refusing to write a backup that has no memory database. Start the stack once on this host so Docker creates the volume, or pass a compose project that already has it."

write_restore_instructions
write_manifest
write_checksums

ARCHIVE="${OUT_DIR}/${STAGE_NAME}.tar.gz"
migrate_log "Packing ${ARCHIVE}"
tar -C "$PARENT" -czf "$ARCHIVE" "$STAGE_NAME"
chmod 600 "$ARCHIVE"
rm -rf "$PARENT"

SUCCESS=1
migrate_log "Wrote ${ARCHIVE}"
migrate_log "Treat that file like a password. On the new host: clone the repo, then bash scripts/mose-restore.sh ${ARCHIVE}"
if [[ "$LEAVE_DOWN" -eq 1 ]]; then
  migrate_log "Leaving Mose stopped because --leave-down was set."
fi
