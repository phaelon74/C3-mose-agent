# Idle MCP sidecar for vladimir-tutin/plex-mcp-server (PyPI). mose-agent connects
# via `docker exec -i mose-plex-ops-admin /usr/local/bin/mcp-entrypoint`.
FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Pin to match upstream GitHub release v1.1.7
RUN pip install --no-cache-dir "plex-mcp-server==1.1.7"

COPY docker/plex-mcp-ops-admin/mcp-entrypoint.sh /usr/local/bin/mcp-entrypoint
RUN chmod +x /usr/local/bin/mcp-entrypoint

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
