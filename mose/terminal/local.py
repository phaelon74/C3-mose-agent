"""Local bash execution without subprocess shell=True (argv list only)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess

from mose.observe import get_logger
from mose.terminal.base import TerminalBackend, TerminalResult

logger = get_logger("terminal.local")


def _find_bash() -> str:
    """Resolve bash for dev/test (Git Bash on Windows, /bin/bash on Unix)."""
    for name in ("bash", "bash.exe"):
        p = shutil.which(name)
        if p:
            return p
    raise RuntimeError(
        "bash not found on PATH — install Git Bash or use WSL, or set terminal.backend=docker"
    )


class LocalTerminalBackend(TerminalBackend):
    """Run commands as `bash -lc <command>` without shell=True on subprocess.run."""

    def __init__(self, default_cwd: str | None = None) -> None:
        self._bash = _find_bash()
        self._default_cwd = default_cwd

    async def run(self, command: str, timeout: int, cwd: str | None) -> TerminalResult:
        work = cwd or self._default_cwd
        try:
            proc = await asyncio.wait_for(
                asyncio.to_thread(
                    self._run_sync,
                    command,
                    timeout,
                    work,
                ),
                timeout=timeout + 5,
            )
        except asyncio.TimeoutError:
            return TerminalResult(124, "", f"Error: Command timed out after {timeout}s")
        except FileNotFoundError:
            return TerminalResult(127, "", f"Error: Working directory not found: {work}")
        return proc

    def _run_sync(self, command: str, timeout: int, cwd: str | None) -> TerminalResult:
        kwargs: dict = {
            "args": [self._bash, "-lc", command],
            "capture_output": True,
            "text": True,
            "stdin": subprocess.DEVNULL,
            "timeout": timeout,
        }
        if cwd:
            kwargs["cwd"] = cwd
        proc = subprocess.run(**kwargs)  # noqa: S603 — argv list, no shell injection vector
        out = proc.stdout or ""
        err = proc.stderr or ""
        return TerminalResult(proc.returncode, out, err)


class LocalShellLegacyBackend(TerminalBackend):
    """Legacy path: subprocess.run(shell=True). Use only in isolated tests."""

    async def run(self, command: str, timeout: int, cwd: str | None) -> TerminalResult:
        try:
            proc = await asyncio.wait_for(
                asyncio.to_thread(
                    subprocess.run,
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    cwd=cwd,
                    timeout=timeout,
                ),
                timeout=timeout + 5,
            )
        except asyncio.TimeoutError:
            return TerminalResult(124, "", f"Error: Command timed out after {timeout}s")
        except FileNotFoundError:
            return TerminalResult(127, "", f"Error: Working directory not found: {cwd}")
        out = proc.stdout or ""
        err = proc.stderr or ""
        return TerminalResult(proc.returncode, out, err)
