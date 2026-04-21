"""Run commands inside a sibling container via docker exec."""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath

from mose.observe import get_logger, log_event
from mose.terminal.base import TerminalBackend, TerminalResult

logger = get_logger("terminal.docker")


def sandbox_workdir(
    cwd: str | None,
    host_workspace: Path | None,
    mount: str,
) -> tuple[str, bool]:
    """Map agent-side cwd to a path inside the sandbox container.

    Returns ``(workdir, used_fallback)``. When ``cwd`` is outside ``host_workspace``,
    returns ``mount`` and ``used_fallback=True`` (caller should log).
    """
    mount_norm = mount.rstrip("/") or "/"
    if cwd is None:
        return mount_norm, False
    p = Path(cwd).resolve()
    if host_workspace is None:
        return mount_norm, False
    hw = host_workspace.resolve()
    try:
        rel = p.relative_to(hw)
    except ValueError:
        return mount_norm, True
    if rel == Path("."):
        return mount_norm, False
    return str(PurePosixPath(mount_norm) / PurePosixPath(rel.as_posix())), False


class DockerTerminalBackend(TerminalBackend):
    """Execute in `container_name` using docker-py (requires /var/run/docker.sock in agent)."""

    def __init__(
        self,
        container_name: str,
        default_cwd: str | None = None,
        workspace_mount: str = "/workspace",
        host_workspace: Path | None = None,
    ) -> None:
        self._container_name = container_name
        self._default_cwd = default_cwd or workspace_mount
        self._workspace_mount = workspace_mount
        self._host_workspace = host_workspace.resolve() if host_workspace is not None else None
        self._client: docker.DockerClient | None = None

    def _get_client(self):
        if self._client is None:
            import docker

            self._client = docker.from_env()
        return self._client

    async def run(self, command: str, timeout: int, cwd: str | None) -> TerminalResult:
        if cwd is None:
            workdir = self._default_cwd
        else:
            workdir, fallback = sandbox_workdir(cwd, self._host_workspace, self._workspace_mount)
            if fallback:
                log_event(
                    logger,
                    "docker_workdir_fallback",
                    cwd=cwd,
                    mount=self._workspace_mount,
                    host_workspace=str(self._host_workspace) if self._host_workspace else "",
                )

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
