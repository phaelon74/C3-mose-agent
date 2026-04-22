# Idle sidecar; mose-agent attaches via `docker exec -i mose-sonarr-diagnostics …` / radarr sibling.
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/arr-diagnostics

COPY docker/arr-diagnostics/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/arr-diagnostics/arr_diagnostics ./arr_diagnostics
COPY docker/arr-diagnostics/scripts ./scripts

ENV PYTHONPATH=/opt/arr-diagnostics
ENV PYTHONUNBUFFERED=1

COPY docker/arr-diagnostics/mcp-entrypoint-sonarr.sh /usr/local/bin/mcp-entrypoint-sonarr
COPY docker/arr-diagnostics/mcp-entrypoint-radarr.sh /usr/local/bin/mcp-entrypoint-radarr
RUN chmod +x /usr/local/bin/mcp-entrypoint-sonarr /usr/local/bin/mcp-entrypoint-radarr

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
