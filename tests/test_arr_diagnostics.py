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

    assert len(SONARR_COMMANDS) == 6


def test_post_episode_search_requires_ids() -> None:
    import json as _json

    from arr_diagnostics.sonarr_mcp import _post_episode_search_command

    class _C:
        def post_json(self, *_a: object, **_k: object) -> object:
            raise AssertionError("should not POST without episode ids")

    out = _post_episode_search_command(_C(), [])  # type: ignore[arg-type]
    assert _json.loads(out)["error"] == "episodeIds_required"


def test_series_lookup_requires_term() -> None:
    import json as _json

    from arr_diagnostics.sonarr_mcp import _get_series_lookup

    class _C:
        def get_json(self, *_a: object, **_k: object) -> object:
            raise AssertionError("should not GET without term")

    out = _get_series_lookup(_C(), "   \t")  # type: ignore[arg-type]
    assert _json.loads(out)["error"] == "term_required"


def test_series_lookup_passes_term_param() -> None:
    import json as _json

    from arr_diagnostics.sonarr_mcp import _get_series_lookup

    class _C:
        def __init__(self) -> None:
            self.last_params: dict[str, object] | None = None

        def get_json(self, path: str, params: dict[str, object] | None = None) -> object:
            self.last_params = params or {}
            return [{"title": "Example"}]

    client = _C()
    out = _get_series_lookup(client, "  Criminal Record  ")  # type: ignore[arg-type]
    assert _json.loads(out)[0]["title"] == "Example"
    assert client.last_params == {"term": "Criminal Record"}


def test_post_episode_search_posts_episode_ids() -> None:
    import json as _json

    from arr_diagnostics.sonarr_mcp import _post_episode_search_command

    class _C:
        def __init__(self) -> None:
            self.last_body: object | None = None

        def post_json(self, path: str, body: object | None = None) -> object:
            self.last_body = body
            return {"id": 1, "name": "EpisodeSearch"}

    client = _C()
    out = _post_episode_search_command(client, [10, 20])  # type: ignore[arg-type]
    assert _json.loads(out)["name"] == "EpisodeSearch"
    assert client.last_body == {"name": "EpisodeSearch", "episodeIds": [10, 20]}


def test_radarr_command_allowlist_length() -> None:
    from arr_diagnostics.radarr_mcp import RADARR_COMMANDS

    assert len(RADARR_COMMANDS) == 6


def test_policy_read_counts_match_plan() -> None:
    from mose import mcp_write_policy as mp

    assert len(mp._SONARR_DIAG_READS) == 26  # noqa: SLF001
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


def test_build_manual_import_command_file_extracts_expected_keys() -> None:
    """``/command`` ManualImport file payload carries only Sonarr's ManualImportFile fields."""
    from arr_diagnostics.sonarr_manual_import import build_manual_import_command_file

    validated_row = {
        "path": "/media/dload/X/file.mkv",
        "folderName": "X",
        "seriesId": 2644,
        "episodeFileId": 0,
        "quality": {"quality": {"id": 3}},
        "languages": [{"id": 1}],
        "releaseGroup": "VCR",
        "downloadId": "abc",
        "indexerFlags": 0,
        "releaseType": "singleEpisode",
        "customFormats": [{"id": 1}],
        "rejections": [],
        "id": 1234,
        "episodes": [{"id": 172292}],
    }
    file_payload = build_manual_import_command_file(validated_row, [172292])
    assert file_payload["path"] == "/media/dload/X/file.mkv"
    assert file_payload["seriesId"] == 2644
    assert file_payload["episodeIds"] == [172292]
    assert file_payload["downloadId"] == "abc"
    assert "rejections" not in file_payload
    assert "id" not in file_payload
    assert "episodes" not in file_payload


def test_manual_import_commit_fires_command_after_validation() -> None:
    """End-to-end: reprocess validates (rejections=[]) then POST /command is called."""
    import json as _json

    from arr_diagnostics import sonarr_manual_import as smi

    calls: list[tuple[str, object]] = []

    validated_row = {
        "path": "/media/dload/x/file.mkv",
        "folderName": "x",
        "seriesId": 2644,
        "quality": {"quality": {"id": 3}},
        "languages": [{"id": 1}],
        "releaseGroup": "VCR",
        "downloadId": "abc",
        "indexerFlags": 0,
        "releaseType": "singleEpisode",
        "customFormats": [],
        "rejections": [],
        "episodes": [{"id": 172292, "seasonNumber": 4, "episodeNumber": 26}],
    }

    class _Client:
        def get_json(self, path: str, params: dict[str, object] | None = None) -> object:
            calls.append(("GET", path))
            return [validated_row]

        def post_json_documented_error(self, path: str, body: object | None = None) -> str:
            calls.append(("POST", path))
            if path == "/manualimport":
                return _json.dumps([validated_row])
            if path == "/command":
                return _json.dumps({"id": 9999, "name": "ManualImport", "status": "queued"})
            raise AssertionError(f"unexpected POST {path}")

    out = smi.manual_import_commit(
        _Client(),  # type: ignore[arg-type]
        {
            "downloadId": "abc",
            "seriesId": 2644,
            "episodeIds": [172292],
            "seasonNumber": 4,
            "episodeNumber": 26,
        },
    )
    assert "ManualImport" in out or "queued" in out
    assert ("POST", "/manualimport") in calls
    assert ("POST", "/command") in calls


def test_manual_import_commit_halts_on_rejections() -> None:
    """If reprocess returns rejections, we must NOT POST /command."""
    import json as _json

    from arr_diagnostics import sonarr_manual_import as smi

    posts: list[str] = []

    class _Client:
        def get_json(self, path: str, params: dict[str, object] | None = None) -> object:
            return [
                {
                    "path": "/x/y.mkv",
                    "seriesId": 2644,
                    "episodes": [{"id": 172292, "seasonNumber": 4, "episodeNumber": 26}],
                    "rejections": [],
                },
            ]

        def post_json_documented_error(self, path: str, body: object | None = None) -> str:
            posts.append(path)
            if path == "/manualimport":
                return _json.dumps([
                    {
                        "path": "/x/y.mkv",
                        "seriesId": 2644,
                        "episodes": [{"id": 172292}],
                        "rejections": [{"reason": "Unknown series", "type": "permanent"}],
                    },
                ])
            raise AssertionError(f"should not POST {path} when rejected")

    out = smi.manual_import_commit(
        _Client(),  # type: ignore[arg-type]
        {
            "downloadId": "abc",
            "seriesId": 2644,
            "episodeIds": [172292],
            "seasonNumber": 4,
            "episodeNumber": 26,
        },
    )
    data = _json.loads(out)
    assert data.get("error") == "manualimport_rejected"
    assert posts == ["/manualimport"]


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


def test_radarr_manual_import_missing_scope_error() -> None:
    import json as _json

    from arr_diagnostics.radarr_mcp import radarr_manual_import_missing_scope_error

    raw = radarr_manual_import_missing_scope_error(None, None, None)
    assert raw is not None
    assert _json.loads(raw)["error"] == "missing_scope"
    assert radarr_manual_import_missing_scope_error("/f", None, None) is None
    assert radarr_manual_import_missing_scope_error(None, "dl-1", None) is None
    assert radarr_manual_import_missing_scope_error(None, None, 1) is None


def test_radarr_prepare_row_queries_downloadid_only() -> None:
    """GET /manualimport must use ``downloadId`` alone (no ``movieId`` on GET)."""
    from arr_diagnostics import radarr_manual_import as rmi

    captured: list[dict[str, object]] = []

    class _FakeClient:
        def get_json(self, path: str, params: dict[str, object] | None = None) -> object:
            captured.append({"path": path, "params": params or {}})
            return [
                {
                    "movieId": 42,
                    "downloadId": "abc",
                    "path": "/media/dload/Gremlins.2.1990/foo.mkv",
                    "quality": {"quality": {"id": 1}},
                    "languages": [{"id": 1}],
                    "rejections": [],
                },
            ]

        def post_json_documented_error(self, *_a: object, **_k: object) -> str:  # pragma: no cover
            raise AssertionError("should not POST in prepare step")

    prep = rmi._prepare_row(  # noqa: SLF001
        _FakeClient(),  # type: ignore[arg-type]
        "abc",
        42,
        path_hints=None,
    )
    assert isinstance(prep, tuple)
    assert len(captured) == 1
    assert captured[0]["path"] == "/manualimport"
    assert captured[0]["params"] == {"downloadId": "abc"}


def test_radarr_manual_import_commit_success() -> None:
    import json as _json

    from arr_diagnostics import radarr_manual_import as rmi

    calls: list[tuple[str, object]] = []

    validated_row = {
        "path": "/media/dload/x/movie.mkv",
        "folderName": "x",
        "movieId": 99,
        "quality": {"quality": {"id": 3}},
        "languages": [{"id": 1}],
        "releaseGroup": "GRP",
        "downloadId": "dl1",
        "indexerFlags": 0,
        "rejections": [],
    }

    class _Client:
        def get_json(self, path: str, params: dict[str, object] | None = None) -> object:
            calls.append(("GET", path, params or {}))
            return [validated_row]

        def post_json_documented_error(self, path: str, body: object | None = None) -> str:
            calls.append(("POST", path, body))
            if path == "/manualimport":
                return _json.dumps([validated_row])
            if path == "/command":
                return _json.dumps({"id": 7, "name": "ManualImport", "status": "queued"})
            raise AssertionError(f"unexpected POST {path}")

    out = rmi.manual_import_commit(
        _Client(),  # type: ignore[arg-type]
        {"downloadId": "dl1", "movieId": 99},
    )
    assert "ManualImport" in out or "queued" in out
    assert calls[0][0] == "GET" and calls[0][1] == "/manualimport"
    assert calls[0][2] == {"downloadId": "dl1"}
    assert ("POST", "/manualimport") in [(c[0], c[1]) for c in calls]
    assert ("POST", "/command") in [(c[0], c[1]) for c in calls]
    mi_body = next(c[2] for c in calls if c[0] == "POST" and c[1] == "/manualimport")
    assert isinstance(mi_body, list) and len(mi_body) == 1
    cmd_body = next(c[2] for c in calls if c[0] == "POST" and c[1] == "/command")
    assert cmd_body["name"] == "ManualImport"
    assert cmd_body["importMode"] == "auto"
    assert len(cmd_body["files"]) == 1
    assert cmd_body["files"][0]["movieId"] == 99


def test_radarr_manual_import_commit_halts_on_rejections() -> None:
    import json as _json

    from arr_diagnostics import radarr_manual_import as rmi

    posts: list[str] = []

    class _Client:
        def get_json(self, path: str, params: dict[str, object] | None = None) -> object:
            return [
                {
                    "path": "/x/y.mkv",
                    "movieId": 1,
                    "downloadId": "d",
                    "quality": {"quality": {"id": 1}},
                    "languages": [{"id": 1}],
                    "rejections": [],
                },
            ]

        def post_json_documented_error(self, path: str, body: object | None = None) -> str:
            posts.append(path)
            if path == "/manualimport":
                return _json.dumps([
                    {
                        "path": "/x/y.mkv",
                        "movieId": 1,
                        "downloadId": "d",
                        "rejections": [{"reason": "blocked", "type": "permanent"}],
                    },
                ])
            raise AssertionError(f"should not POST {path} when rejected")

    out = rmi.manual_import_commit(_Client(), {"downloadId": "d", "movieId": 1})  # type: ignore[arg-type]
    data = _json.loads(out)
    assert data.get("error") == "manualimport_rejected"
    assert posts == ["/manualimport"]


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


def test_safe_tool_converts_http_status_error_to_json() -> None:
    """A tool raising ``httpx.HTTPStatusError`` must return JSON, not re-raise.

    This is what keeps a single Sonarr 4xx/5xx from killing the MCP stdio
    session and poisoning every subsequent tool call with
    ``anyio.ClosedResourceError``.
    """
    import json as _json

    import httpx

    from arr_diagnostics.client import safe_tool

    @safe_tool
    def boom() -> str:
        req = httpx.Request("GET", "http://x/api/v3/queue/details")
        resp = httpx.Response(500, text="kaboom", request=req)
        raise httpx.HTTPStatusError("500", request=req, response=resp)

    out = boom()
    parsed = _json.loads(out)
    assert parsed["error"] == "http_error"
    assert parsed["http_status"] == 500
    assert parsed["tool"] == "boom"
    assert "kaboom" in parsed["body"]


def test_safe_tool_converts_transport_error_to_json() -> None:
    """Connection errors (e.g. Sonarr not listening) become JSON, not a crash."""
    import json as _json

    import httpx

    from arr_diagnostics.client import safe_tool

    @safe_tool
    def boom() -> str:
        raise httpx.ConnectError("connection refused")

    parsed = _json.loads(boom())
    assert parsed["error"] == "transport_error"
    assert "connection refused" in parsed["detail"]


def test_safe_tool_preserves_signature_for_fastmcp_introspection() -> None:
    """FastMCP builds tool JSON schema from the wrapped function's signature.

    ``functools.wraps`` sets ``__wrapped__`` so ``inspect.signature`` (used by
    FastMCP with ``follow_wrapped=True`` by default) sees the original params.
    """
    import inspect

    from arr_diagnostics.client import safe_tool

    @safe_tool
    def my_tool(series_id: int, season: int | None = None) -> str:
        return "ok"

    sig = inspect.signature(my_tool)
    assert list(sig.parameters.keys()) == ["series_id", "season"]
    assert sig.parameters["series_id"].annotation is int
