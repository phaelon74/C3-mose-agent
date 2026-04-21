"""Bash tool policy: allowlist for read-only `bash` tool; dangerous-pattern block for `sre_execute`."""

from __future__ import annotations

import re

# Block destructive / host-risk patterns even when operator approved sre_execute (defense in depth).
DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\brm\s+-rf\s+/\s*$",
        r"\brm\s+-rf\s+/\s+",
        r"\bmkfs\b",
        r"\bdd\s+if=",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\binit\s+0\b",
        r"\bsystemctl\s+(halt|poweroff|reboot)\b",
        r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;",  # fork bomb
        r"\b>\s*/dev/sda",
    ]
]

# Prefix allowlist for `bash` (read-only / observability). Anything else must use `sre_execute`.
# Patterns are matched against stripped one-line commands with re.match (anchored).
_BASH_ALLOWLIST: list[re.Pattern[str]] = [
    re.compile(p)
    for p in [
        r"^(echo|printf)\b",
        r"^pwd\b",
        r"^whoami\b",
        r"^(true|false)\s*$",
        r"^exit\s+\d+",
        r"^sleep\s+\d+",
        r"^env\b",
        r"^printenv\b",
        r"^(ls|dir)\b",
        r"^cat\s",
        r"^head\s",
        r"^tail\s",
        r"^grep\s",
        r"^sed\s",
        r"^awk\s",
        r"^sort\s",
        r"^uniq\s",
        r"^wc\s",
        r"^stat\s",
        r"^file\s",
        r"^find\s",
        r"^du\s",
        r"^df\b",
        r"^free\b",
        r"^mount\b",
        r"^uname\b",
        r"^hostname\b",
        r"^ps\b",
        r"^top\b",
        r"^htop\b",
        r"^ss\s",
        r"^netstat\b",
        r"^systemctl\s+status\b",
        r"^journalctl\b",
        r"^docker\s+(ps|logs|stats|inspect|images|network|volume|info|version|compose)\b",
        r"^curl\s",
        r"^wget\s",
        r"^ping\s",
        r"^nslookup\s",
        r"^dig\s",
        r"^ip\s+(addr|route|link|neigh|rule)\b",
        r"^python3?(\.\d+)?\s+[\w\./\\-]+\.py\b",
        r"^which\s",
        r"^type\s",
    ]
]


def is_dangerous_command(command: str) -> bool:
    """True if command matches a blocked destructive pattern."""
    for pat in DANGEROUS_PATTERNS:
        if pat.search(command):
            return True
    return False


def is_bash_allowlisted(command: str) -> bool:
    """True if `bash` may run this without requiring `sre_execute` instead."""
    c = command.strip()
    if not c or "\n" in c or "\r" in c:
        return False
    if is_dangerous_command(c):
        return False
    for pat in _BASH_ALLOWLIST:
        if pat.match(c):
            return True
    return False


def bash_rejection_message(command: str) -> str:
    """User-facing hint when bash is not allowlisted."""
    return (
        "This command is not allowed via `bash` (read-only allowlist). "
        "For state-changing or broader commands, use `sre_execute` with a clear reason and target_system — "
        "it will prompt for human approval before running.\n"
        f"Blocked command: {command!r}"
    )
