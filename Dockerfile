# Mose SRE/DevOps agent — Linux container.
#
# Runs as an unprivileged user (uid 1000, gid 1000). When the container needs
# access to the host Docker socket (TERMINAL_BACKEND=docker), pass the host's
# docker group id as a build arg so the in-container `mose` user is a member
# of a matching group:
#
#   DOCKER_GID=$(getent group docker | cut -d: -f3)
#   docker compose build --build-arg DOCKER_GID=$DOCKER_GID
#
# Without this the non-root user would need to run as root to read the socket,
# which defeats the purpose. Use a socket proxy for stricter deployments.

# Client-only docker binary for `docker exec -i` MCP stdio bridge (see INSTALL.md D).
FROM docker:26-cli AS dockercli

FROM python:3.11-slim-bookworm

ARG DOCKER_GID=999
ARG APP_UID=1000
ARG APP_GID=1000

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
COPY . .

# Fail fast with actionable errors if pyproject.toml is a BOM/LFS pointer/UTF-16 style corrupt file.
RUN python3 docker/check_pyproject.py

RUN pip install --no-cache-dir -e ".[dev]" \
    && groupadd -g ${APP_GID} mose \
    && useradd -m -u ${APP_UID} -g ${APP_GID} -s /bin/bash mose \
    && (getent group docker >/dev/null || groupadd -g ${DOCKER_GID} docker) \
    && usermod -aG docker mose \
    && mkdir -p /app/data/logs /app/data/workspace /app/data/tool_outputs \
    && chown -R mose:mose /app

USER mose
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "mose"]
