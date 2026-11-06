#!/usr/bin/env python3
"""Add the ``plex-issues`` server to the live portal MCP config.

Idempotent merge — only inserts what is missing, never touches existing
servers. Run on the mose host from the repo root::

    python3 scripts/add-plex-issues-portal.py
    docker compose build mose-mcp-portal
    docker compose up -d --force-recreate mose-mcp-portal
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ENTRY = {
    "command": "docker",
    "args": [
        "exec",
        "-i",
        "mose-plex-issues-diagnostics",
        "/usr/local/bin/mcp-entrypoint",
    ],
    "transport": "stdio",
}

TARGETS = [Path("mcp_servers.portal.json"), Path("mcp_servers.portal.example.json")]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    changed = False
    for rel in TARGETS:
        path = rel if Path(rel).exists() else root / rel
        if not path.exists():
            print(f"skip (missing): {path}")
            continue
        cfg = json.loads(path.read_text())
        servers = cfg.setdefault("servers", {})
        if "plex-issues" in servers:
            if servers["plex-issues"] == ENTRY:
                print(f"ok (already present): {rel}")
            else:
                servers["plex-issues"] = ENTRY
                path.write_text(json.dumps(cfg, indent=2) + "\n")
                print(f"updated (stale entry replaced): {rel}")
                changed = True
            continue
        servers["plex-issues"] = ENTRY
        path.write_text(json.dumps(cfg, indent=2) + "\n")
        print(f"added: {rel}")
        changed = True
    if not changed:
        print("nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
