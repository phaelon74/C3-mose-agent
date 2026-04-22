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


def test_manual_import_hints_match_folded_when_punctuation_differs() -> None:
    """Dots vs underscores in filenames still match queue release name hints."""
    from arr_diagnostics.sonarr_manual_import import _pick_manual_row

    rows = [
        {"seriesId": 1, "path": "/mnt/DL/MPACT_x_Nightline_S04E26/foo.mkv", "episodes": []},
        {"seriesId": 1, "path": "/mnt/DL/other/show.mkv", "episodes": []},
    ]
    picked = _pick_manual_row(
        rows,
        1,
        999,
        season_number=4,
        episode_number=26,
        path_hints=["MPACT.x.Nightline.S04E26"],
    )
    assert picked is rows[0]


def test_manual_import_row_picked_by_nested_season_episode() -> None:
    """When episode id is missing on manualimport rows, match S/E on nested episodes."""
    from arr_diagnostics.sonarr_manual_import import _pick_manual_row

    rows = [
        {
            "seriesId": 10,
            "downloadId": "abc",
            "path": "/dl/a/other.mkv",
            "episodes": [{"seasonNumber": 4, "episodeNumber": 25}],
        },
        {
            "seriesId": 10,
            "downloadId": "abc",
            "path": "/dl/a/target.mkv",
            "episodes": [{"seasonNumber": 4, "episodeNumber": 26}],
        },
    ]
    picked = _pick_manual_row(
        rows,
        10,
        172292,
        season_number=4,
        episode_number=26,
        path_hints=None,
    )
    assert picked is rows[1]


def test_prepare_row_queries_downloadid_only() -> None:
    """Sonarr ignores downloadId when seriesId is also passed (library/season scan returned).

    Regression test: ``_prepare_row`` MUST call ``GET /manualimport`` with ``downloadId`` only.
    """
    from arr_diagnostics import sonarr_manual_import as smi

    captured: list[dict[str, object]] = []

    class _FakeClient:
        def get_json(self, path: str, params: dict[str, object] | None = None) -> object:
            captured.append({"path": path, "params": params or {}})
            return [
                {
                    "seriesId": 2644,
                    "downloadId": "abc",
                    "path": "/media/dload/Release.S04E26/file.mkv",
                    "episodes": [
                        {
                            "id": 172292,
                            "seasonNumber": 4,
                            "episodeNumber": 26,
                        },
                    ],
                    "seasonNumber": 4,
                },
            ]

        def post_json_documented_error(self, *_a: object, **_k: object) -> str:  # pragma: no cover - not used
            raise AssertionError("should not POST in prepare step")

    prep = smi._prepare_row(  # noqa: SLF001
        _FakeClient(),  # type: ignore[arg-type]
        "abc",
        2644,
        172292,
        season_number=4,
        episode_number=26,
        path_hints=None,
    )
    assert isinstance(prep, tuple)
    assert len(captured) == 1
    got = captured[0]
    assert got["path"] == "/manualimport"
    assert got["params"] == {"downloadId": "abc"}


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
