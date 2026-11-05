"""FastMCP server exposing plex_issues_* tools (plex.tv Reported Issues GraphQL)."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from plex_issues_diagnostics.client import (
    REPORT_BY_ID_QUERY,
    REPORT_COMMENTS_QUERY,
    REPORTS_QUERY,
    PlexIssuesClient,
    json_response,
    parse_comment,
    parse_report,
)
from plex_issues_diagnostics.util import safe_tool_decorator


def build_plex_issues_app(c: PlexIssuesClient) -> FastMCP:
    mcp = FastMCP("plex-issues-diagnostics")
    tool = safe_tool_decorator(mcp.tool)

    def _envelope(data: dict[str, Any], node_key: str, limit: int) -> dict[str, Any]:
        """Unwrap ``data[node_key]`` defensively; a null node means not found."""
        raw = (data or {}).get(node_key)
        if raw is None:
            return {"error": "not_found", "id": (data or {}).get("__id__", "")}
        return raw

    @tool()
    def plex_issues_list(limit: int = 25) -> str:
        """List the owner's Plex Reported Issues (newest first). Read-only."""
        limit = max(1, int(limit))
        nodes: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            variables: dict[str, Any] = {"first": min(limit, 100)}
            if after:
                variables["after"] = after
            data = c.query("getReportedIssues", REPORTS_QUERY, variables)
            reports = data.get("reports") or {}
            nodes.extend(reports.get("nodes") or [])
            if len(nodes) >= limit:
                break
            page = reports.get("pageInfo") or {}
            if not page.get("hasNextPage") or not page.get("endCursor"):
                break
            after = page["endCursor"]
        rows = [parse_report(n) for n in nodes[:limit]]
        return json_response({"issues": rows, "count": len(rows)})

    @tool()
    def plex_issues_get(id: str) -> str:
        """Fetch a single Reported Issue by id. Read-only."""
        data = c.query("reportByID", REPORT_BY_ID_QUERY, {"id": str(id)})
        node = data.get("reportByID")
        if node is None:
            return json_response({"error": "not_found", "id": str(id)})
        return json_response(parse_report(node))

    @tool()
    def plex_issues_comments(id: str, limit: int = 50) -> str:
        """Fetch comments on a Reported Issue. Read-only."""
        data = c.query(
            "reportComments",
            REPORT_COMMENTS_QUERY,
            {"id": str(id), "first": min(max(1, int(limit)), 100)},
        )
        rc = data.get("reportComments") or {}
        rows = [parse_comment(n) for n in (rc.get("nodes") or [])[: max(1, int(limit))]]
        return json_response({"comments": rows, "count": len(rows)})

    return mcp
