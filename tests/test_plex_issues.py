"""Tests for the plex-issues-diagnostics sidecar (fake GraphQL client)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "docker" / "plex-issues-diagnostics"

REPORT_URL = "server://09cd4bad4daabe621b1481d260ee32a2b13abcd3/com.plexapp.plugins.library/library/metadata/407269"


def _node(**over: object) -> dict:
    n = {
        "__typename": "Report",
        "id": "be6c0b58-1111-2222-3333-444455556666",
        "message": "Is there a english dubbed version available",
        "user": {"id": "u1", "username": "robert.f49", "displayName": "Robert F", "isMuted": False, "isBlocked": False},
        "url": REPORT_URL,
        "date": "2023-11-17T21:21:05Z",
        "commentCount": 2,
    }
    n.update(over)
    return n


class FakeClient:
    def __init__(self, pages: list[dict] | None = None, single: dict | None = None,
                 comments: dict | None = None, fail: Exception | None = None) -> None:
        self.pages = pages or []
        self.single = single
        self.comments = comments
        self.fail = fail
        self.calls: list[dict] = []

    def query(self, operation: str, query: str, variables: dict | None = None) -> dict:
        self.calls.append({"operation": operation, "variables": variables})
        if self.fail:
            raise self.fail
        if operation == "getReportedIssues":
            return self.pages.pop(0)
        if operation == "reportByID":
            return {"reportByID": self.single}
        if operation == "reportComments":
            return {"reportComments": self.comments}
        raise AssertionError(f"unexpected operation {operation}")


@pytest.fixture(scope="module", autouse=True)
def _prepend_pkg_path() -> None:
    p = str(PKG)
    if p not in sys.path:
        sys.path.insert(0, p)


def test_parse_report_extracts_rating_key() -> None:
    from plex_issues_diagnostics.client import parse_report

    row = parse_report(_node())
    assert row["ratingKey"] == 407269
    assert row["user"] == "robert.f49"
    assert row["message"] == "Is there a english dubbed version available"
    assert row["commentCount"] == 2


def test_parse_report_malformed_node_no_raise() -> None:
    from plex_issues_diagnostics.client import parse_report

    row = parse_report({"url": "weird", "user": None})
    assert row["ratingKey"] is None
    assert row["id"] == ""
    assert row["user"] == ""
    assert row["message"] == ""
    assert row["commentCount"] == 0


def test_list_respects_limit_and_pagination() -> None:
    from plex_issues_diagnostics.mcp_server import build_plex_issues_app

    page1 = {"reports": {"nodes": [_node(id=str(i)) for i in range(2)],
                         "pageInfo": {"hasNextPage": True, "endCursor": "CUR1"}}}
    page2 = {"reports": {"nodes": [_node(id="99")],
                         "pageInfo": {"hasNextPage": False, "endCursor": "CUR2"}}}
    c = FakeClient(pages=[page1, page2])
    app = build_plex_issues_app(c)
    fn = app._tool_manager._tools["plex_issues_list"].fn  # noqa: SLF001
    out = fn(limit=3)
    data = json.loads(out)
    assert data["count"] == 3
    assert [i["id"] for i in data["issues"]] == ["0", "1", "99"]
    assert c.calls[0]["variables"] == {"first": 3}
    assert c.calls[1]["variables"] == {"first": 3, "after": "CUR1"}


def test_list_empty_nodes_healthy() -> None:
    from plex_issues_diagnostics.mcp_server import build_plex_issues_app

    c = FakeClient(pages=[{"reports": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": "x"}}}])
    app = build_plex_issues_app(c)
    fn = app._tool_manager._tools["plex_issues_list"].fn  # noqa: SLF001
    data = json.loads(fn(limit=10))
    assert data == {"issues": [], "count": 0}


def test_get_returns_compact_row() -> None:
    from plex_issues_diagnostics.mcp_server import build_plex_issues_app

    c = FakeClient(single=_node())
    app = build_plex_issues_app(c)
    fn = app._tool_manager._tools["plex_issues_get"].fn  # noqa: SLF001
    data = json.loads(fn(id="be6c0b58"))
    assert data["ratingKey"] == 407269
    assert c.calls[0] == {"operation": "reportByID", "variables": {"id": "be6c0b58"}}


def test_get_not_found() -> None:
    from plex_issues_diagnostics.mcp_server import build_plex_issues_app

    c = FakeClient(single=None)
    app = build_plex_issues_app(c)
    fn = app._tool_manager._tools["plex_issues_get"].fn  # noqa: SLF001
    data = json.loads(fn(id="nope"))
    assert data == {"error": "not_found", "id": "nope"}


def test_comments_rows() -> None:
    from plex_issues_diagnostics.mcp_server import build_plex_issues_app

    c = FakeClient(comments={"nodes": [{"id": "c1", "message": "ack", "date": "2023-11-18",
                                        "status": "OPEN", "user": {"username": "plexstaff"}}],
                             "pageInfo": {"hasNextPage": False, "endCursor": "z"}})
    app = build_plex_issues_app(c)
    fn = app._tool_manager._tools["plex_issues_comments"].fn  # noqa: SLF001
    data = json.loads(fn(id="be6c0b58", limit=50))
    assert data["count"] == 1
    assert data["comments"][0] == {"id": "c1", "date": "2023-11-18", "message": "ack",
                                   "status": "OPEN", "user": "plexstaff"}


def test_graphql_http_error_becomes_json_not_raise() -> None:
    import httpx

    from plex_issues_diagnostics.mcp_server import build_plex_issues_app

    req = httpx.Request("POST", "https://community.plex.tv/api")
    resp = httpx.Response(401, request=req, text="unauthorized")
    c = FakeClient(fail=httpx.HTTPStatusError("401", request=req, response=resp))
    app = build_plex_issues_app(c)
    fn = app._tool_manager._tools["plex_issues_list"].fn  # noqa: SLF001
    data = json.loads(fn(limit=5))
    assert data["error"] == "http_error"
    assert data["http_status"] == 401


def test_runtime_graphql_errors_wrapped() -> None:
    from plex_issues_diagnostics.mcp_server import build_plex_issues_app

    c = FakeClient(fail=RuntimeError("graphql_errors: [{'message': 'bad'}]"))
    app = build_plex_issues_app(c)
    fn = app._tool_manager._tools["plex_issues_get"].fn  # noqa: SLF001
    data = json.loads(fn(id="x"))
    assert data["error"] == "tool_unhandled_exception"
    assert "graphql_errors" in data["detail"]
