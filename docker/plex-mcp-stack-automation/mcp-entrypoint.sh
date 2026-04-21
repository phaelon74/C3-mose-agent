#!/bin/sh
set -e
if [ -z "${PLEX_URL}" ] || [ -z "${PLEX_TOKEN}" ]; then
  echo "mcp-entrypoint: PLEX_URL and PLEX_TOKEN must be set in the container environment." >&2
  exit 1
fi
exec node /opt/plex-mcp-server/build/plex-mcp-server.js
