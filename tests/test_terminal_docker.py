"""Docker terminal backend module (no daemon required)."""

from __future__ import annotations

from mose.terminal.docker import DockerTerminalBackend


def test_docker_backend_class():
    b = DockerTerminalBackend("mose-sandbox", default_cwd="/workspace")
    assert b._container_name == "mose-sandbox"
