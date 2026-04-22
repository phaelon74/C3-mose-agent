"""Imports for Docker image ``docker/arr-diagnostics`` (smoke parity with plan tool counts)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARR_PKG = ROOT / "docker" / "arr-diagnostics"


@pytest.fixture(scope="module", autouse=True)
def _prepend_arr_path() -> None:
    p = str(ARR_PKG)
    if p not in sys.path:
        sys.path.insert(0, p)


def test_sonarr_command_allowlist_length() -> None:
    from arr_diagnostics.sonarr_mcp import SONARR_COMMANDS

    assert len(SONARR_COMMANDS) == 7


def test_radarr_command_allowlist_length() -> None:
    from arr_diagnostics.radarr_mcp import RADARR_COMMANDS

    assert len(RADARR_COMMANDS) == 6


def test_policy_read_counts_match_plan() -> None:
    from mose import mcp_write_policy as mp

    assert len(mp._SONARR_DIAG_READS) == 24  # noqa: SLF001
    assert len(mp._RADARR_DIAG_READS) == 23  # noqa: SLF001


def test_build_apps_do_not_raise() -> None:
    from arr_diagnostics.client import ArrClient
    from arr_diagnostics.radarr_mcp import build_radarr_app
    from arr_diagnostics.sonarr_mcp import build_sonarr_app

    s = ArrClient("http://127.0.0.1:8989", "test-key")
    r = ArrClient("http://127.0.0.1:7878", "test-key")
    try:
        assert build_sonarr_app(s) is not None
        assert build_radarr_app(r) is not None
    finally:
        s.close()
        r.close()
