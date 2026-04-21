"""Pluggable terminal backends (local bash, Docker exec)."""

from __future__ import annotations

from mose.config import TerminalConfig
from mose.observe import get_logger, log_event
from mose.terminal.base import TerminalBackend, TerminalResult
from mose.terminal.docker import DockerTerminalBackend
from mose.terminal.local import LocalShellLegacyBackend, LocalTerminalBackend

logger = get_logger("terminal")

_backend: TerminalBackend | None = None


def get_backend() -> TerminalBackend:
    """Return the configured backend, defaulting to local bash argv mode."""
    global _backend
    if _backend is None:
        _backend = LocalTerminalBackend()
        log_event(logger, "terminal_backend_default", backend="local")
    return _backend


def init_terminal(config: TerminalConfig, workspace: str | None = None) -> None:
    """Configure terminal backend from config + env TERMINAL_BACKEND override."""
    global _backend
    import os

    backend = os.environ.get("TERMINAL_BACKEND", config.backend).lower()

    if backend == "docker":
        _backend = DockerTerminalBackend(
            container_name=config.container,
            default_cwd=config.workspace_mount,
            workspace_mount=config.workspace_mount,
        )
        log_event(logger, "terminal_backend_init", backend="docker", container=config.container)
    elif backend == "legacy_shell":
        _backend = LocalShellLegacyBackend()
        log_event(logger, "terminal_backend_init", backend="legacy_shell")
    else:
        _backend = LocalTerminalBackend(default_cwd=workspace)
        log_event(logger, "terminal_backend_init", backend="local")
