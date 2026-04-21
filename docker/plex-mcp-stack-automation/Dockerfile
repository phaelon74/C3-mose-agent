# Idle MCP sidecar for niavasha/plex-mcp-server (TypeScript). mose-agent connects
# via `docker exec -i mose-plex-stack-automation /usr/local/bin/mcp-entrypoint`.
FROM node:22-bookworm-slim

ARG PLEX_MCP_STACK_TAG=v1.2.0

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone --depth 1 --branch "${PLEX_MCP_STACK_TAG}" \
    https://github.com/niavasha/plex-mcp-server.git plex-mcp-server

WORKDIR /opt/plex-mcp-server
RUN npm ci && npm run build

COPY docker/plex-mcp-stack-automation/mcp-entrypoint.sh /usr/local/bin/mcp-entrypoint
RUN chmod +x /usr/local/bin/mcp-entrypoint

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
