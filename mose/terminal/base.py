"""Abstract terminal backend for sandboxed shell execution."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TerminalResult:
    """Result of running a shell command."""

    exit_code: int
    stdout: str
    stderr: str


class TerminalBackend(ABC):
    """Run shell commands via local subprocess or remote/docker sandbox."""

    @abstractmethod
    async def run(self, command: str, timeout: int, cwd: str | None) -> TerminalResult:
        """Execute `command` with a shell (bash -lc). cwd may be None for backend default."""
