"""Docker terminal backend: path mapping (no Docker daemon required)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mose.terminal.docker import DockerTerminalBackend, sandbox_workdir


def test_sandbox_workdir_none_cwd():
    w, fb = sandbox_workdir(None, Path("/app/ws"), "/workspace")
    assert w == "/workspace"
    assert not fb


def test_sandbox_workdir_at_workspace_root():
    hw = Path("/app/data/workspace").resolve()
    w, fb = sandbox_workdir(str(hw), hw, "/workspace")
    assert w == "/workspace"
    assert not fb


def test_sandbox_workdir_nested():
    hw = Path("/app/data/workspace").resolve()
    cwd = hw / "proj" / "a"
    w, fb = sandbox_workdir(str(cwd), hw, "/workspace")
    assert w == "/workspace/proj/a"
    assert not fb


def test_sandbox_workdir_outside_fallback():
    hw = Path("/app/data/workspace").resolve()
    w, fb = sandbox_workdir("/etc/passwd", hw, "/workspace")
    assert w == "/workspace"
    assert fb


def test_sandbox_workdir_trailing_slash_cwd(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    (d / "sub").mkdir()
    hw = d.resolve()
    w, fb = sandbox_workdir(str(d / "sub") + "/", hw, "/workspace")
    assert w == "/workspace/sub"
    assert not fb


def test_sandbox_workdir_no_host_workspace():
    w, fb = sandbox_workdir("/any/path", None, "/workspace")
    assert w == "/workspace"
    assert not fb


@pytest.mark.skipif(os.name == "nt", reason="symlink workspace test is POSIX-oriented")
def test_sandbox_workdir_symlink_parity(tmp_path):
    real = tmp_path / "real_ws"
    real.mkdir()
    link = tmp_path / "link_ws"
    link.symlink_to(real, target_is_directory=True)
    hw = real.resolve()
    cwd_link = (link / "sub").resolve()
    w, fb = sandbox_workdir(str(cwd_link), hw, "/workspace")
    assert w == "/workspace/sub"
    assert not fb


def test_docker_backend_class():
    b = DockerTerminalBackend("mose-sandbox", default_cwd="/workspace")
    assert b._container_name == "mose-sandbox"
    assert b._host_workspace is None
