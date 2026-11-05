# Idle sidecar; portal attaches via `docker exec -i mose-plex-issues-diagnostics …`.
# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/plex-issues-diagnostics

COPY docker/plex-issues-diagnostics/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY docker/plex-issues-diagnostics/plex_issues_diagnostics ./plex_issues_diagnostics

ENV PYTHONPATH=/opt/plex-issues-diagnostics
ENV PYTHONUNBUFFERED=1

COPY docker/plex-issues-diagnostics/mcp-entrypoint.sh /usr/local/bin/mcp-entrypoint
RUN chmod +x /usr/local/bin/mcp-entrypoint

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
