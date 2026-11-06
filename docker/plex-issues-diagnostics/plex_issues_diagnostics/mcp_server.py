"""FastMCP server exposing plex_issues_* tools (plex.tv Reported Issues GraphQL)."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from plex_issues_diagnostics.client import (
    CREATE_REPORT_COMMENT_MUTATION,
    REPORTS_QUERY,
    REPORT_BY_ID_QUERY,
    REPORT_COMMENTS_QUERY,
    json_response,
    parse_comment,
    parse_report,
)
from plex_issues_diagnostics.util import safe_tool_decorator


def _resolve_report_id(c: PlexIssuesClient, id: int | str) -> str | None:
    """Return the report UUID for ``id``.

    Accepts the UUID directly. Also accepts a ratingKey (int) — Mose passes these
    naturally — and resolves it by scanning the list feed for a matching row
    (one page of 100; newest-first so recent reports resolve immediately).
    Returns None when the id cannot be resolved.
    """
    s = str(id).strip()
    if not s:
        return None
    if "-" in s:  # UUID shape
        return s
    try:
        rating_key = int(s)
    except ValueError:
        return s  # opaque string; let reportByID decide
    data = c.query("getReportedIssues", REPORTS_QUERY, {"first": 100})
    for node in (data.get("reports") or {}).get("nodes") or []:
        row = parse_report(node)
        if row.get("ratingKey") == rating_key:
            return str(row.get("id") or "")
    return None


def build_plex_issues_app(c: PlexIssuesClient) -> FastMCP:
    mcp = FastMCP("plex-issues-diagnostics")
    tool = safe_tool_decorator(mcp.tool)

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
    def plex_issues_get(id: int | str) -> str:
        """Fetch a single Reported Issue by report UUID (or ratingKey int, auto-resolved via the list feed). Read-only."""
        rid = _resolve_report_id(c, id)
        if rid is None:
            return json_response({"error": "not_found", "id": str(id)})
        data = c.query("reportByID", REPORT_BY_ID_QUERY, {"id": rid})
        node = data.get("reportByID")
        if node is None:
            return json_response({"error": "not_found", "id": rid})
        return json_response(parse_report(node))

    @tool()
    def plex_issues_comments(id: int | str, limit: int = 50) -> str:
        """Fetch comments on a Reported Issue by report UUID (or ratingKey int, auto-resolved). Read-only."""
        rid = _resolve_report_id(c, id)
        if rid is None:
            return json_response({"error": "not_found", "id": str(id)})
        data = c.query(
            "reportComments",
            REPORT_COMMENTS_QUERY,
            {"id": rid, "first": min(max(1, int(limit)), 100)},
        )
        rc = data.get("reportComments") or {}
        rows = [parse_comment(n) for n in (rc.get("nodes") or [])[: max(1, int(limit))]]
        return json_response({"comments": rows, "count": len(rows)})

    @tool()
    def plex_issues_reply(id: int | str, message: str) -> str:
        """Post a comment reply on a Reported Issue (OWNER ACTION — requires admin approval)."""
        if not str(message).strip():
            return json.dumps({"error": "message_required", "detail": "Reply text is empty."})
        rid = _resolve_report_id(c, id)
        if rid is None:
            return json_response({"error": "not_found", "id": str(id)})
        data = c.query(
            "createReportComment",
            CREATE_REPORT_COMMENT_MUTATION,
            {"input": {"message": str(message), "report": rid}},
        )
        node = data.get("createReportComment")
        if node is None:
            return json_response({"error": "not_found", "id": rid})
        # Do NOT re-fetch via commentByID: it can 404 briefly ("Comment not found")
        # even though the mutation succeeded (captured live 2026-11-05).
        user = node.get("user") or {}
        return json_response({
            "id": node.get("id") or "",
            "status": node.get("status") or "",
            "date": node.get("date") or "",
            "message": node.get("message") or "",
            "user": user.get("username") or user.get("displayName") or "",
        })

    return mcp
