"""Run Sonarr or Radarr diagnostics MCP over stdio: ``python -m arr_diagnostics sonarr|radarr``."""

from __future__ import annotations

import atexit
import os
import sys

from arr_diagnostics.client import ArrClient
from arr_diagnostics.radarr_mcp import build_radarr_app
from arr_diagnostics.sonarr_mcp import build_sonarr_app


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python -m arr_diagnostics sonarr|radarr", file=sys.stderr)
        sys.exit(2)
    kind = sys.argv[1].lower().strip()
    if kind == "sonarr":
        url = os.environ.get("SONARR_URL", "").strip()
        key = os.environ.get("SONARR_API_KEY", "").strip()
        label = "SONARR_URL / SONARR_API_KEY"
    elif kind == "radarr":
        url = os.environ.get("RADARR_URL", "").strip()
        key = os.environ.get("RADARR_API_KEY", "").strip()
        label = "RADARR_URL / RADARR_API_KEY"
    else:
        print("first argument must be sonarr or radarr", file=sys.stderr)
        sys.exit(2)
    if not url or not key:
        print(f"mcp-entrypoint: set {label} in the container environment.", file=sys.stderr)
        sys.exit(1)

    client = ArrClient(url, key)
    atexit.register(client.close)

    if kind == "sonarr":
        app = build_sonarr_app(client)
    else:
        app = build_radarr_app(client)

    app.run(transport="stdio")


if __name__ == "__main__":
    main()
