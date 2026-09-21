# Shared helpers for scripts/mose-backup.sh and scripts/mose-restore.sh.
# Not executed directly.
# shellcheck shell=bash

migrate_die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

migrate_log() {
  printf '[%s] %s\n' "$(date -Is)" "$*"
}

migrate_require_docker() {
  command -v docker >/dev/null 2>&1 || migrate_die "docker not found in PATH"
  docker info >/dev/null 2>&1 || migrate_die "cannot talk to the Docker daemon. Start Docker and run this as a user in the docker group."
  docker compose version >/dev/null 2>&1 || migrate_die "docker compose v2 plugin not found"
  command -v python3 >/dev/null 2>&1 || migrate_die "python3 is required to read docker compose config"
  command -v sha256sum >/dev/null 2>&1 || migrate_die "sha256sum not found"
}

# Call after cd to the directory that contains docker-compose.yml.
migrate_load_compose() {
  COMPOSE_JSON=$(docker compose config --format json)
  [[ -n "$COMPOSE_JSON" ]] || migrate_die "docker compose config returned no JSON"
}

migrate_project_name() {
  printf '%s' "$COMPOSE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])'
}

# Echo the on-disk Docker volume name for a compose volume key (mose-data, ...).
migrate_volume_full_name() {
  local logical="$1"
  printf '%s' "$COMPOSE_JSON" | python3 -c '
import json, sys
logical = sys.argv[1]
cfg = json.load(sys.stdin)
vol = (cfg.get("volumes") or {}).get(logical)
if not vol:
    sys.stderr.write("volume %s is not in this compose file\n" % logical)
    sys.exit(2)
name = vol.get("name") or ""
if not name:
    sys.stderr.write("compose did not assign a name to %s\n" % logical)
    sys.exit(2)
print(name)
' "$logical"
}

# Bind mounts other than the Docker socket. Tab-separated: service, source, target.
migrate_extra_bind_mounts() {
  printf '%s' "$COMPOSE_JSON" | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
for svc, spec in cfg.get("services", {}).items():
    for vol in spec.get("volumes") or []:
        if not isinstance(vol, dict) or vol.get("type") != "bind":
            continue
        source = vol.get("source") or ""
        target = vol.get("target") or ""
        if source in ("/var/run/docker.sock", "/run/docker.sock"):
            continue
        print("%s\t%s\t%s" % (svc, source, target))
'
}

# Local image that has tar. Prefers images already on this machine.
migrate_helper_image() {
  local id="" img=""
  if id=$(docker inspect -f '{{.Image}}' mose-agent 2>/dev/null) && [[ -n "$id" ]]; then
    if docker image inspect "$id" >/dev/null 2>&1; then
      printf '%s\n' "$id"
      return 0
    fi
  fi
  while IFS= read -r img; do
    [[ -z "$img" ]] && continue
    if docker image inspect "$img" >/dev/null 2>&1; then
      printf '%s\n' "$img"
      return 0
    fi
  done < <(docker compose config --images 2>/dev/null || true)
  if docker image inspect python:3.11-slim-bookworm >/dev/null 2>&1; then
    printf '%s\n' python:3.11-slim-bookworm
    return 0
  fi
  # GNU tar, same family as the agent image. Avoid busybox tar so archives
  # created on one host extract on the other.
  printf '%s\n' python:3.11-slim-bookworm
}

# Print names of compose services that are currently running, one per line.
migrate_running_service_names() {
  local tmp
  tmp=$(mktemp)
  if docker compose ps --status running --services >"$tmp" 2>/dev/null; then
    sed '/^$/d' "$tmp"
    rm -f "$tmp"
    return 0
  fi
  rm -f "$tmp"
  docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null \
    | awk 'tolower($2)=="running" { print $1 }' || true
}

migrate_ensure_helper_image() {
  local helper="$1"
  if docker image inspect "$helper" >/dev/null 2>&1; then
    return 0
  fi
  migrate_log "Pulling helper image ${helper} so volume archives stay valid tar streams"
  docker pull "$helper"
}
