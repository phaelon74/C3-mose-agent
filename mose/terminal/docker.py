"""Run commands inside a sibling container via docker exec."""

from __future__ import annotations

import asyncio

from mose.observe import get_logger, log_event
from mose.terminal.base import TerminalBackend, TerminalResult

logger = get_logger("terminal.docker")


class DockerTerminalBackend(TerminalBackend):
    """Execute in `container_name` using docker-py (requires /var/run/docker.sock in agent)."""

    def __init__(
        self,
        container_name: str,
        default_cwd: str | None = None,
        workspace_mount: str = "/workspace",
    ) -> None:
        self._container_name = container_name
        self._default_cwd = default_cwd or workspace_mount
        self._workspace_mount = workspace_mount
        self._client: docker.DockerClient | None = None

    def _get_client(self):
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    async def run(self, command: str, timeout: int, cwd: str | None) -> TerminalResult:
        workdir = cwd or self._default_cwd

        def _exec() -> TerminalResult:
            try:
                client = self._get_client()
                container = client.containers.get(self._container_name)
            except Exception as e:
                log_event(logger, "docker_backend_error", error=str(e))
                return TerminalResult(125, "", f"Error: Docker backend: {e}")

            try:
                raw = container.exec_run(
                    cmd=["/bin/bash", "-lc", command],
                    workdir=workdir,
                    demux=True,
                )
            except Exception as e:
                return TerminalResult(125, "", f"Error: docker exec failed: {e}")

            if isinstance(raw, tuple) and len(raw) >= 2:
                code, output = raw[0], raw[1]
            else:
                code, output = getattr(raw, "exit_code", 1), getattr(raw, "output", None)
            code = int(code) if code is not None else 1

            if output is None:
                return TerminalResult(code, "", "")
            if isinstance(output, tuple) and len(output) == 2:
                stdout_b, stderr_b = output[0], output[1]
            else:
                stdout_b, stderr_b = (output or b""), b""
            out = (stdout_b or b"").decode("utf-8", errors="replace")
            err = (stderr_b or b"").decode("utf-8", errors="replace")
            return TerminalResult(code, out, err)

        try:
            return await asyncio.wait_for(asyncio.to_thread(_exec), timeout=timeout + 5)
        except asyncio.TimeoutError:
            return TerminalResult(124, "", f"Error: Command timed out after {timeout}s")
