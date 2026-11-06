"""Synchronous httpx client for the plex.tv Reported Issues GraphQL API."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

ENDPOINT = "https://community.plex.tv/api"

REPORTS_QUERY = (
    "query getReportedIssues($first: PaginationInt!, $after: String) { "
    "reports(after: $after, first: $first) { nodes { __typename id message "
    "user { id avatar username displayName isMuted isBlocked } url date commentCount } "
    "pageInfo { hasNextPage endCursor } } }"
)

REPORT_BY_ID_QUERY = (
    "query reportByID($id: ID!) { reportByID(id: $id) { __typename id message "
    "user { id avatar username displayName isMuted isBlocked } url date commentCount } }"
)

CREATE_REPORT_COMMENT_MUTATION = (
    "mutation createReportComment($input: CreateReportCommentInput!) { "
    "createReportComment(input: $input) { __typename date id message status "
    "user { id avatar username displayName isHidden isMuted isBlocked } } }"
)

REPORT_COMMENTS_QUERY = (
    "query reportComments($id: ID!, $first: PaginationInt, $after: String) { "
    "reportComments(id: $id, first: $first, after: $after) { nodes { id message "
    "user { username displayName } date status } pageInfo { hasNextPage endCursor } } }"
)

_RATING_KEY_RE = re.compile(r"/library/metadata/(\d+)/?$")


def parse_report(node: dict[str, Any]) -> dict[str, Any]:
    """Compact row for a Report node; missing keys degrade to empty, never raise."""
    user = node.get("user") or {}
    url = node.get("url") or ""
    m = _RATING_KEY_RE.search(url)
    return {
        "id": node.get("id") or "",
        "user": user.get("username") or user.get("displayName") or "",
        "date": node.get("date") or "",
        "message": node.get("message") or "",
        "ratingKey": int(m.group(1)) if m else None,
        "commentCount": node.get("commentCount") or 0,
    }


def parse_comment(node: dict[str, Any]) -> dict[str, Any]:
    user = node.get("user") or {}
    return {
        "id": node.get("id") or "",
        "date": node.get("date") or "",
        "message": node.get("message") or "",
        "status": node.get("status") or "",
        "user": user.get("username") or user.get("displayName") or "",
    }


def truncate_output(text: str, max_lines: int = 200, max_chars: int = 20000) -> str:
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[:max_lines]) + f"\n\n... truncated to {max_lines} lines"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n... truncated to {max_chars} characters"
    return text


def json_response(data: Any, max_chars: int = 20000) -> str:
    return truncate_output(json.dumps(data, indent=2, default=str), max_chars=max_chars)


class PlexIssuesClient:
    """POSTs captured GraphQL operations to ``community.plex.tv/api``."""

    def __init__(
        self,
        token: str = "",
        endpoint: str = ENDPOINT,
        timeout: float = 30.0,
    ) -> None:
        self.token = token.strip()
        self.endpoint = endpoint
        self.timeout = timeout
        self._http = httpx.Client(
            headers={
                "Content-Type": "application/json",
                "x-plex-token": self.token,
                "Origin": "https://app.plex.tv",
                "Referer": "https://app.plex.tv/",
                "User-Agent": "MosePlexIssues/1.0",
            },
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def query(self, operation: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST one GraphQL operation; raise on HTTP or GraphQL errors."""
        body = {"query": query, "variables": variables or {}, "operationName": operation}
        resp = self._http.post(self.endpoint, json=body)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"graphql_errors: {payload['errors']}")
        return payload.get("data") or {}
