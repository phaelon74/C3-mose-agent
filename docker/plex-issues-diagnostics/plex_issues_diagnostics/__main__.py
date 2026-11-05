"""Run the Plex Reported Issues MCP over stdio."""

from __future__ import annotations

import atexit
import os
import sys

from plex_issues_diagnostics.client import PlexIssuesClient
from plex_issues_diagnostics.mcp_server import build_plex_issues_app


def main() -> None:
    token = os.environ.get("PLEXTV_TOKEN", "").strip()
    if not token:
        print("mcp-entrypoint: set PLEXTV_TOKEN in the container environment.", file=sys.stderr)
        sys.exit(1)

    client = PlexIssuesClient(token)
    atexit.register(client.close)
    app = build_plex_issues_app(client)
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
