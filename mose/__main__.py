"""Entry point: python -m mose [--skill-review]"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

from mose.config import assert_signal_account_requires_groups, load_config, signal_runtime_ready
from mose.observe import setup_logging, get_logger, log_event
from mose.llm import create_llm_client
from mose.memory import MemoryManager
from mose.mcp_manager import MCPManager
from mose.agent import Agent
from mose.learning import (
    handle_skill_decision,
    init_skill_decision_runtime,
    init_skill_promotion,
    init_skill_recovery_notice,
    init_skill_reminder,
    init_skill_review,
)
from mose.tools import (
    execute_mcp_tool,
    init_approval,
    init_skills_dir,
    init_terminal,
    init_tool_registry,
    init_tracker_propose_callback,
    init_workspace,
)
from mose.trackers import (
    TrackerScheduler,
    default_plex_codemode_collector,
    default_plex_cpu_monitor_collector,
    default_plex_viewers_collector,
    init_tracker_alert_callback,
    unwrap_codemode_portal_response,
    parse_collector_json,
)
from mose.tracker_decision import handle_tracker_decision, init_tracker_decision_runtime
from mose.task_decision import (
    format_task_proposal_message,
    handle_task_decision,
    init_task_decision_runtime,
    init_task_propose_callback,
)
from mose.task_scheduler import (
    init_task_delivery_callback,
    init_task_failure_callback,
)
from mose.playbook_decision import (
    format_playbook_notification_body,
    handle_playbook_decision,
    init_playbook_decision_runtime,
    init_playbook_propose_callback,
    init_playbook_run_delivery_callback,
    init_playbook_run_reject_callback,
)
from mose.upcoming import (
    init_upcoming_failure_notify,
    init_upcoming_notify,
    run_daily_sync,
    run_weekly_recommend,
)
from mose.upcoming_decision import handle_upcoming_decision, init_upcoming_delivery_callback
from mose.upcoming_store import UpcomingStore


async def _maybe_start_portal_approval_bridge(config) -> Any:
    """Start POST /approve when ``[portal].enabled`` (mutating Code Mode MCP calls)."""
    if not config.portal.enabled:
        return None
    from mose.approval_bridge import start_approval_bridge

    return await start_approval_bridge(config.portal)


async def _cli_tracker_propose_callback(slug: str, description: str, expires_at: float) -> None:
    from datetime import datetime, timezone

    exp = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(timespec="minutes")
    print(
        f"\n[tracker proposal] {slug}\n"
        f"  {description}\n"
        f"  Expires: {exp} UTC\n"
        f"  Decide: python -m mose --decide {slug} y|n\n"
    )


async def _cli_tracker_alert(tracker: Any, message: str) -> None:
    slug = getattr(tracker, "slug", "tracker")
    print(f"\n[tracker alert:{slug}]\n{message}\n")


async def _cli_task_propose_callback(
    slug: str, description: str, expires_at: float, payload: dict[str, Any]
) -> None:
    from datetime import datetime, timezone

    from mose.config import load_config

    cfg = load_config()
    exp = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(timespec="minutes")
    body = format_task_proposal_message(payload, timezone=cfg.scheduler.timezone)
    print(
        f"\n[scheduled task proposal] {slug}\n"
        f"{body}\n"
        f"  Expires: {exp} UTC\n"
        f"  Decide: python -m mose --decide {slug} y|n\n"
    )


async def _cli_task_delivery(task: Any, recipient: str, body: str) -> None:
    slug = getattr(task, "slug", "task")
    print(f"\n[scheduled task:{slug} -> {recipient}]\n{body}\n")


async def _cli_task_failure(task: Any, message: str) -> None:
    slug = getattr(task, "slug", "task")
    print(f"\n[scheduled task FAILURE:{slug}]\n{message}\n")


async def _cli_playbook_propose_callback(
    slug: str, description: str, expires_at: float, payload: dict[str, Any]
) -> None:
    from datetime import datetime, timezone

    exp = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(timespec="minutes")
    body = format_playbook_notification_body(payload)
    print(
        f"\n[playbook proposal] {slug}\n"
        f"{body}\n"
        f"  Expires: {exp} UTC\n"
        f"  Decide: python -m mose --decide {slug} y|n\n"
    )


async def _cli_playbook_run_delivery(run_slug: str, body: str, payload: dict[str, Any]) -> None:
    playbook_slug = str(payload.get("playbook_slug") or "playbook")
    print(f"\n[playbook run:{playbook_slug} -> {run_slug}]\n{body}\n")


async def _cli_playbook_run_reject(slug: str, payload: dict[str, Any]) -> None:
    playbook_slug = str(payload.get("playbook_slug") or "playbook")
    print(f"\n[playbook run rejected] {playbook_slug} / {slug}\n")


async def _cli_skill_propose_callback(
    path: str, slug: str, title: str, description: str, rationale: str, expires_at: float
) -> None:
    """CLI proposal notification. Prompts inline and resolves the decision.

    Unlike the Signal path, the CLI is an interactive foreground process, so
    we can safely ask synchronously here and call ``handle_skill_decision``
    before returning. The durable row still exists — if the user aborts with
    Ctrl-C the proposal will be swept on next startup.
    """
    from datetime import datetime, timezone
    expires_str = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(timespec="minutes")
    print(
        "\n[skill proposal] The agent would like to build a new skill:\n"
        f"  Slug:        {slug}\n"
        f"  Title:       {title}\n"
        f"  Description: {description}\n"
        f"  Rationale:   {rationale}\n"
        f"  Proposal:    {path}\n"
        f"  Expires:     {expires_str} (UTC)\n"
    )
    loop = asyncio.get_event_loop()
    try:
        response = await loop.run_in_executor(None, lambda: input("Approve build? [y/N]: "))
    except (EOFError, KeyboardInterrupt):
        print("(deferred; decide later with 'python -m mose --decide <slug> [y|n]')")
        return
    approved = response.strip().lower() in ("y", "yes")
    applied = await handle_skill_decision(slug, approved=approved)
    if applied:
        print(f"[skill proposal] {slug}: {'built' if approved else 'rejected'}.")


def _cli_skill_review_notify(report_path: str, summary: str) -> None:
    """CLI notification after a skill review completes."""
    print("\n[skill review] complete")
    print(f"  Report: {report_path}")
    for line in summary.splitlines():
        print(f"  {line}")


def _cli_upcoming_notify(summary: str, report_path: str, attachment_path: str | None = None) -> None:
    print("\n[upcoming media]")
    print(f"  Report: {report_path}")
    if attachment_path:
        print(f"  Attachment: {attachment_path}")
    for line in summary.splitlines():
        print(f"  {line}")


def _cli_upcoming_failure(kind: str, message: str, _unused: str | None = None) -> None:
    print(f"\n[upcoming {kind} FAILURE]\n{message}\n")


async def _cli_upcoming_delivery(slug: str, body: str) -> None:
    print(f"\n[upcoming add:{slug}]\n{body}\n")


async def _cli_skill_recovery_notice(
    still_pending, expired_while_down, approved_unbuilt
) -> None:
    """Print outstanding skill approvals at startup.

    ``still_pending`` invites a decision via ``--decide <slug> y|n``.
    ``expired_while_down`` is informational (already in ``skills/rejected/``).
    ``approved_unbuilt`` warns that a build is queued — stop with
    ``--decide <slug> cancel``.
    """
    if not still_pending and not expired_while_down and not approved_unbuilt:
        return
    from datetime import datetime, timezone

    def _fmt(epoch: float) -> str:
        try:
            return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat(timespec="minutes")
        except (OverflowError, OSError, ValueError):
            return str(epoch)

    def _title(row) -> str:
        return (row.payload or {}).get("title", row.slug) if isinstance(row.payload, dict) else row.slug

    print("\n[startup recovery] outstanding skill approvals")
    if still_pending:
        print(f"  Still pending ({len(still_pending)}) — decide with 'python -m mose --decide <slug> y|n':")
        for row in still_pending:
            print(f"    - {row.slug}  ({_title(row)})  expires {_fmt(row.expires_at)} UTC")
    if approved_unbuilt:
        print(
            f"  Approved but not yet built ({len(approved_unbuilt)}) — "
            "build will auto-start after the grace window. "
            "Stop with 'python -m mose --decide <slug> cancel':"
        )
        for row in approved_unbuilt:
            print(f"    - {row.slug}  ({_title(row)})")
    if expired_while_down:
        print(f"  Expired while down ({len(expired_while_down)}) — moved to skills/rejected/, no action needed:")
        for row in expired_while_down:
            print(f"    - {row.slug}  ({_title(row)})  expired {_fmt(row.expires_at)} UTC")
    print()


async def _cli_approval_callback(command: str, reason: str, target_system: str) -> bool:
    """Prompt user for approval via stdin. Used in CLI mode."""
    print(f"\n[sre_execute] Approval required")
    print(f"  System: {target_system}")
    print(f"  Reason: {reason}")
    print(f"  Command: {command}")
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(None, lambda: input("Approve? [y/N]: "))
    return response.strip().lower() in ("y", "yes")


def _format_tool_args(name: str, arguments: str) -> str:
    """Extract a short summary from tool call arguments."""
    try:
        args = json.loads(arguments) if arguments else {}
    except (json.JSONDecodeError, TypeError):
        args = {}

    if name == "bash" and "command" in args:
        return args["command"]
    if name == "sre_execute" and "command" in args:
        return args["command"]
    if name in ("read_file", "write_file") and "path" in args:
        return args["path"]
    if name == "list_directory" and "path" in args:
        return args["path"]
    if name == "web_search" and "query" in args:
        return args["query"]
    if name == "web_fetch" and "url" in args:
        return args["url"]
    if name in ("delegate", "code_task") and "task" in args:
        return args["task"]

    # Fallback: first string value or raw length
    for v in args.values():
        if isinstance(v, str) and v:
            return v[:80]
    return f"({len(arguments)} chars)" if arguments else ""


def _print_tool_call(name: str, arguments: str, result: str) -> None:
    """Print a tool call inline during CLI mode."""
    summary = _format_tool_args(name, arguments)
    # Truncate summary to 120 chars
    if len(summary) > 120:
        summary = summary[:117] + "..."
    print(f"  [{name}] {summary}")

    # Show first non-empty line of result as preview
    preview = ""
    for line in result.splitlines():
        stripped = line.strip()
        if stripped:
            preview = stripped
            break
    if preview:
        if len(preview) > 120:
            preview = preview[:117] + "..."
        print(f"  -> {preview}")


async def _run_cli(agent: Agent) -> None:
    """Interactive CLI REPL for testing without Discord."""
    session_id = f"cli-{int(time.time())}"
    print("Mose CLI (type 'exit' or Ctrl+D to quit)")
    print(f"Session: {session_id}\n")

    loop = asyncio.get_event_loop()
    while True:
        try:
            user_input = await loop.run_in_executor(None, input, "mose> ")
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input.strip():
            continue
        if user_input.strip().lower() in ("exit", "quit"):
            break

        try:
            response = await agent.process(user_input.strip(), session_id)
            print(f"\n{response}\n")
        except Exception as e:
            print(f"\nError: {e}\n")


def _run_list_trackers_cli(config) -> int:
    memory = MemoryManager(config.memory)
    rows = memory.list_trackers(enabled_only=False)
    memory.close()
    out = [
        {
            "slug": t.slug,
            "enabled": t.enabled,
            "schedule_seconds": t.schedule_seconds,
            "last_status": t.last_status,
        }
        for t in rows
    ]
    print(json.dumps(out, indent=2))
    return 0


def _run_seed_tracker_cli(config, name: str) -> int:
    memory = MemoryManager(config.memory)
    try:
        if memory.get_tracker(name):
            print(f"tracker '{name}' already exists")
            return 1
        seeds: dict[str, dict] = {
            "plex_streams": {
                "slug": "plex_streams",
                "description": "Plex active sessions / transcodes (seeded)",
                "collector_ref": default_plex_codemode_collector(),
                "schedule_seconds": config.trackers.default_schedule_seconds,
                "aggregations": ["streams", "transcodes"],
                "alert_rules": [
                    {
                        "id": "streams_record",
                        "type": "new_daily_high",
                        "metric": "streams_daily_max",
                        "lookback_days": 30,
                    }
                ],
            },
            "plex_cpu_monitor": {
                "slug": "plex-cpu-monitor",
                "description": "Plex host/process CPU and memory (latest resources sample)",
                "collector_ref": default_plex_cpu_monitor_collector(),
                "schedule_seconds": config.trackers.default_schedule_seconds,
                "aggregations": [
                    "host_cpu_pct",
                    "host_memory_pct",
                    "process_cpu_pct",
                    "process_memory_pct",
                ],
                "alert_rules": [],
            },
            "plex_viewers": {
                "slug": "plex-viewers",
                "description": "Plex active viewers, transcodes, bandwidth, per-session snapshot",
                "collector_ref": default_plex_viewers_collector(),
                "schedule_seconds": config.trackers.default_schedule_seconds,
                "aggregations": ["viewers", "transcodes", "direct_plays", "total_bandwidth_mbps"],
                "alert_rules": [],
            },
        }
        spec = seeds.get(name)
        if spec is None:
            print(f"Unknown seed name '{name}'. Try: {', '.join(sorted(seeds))}")
            return 2
        memory.create_tracker(
            slug=spec["slug"],
            description=spec["description"],
            collector_kind="codemode",
            collector_ref=spec["collector_ref"],
            schedule_seconds=spec["schedule_seconds"],
            aggregations=spec["aggregations"],
            alert_rules=spec["alert_rules"],
            recipients=[config.trackers.default_recipient],
        )
        print(f"Seeded tracker '{spec['slug']}' (approve not required — direct insert).")
        return 0
    finally:
        memory.close()


def _run_apply_plex_trackers_cli(config) -> int:
    """Patch plex-cpu-monitor and plex-viewers collectors from built-in templates."""
    memory = MemoryManager(config.memory)
    schedule = config.trackers.default_schedule_seconds
    try:
        updates = [
            ("plex-cpu-monitor", default_plex_cpu_monitor_collector()),
            ("plex-viewers", default_plex_viewers_collector()),
        ]
        applied: list[str] = []
        missing: list[str] = []
        for slug, ref in updates:
            if memory.get_tracker(slug) is None:
                missing.append(slug)
                continue
            memory.update_tracker(
                slug,
                collector_ref=ref,
                collector_kind="codemode",
                schedule_seconds=schedule,
                consecutive_failures=0,
                last_status=None,
                enabled=True,
            )
            applied.append(slug)
        print(json.dumps({"applied": applied, "missing": missing, "schedule_seconds": schedule}, indent=2))
        return 0 if applied else (2 if missing else 1)
    finally:
        memory.close()


def _run_apply_tracker_schedule_cli(config, seconds: int | None) -> int:
    """Set schedule_seconds on all tracker rows."""
    schedule = config.trackers.default_schedule_seconds if seconds is None else seconds
    schedule = max(5, min(int(schedule), 86400))
    memory = MemoryManager(config.memory)
    try:
        updated: list[dict[str, int | str]] = []
        for t in memory.list_trackers(enabled_only=False):
            before = t.schedule_seconds
            memory.update_tracker(t.slug, schedule_seconds=schedule)
            updated.append({"slug": t.slug, "before": before, "after": schedule})
        print(json.dumps({"schedule_seconds": schedule, "updated": updated}, indent=2))
        return 0
    finally:
        memory.close()


def _run_tracker_compact_cli(config, *, vacuum: bool) -> int:
    memory = MemoryManager(config.memory)
    try:
        stats = memory.compact_tracker_storage(
            sample_retention_days=config.trackers.sample_retention_days,
            rollup_retention_days=config.trackers.rollup_retention_days,
            vacuum=vacuum,
        )
        print(json.dumps(stats, indent=2))
        return 0
    finally:
        memory.close()


async def _init_mcp_for_cli(config) -> MCPManager:
    mcp = MCPManager()
    mcp_config_path = config.root_dir / "mcp_servers.json"
    await mcp.load_servers(mcp_config_path)
    init_tool_registry(mcp, config)
    return mcp


async def _run_debug_tracker_collector_cli(config, slug: str) -> int:
    """Run one collector via Code Mode and print raw portal + parsed output."""
    memory = MemoryManager(config.memory)
    tr = memory.get_tracker(slug)
    if tr is None:
        print(f"Error: unknown tracker '{slug}'", file=sys.stderr)
        memory.close()
        return 2
    mcp = await _init_mcp_for_cli(config)
    try:

        async def _exec_codemode(code: str, timeout: int) -> tuple[str, bool]:
            return await execute_mcp_tool(
                "mcp-portal__portal_codemode_execute",
                {"code": code, "timeout_seconds": min(120, max(5, int(timeout)))},
            )

        text, is_err = await _exec_codemode(
            tr.collector_ref,
            min(120, max(10, int(config.trackers.code_timeout_seconds))),
        )
        report: dict[str, Any] = {
            "slug": slug,
            "collector_kind": tr.collector_kind,
            "mcp_is_error": is_err,
            "raw_preview": text[:2000],
            "raw_len": len(text or ""),
        }
        try:
            stdout = unwrap_codemode_portal_response(text)
            report["stdout_preview"] = stdout[:2000]
            report["stdout_len"] = len(stdout or "")
            parsed = parse_collector_json(stdout)
            report["parsed_metrics_keys"] = sorted((parsed.get("metrics") or {}).keys())
            report["parse_ok"] = True
        except Exception as e:
            report["parse_ok"] = False
            report["error"] = str(e)
        print(json.dumps(report, indent=2))
        return 0 if report.get("parse_ok") else 1
    finally:
        await mcp.close()
        memory.close()


async def _run_tracker_run_now_cli(config, slug: str) -> int:
    """Run one tracker tick with MCP (same path as the live agent scheduler)."""
    memory = MemoryManager(config.memory)
    mcp = await _init_mcp_for_cli(config)
    try:

        async def _exec_codemode(code: str, timeout: int) -> tuple[str, bool]:
            return await execute_mcp_tool(
                "mcp-portal__portal_codemode_execute",
                {"code": code, "timeout_seconds": min(120, max(5, int(timeout)))},
            )

        sch = TrackerScheduler(memory, config.trackers, execute_codemode=_exec_codemode)
        msg = await sch.run_once(slug)
        print(msg)
        tr = memory.get_tracker(slug)
        if tr is not None:
            print(json.dumps({"last_status": tr.last_status, "consecutive_failures": tr.consecutive_failures}, indent=2))
        return 0 if "completed" in msg else 1
    finally:
        await mcp.close()
        memory.close()


async def _run_scheduled_task_run_now_cli(config, slug: str) -> int:
    """Run one scheduled task via the full agent loop (MCP + LLM + tool allowlist)."""
    from mose.agent import Agent

    init_workspace(config.agent.workspace, config.agent.allow_read_outside)
    init_skills_dir(config.agent.skills_path)
    memory = MemoryManager(config.memory)
    task = memory.get_scheduled_task(slug)
    if task is None:
        print(f"Error: unknown scheduled task '{slug}'", file=sys.stderr)
        memory.close()
        return 2
    mcp = await _init_mcp_for_cli(config)
    llm = create_llm_client(config.llm)
    agent = Agent(config, llm, memory, mcp)
    try:
        result = await agent.run_scheduled_task(task)
        print(json.dumps(result, indent=2))
        task2 = memory.get_scheduled_task(slug)
        if task2 is not None:
            print(
                json.dumps(
                    {
                        "slug": slug,
                        "last_status": task2.last_status,
                        "consecutive_failures": task2.consecutive_failures,
                    },
                    indent=2,
                )
            )
        return 0 if result.get("status") == "ok" else 1
    finally:
        await mcp.close()
        memory.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mose", description="Mose SRE/DevOps agent")
    parser.add_argument(
        "--skill-review",
        action="store_true",
        help="Run a one-shot skill-quality review, write the report, and exit.",
    )
    parser.add_argument(
        "--skill-review-no-notify",
        action="store_true",
        help="With --skill-review: do not send the summary via Signal.",
    )
    parser.add_argument(
        "--decide",
        nargs="+",
        metavar="ARG",
        help="Resolve a pending approval: SLUG DECISION [LINES]. "
             "DECISION is 'approve' / 'yes' / 'y', 'reject' / 'no' / 'n', "
             "or 'cancel' / 'stop'. Optional LINES (e.g. 1,4,7) for upcoming-media subset.",
    )
    parser.add_argument(
        "--sweep-approvals",
        action="store_true",
        help="Run the pending-approvals sweep (expire stale, remind admin) and exit.",
    )
    parser.add_argument(
        "--tracker-compact",
        action="store_true",
        help="Run one-shot tracker sample/rollup retention cleanup and exit.",
    )
    parser.add_argument(
        "--tracker-compact-vacuum",
        action="store_true",
        help="With --tracker-compact: also run VACUUM (can be slow).",
    )
    parser.add_argument(
        "--list-trackers",
        action="store_true",
        help="Print configured trackers as JSON and exit.",
    )
    parser.add_argument(
        "--seed-tracker",
        metavar="NAME",
        help="Insert a built-in tracker row (e.g. plex_streams, plex_cpu_monitor, plex_viewers) and exit.",
    )
    parser.add_argument(
        "--apply-plex-trackers",
        action="store_true",
        help="Update plex-cpu-monitor and plex-viewers collector_ref and schedule from built-in templates and exit.",
    )
    parser.add_argument(
        "--apply-tracker-schedule",
        nargs="?",
        type=int,
        const=-1,
        default=None,
        metavar="SECONDS",
        help="Set schedule_seconds on all trackers (default: config default_schedule_seconds) and exit.",
    )
    parser.add_argument(
        "--tracker-run-now",
        metavar="SLUG",
        help="Run one tracker tick via Code Mode (loads MCP; does not need the live agent).",
    )
    parser.add_argument(
        "--debug-tracker-collector",
        metavar="SLUG",
        help="Run a tracker's collector once and print raw portal output + parse diagnostics.",
    )
    parser.add_argument(
        "--scheduled-task-run-now",
        metavar="SLUG",
        help="Run one scheduled task immediately (full agent loop; does not need live Signal bot).",
    )
    parser.add_argument(
        "--upcoming-sync",
        action="store_true",
        help="Run one upcoming-media daily sync (TMDB + Radarr/Sonarr snapshot) and exit.",
    )
    parser.add_argument(
        "--upcoming-recommend",
        action="store_true",
        help="Run the weekly upcoming-media recommend job (syncs first if stale) and exit.",
    )
    parser.add_argument(
        "--upcoming-attach-test",
        metavar="PATH",
        help="Send a test Markdown attachment to the Signal admin group and exit.",
    )
    return parser.parse_args(argv)


async def _run_decide_once(config, slug: str, decision: str, line_spec: str | None = None) -> int:
    """Apply a skill-proposal decision from the CLI (used by operator scripts).

    ``decision`` may be approve/yes/y, reject/no/n/deny, or cancel/stop/
    abort/halt (abort an approved-but-unbuilt build during its grace window).
    """
    verb = decision.strip().lower()
    if verb in ("approve", "yes", "y"):
        action = "approve"
    elif verb in ("reject", "no", "n", "deny"):
        action = "reject"
    elif verb in ("cancel", "stop", "abort", "halt"):
        action = "cancel"
    else:
        print(f"Unknown decision '{decision}'. Use approve/yes/y, reject/no/n, or cancel/stop.")
        return 2

    llm = create_llm_client(config.llm)
    memory = MemoryManager(config.memory)
    init_workspace(config.agent.workspace, config.agent.allow_read_outside)
    init_skills_dir(config.agent.skills_path)

    from mose.learning import SkillLearner
    learner = SkillLearner(
        config.learning,
        Path(config.agent.skills_path),
        log_dir=Path(config.learning.review_log_dir),
        proposal_timeout_seconds=int(config.signal.proposal_timeout_seconds),
    )

    if action == "cancel":
        applied = learner.cancel_approved_build(slug, memory)
        memory.close()
        print(f"{slug}: {'build cancelled' if applied else 'noop (not approved-but-unbuilt)'}")
        return 0 if applied else 1

    row = memory.get_pending_approval(slug)
    if row is not None and row.kind in (
        "scheduled_task_proposal",
        "scheduled_task_deletion",
        "scheduled_task_update",
    ):
        init_task_decision_runtime(
            memory=memory,
            get_scheduler=lambda: None,
            timezone=config.scheduler.timezone,
        )
        applied = await handle_task_decision(slug, approved=(action == "approve"))
        memory.close()
        print(f"{slug}: {'applied' if applied else 'noop (already decided, duplicate, or unknown)'}")
        return 0 if applied else 1

    if row is not None and row.kind in ("tracker_proposal", "tracker_deletion"):
        init_tracker_decision_runtime(memory=memory, get_scheduler=lambda: None)
        applied = await handle_tracker_decision(slug, approved=(action == "approve"))
        memory.close()
        print(f"{slug}: {'applied' if applied else 'noop (already decided, duplicate, or unknown)'}")
        return 0 if applied else 1

    if row is not None and row.kind in (
        "playbook_proposal",
        "playbook_deletion",
        "playbook_update",
        "playbook_run_proposal",
    ):
        init_workspace(config.agent.workspace, config.agent.allow_read_outside)
        init_terminal(config.terminal, config.agent.workspace)
        mcp = MCPManager()
        mcp_config_path = config.root_dir / "mcp_servers.json"
        await mcp.load_servers(mcp_config_path)
        init_tool_registry(mcp, config)
        agent = Agent(config, llm, memory, mcp)
        init_playbook_decision_runtime(memory=memory, get_agent=lambda: agent)
        init_playbook_run_delivery_callback(_cli_playbook_run_delivery)
        init_playbook_run_reject_callback(_cli_playbook_run_reject)
        from mose import playbook_decision as _pd

        _pd._runtime["await_playbook_runs"] = True
        try:
            applied = await handle_playbook_decision(slug, approved=(action == "approve"))
        finally:
            _pd._runtime.pop("await_playbook_runs", None)
            await mcp.close()
        memory.close()
        print(f"{slug}: {'applied' if applied else 'noop (already decided, duplicate, or unknown)'}")
        return 0 if applied else 1

    if row is not None and row.kind == "upcoming_add":
        store = UpcomingStore(config.upcoming.db_path)
        from mose.upcoming_decision import init_upcoming_decision_runtime

        init_upcoming_decision_runtime(memory=memory, store=store, config=config, execute_codemode=None)
        init_upcoming_delivery_callback(_cli_upcoming_delivery)
        applied = await handle_upcoming_decision(slug, approved=(action == "approve"), line_spec=line_spec)
        store.close()
        memory.close()
        print(f"{slug}: {'applied' if applied else 'noop (already decided or unknown)'}")
        return 0 if applied else 1

    init_skill_decision_runtime(learner=learner, memory=memory, llm=llm)
    applied = await handle_skill_decision(slug, approved=(action == "approve"))
    memory.close()
    print(f"{slug}: {'applied' if applied else 'noop (already decided or unknown)'}")
    return 0 if applied else 1


async def _run_sweep_once(config) -> int:
    """Expire stale skill proposals and re-ping admins (fire-and-forget Signal)."""
    llm = create_llm_client(config.llm)
    memory = MemoryManager(config.memory)
    init_workspace(config.agent.workspace, config.agent.allow_read_outside)
    init_skills_dir(config.agent.skills_path)

    # Wire only the reminder channel that fits this run.
    if signal_runtime_ready(config.signal):
        from mose.signal_bot import _signal_skill_reminder_callback
        init_skill_reminder(_signal_skill_reminder_callback)
    else:
        init_skill_reminder(None)

    from mose.learning import SkillLearner
    learner = SkillLearner(
        config.learning,
        Path(config.agent.skills_path),
        log_dir=Path(config.learning.review_log_dir),
        proposal_timeout_seconds=int(config.signal.proposal_timeout_seconds),
    )
    expired, reminded = await learner.sweep_expired_approvals(memory, reminder=True)
    memory.close()
    print(f"sweep: expired={expired} reminded={reminded}")
    return 0


async def _run_skill_review_once(config, *, notify: bool) -> int:
    """One-shot skill review entry point used by systemd timers and operators."""
    logger = get_logger("main")
    init_workspace(config.agent.workspace, config.agent.allow_read_outside)
    init_terminal(config.terminal, config.agent.workspace)
    init_skills_dir(config.agent.skills_path)

    llm = create_llm_client(config.llm)
    memory = MemoryManager(config.memory)
    mcp = MCPManager()
    # No MCP config load needed for a review run; keep startup light.
    init_tool_registry(mcp, config)

    # Register notify target (Signal if configured, else CLI stdout) before building Agent.
    if notify and signal_runtime_ready(config.signal):
        from mose.signal_bot import _signal_skill_review_notify
        init_skill_review(_signal_skill_review_notify)
    else:
        init_skill_review(_cli_skill_review_notify if notify else None)

    agent = Agent(config, llm, memory, mcp)
    try:
        report = await agent.run_skill_review(notify=notify)
        if report is None:
            log_event(logger, "skill_review_cli_no_report")
            return 1
        print(str(report))
        return 0
    finally:
        await mcp.close()
        memory.close()


async def _run_upcoming_sync_once(config) -> int:
    store = UpcomingStore(config.upcoming.db_path)
    try:
        result = await run_daily_sync(store, config.upcoming)
        print(json.dumps({k: v for k, v in result.items() if k != "stats"}, indent=2))
        if result.get("stats"):
            print(json.dumps(result["stats"], indent=2, default=str))
        return 0 if result.get("status") == "ok" else 1
    finally:
        store.close()


async def _run_upcoming_recommend_once(config) -> int:
    store = UpcomingStore(config.upcoming.db_path)
    memory = MemoryManager(config.memory)
    init_upcoming_notify(_cli_upcoming_notify)
    init_upcoming_failure_notify(_cli_upcoming_failure)
    try:
        result = await run_weekly_recommend(
            store,
            config.upcoming,
            log_dir=config.observe.log_dir,
            memory=memory,
            recipient=(config.signal.admin_group_id or "").strip() or "cli",
        )
        print(json.dumps({k: v for k, v in result.items() if k != "items"}, indent=2, default=str))
        return 0 if result.get("status") == "ok" else 1
    finally:
        store.close()
        memory.close()


async def _run_upcoming_attach_test(config, path: str) -> int:
    from pathlib import Path as _Path

    p = _Path(path)
    if not p.is_file():
        print(f"File not found: {path}")
        return 1
    if not signal_runtime_ready(config.signal):
        print(f"Signal is not configured. File exists at {p.resolve()}")
        return 2
    from mose.signal_bot import MoseSignalBot

    class _Dummy:
        memory = None

    bot = MoseSignalBot(_Dummy(), config.signal)  # type: ignore[arg-type]
    try:
        await bot._connect()
        bot._reader_task = asyncio.create_task(bot._reader_loop())
        await bot._send_message(
            config.signal.admin_group_id,
            "Upcoming attach test — if you see this file, outbound attachments work.",
            attachments=[str(p.resolve())],
        )
        print("Sent test attachment to Signal admin group.")
        return 0
    finally:
        await bot.close()


async def main() -> None:
    args = _parse_args(sys.argv[1:])
    config = load_config()
    assert_signal_account_requires_groups(config.signal)

    # Set up logging first
    setup_logging(config.observe.log_dir, config.observe.log_level)
    logger = get_logger("main")

    if args.skill_review:
        log_event(logger, "skill_review_cli", notify=not args.skill_review_no_notify)
        code = await _run_skill_review_once(config, notify=not args.skill_review_no_notify)
        sys.exit(code)

    if args.decide:
        if len(args.decide) < 2:
            print("Usage: python -m mose --decide SLUG DECISION [LINES]")
            sys.exit(2)
        slug, decision = args.decide[0], args.decide[1]
        line_spec = ",".join(args.decide[2:]) if len(args.decide) > 2 else None
        log_event(logger, "skill_decide_cli", slug=slug, decision=decision)
        code = await _run_decide_once(config, slug, decision, line_spec)
        sys.exit(code)

    if args.sweep_approvals:
        log_event(logger, "skill_sweep_cli")
        code = await _run_sweep_once(config)
        sys.exit(code)

    if args.tracker_compact:
        log_event(logger, "tracker_compact_cli", vacuum=args.tracker_compact_vacuum)
        code = _run_tracker_compact_cli(config, vacuum=args.tracker_compact_vacuum)
        sys.exit(code)

    if args.list_trackers:
        sys.exit(_run_list_trackers_cli(config))

    if args.seed_tracker:
        log_event(logger, "seed_tracker_cli", name=args.seed_tracker)
        sys.exit(_run_seed_tracker_cli(config, args.seed_tracker.strip()))

    if args.apply_plex_trackers:
        log_event(logger, "apply_plex_trackers_cli")
        sys.exit(_run_apply_plex_trackers_cli(config))

    if args.apply_tracker_schedule is not None:
        sec = None if args.apply_tracker_schedule == -1 else args.apply_tracker_schedule
        log_event(logger, "apply_tracker_schedule_cli", seconds=sec)
        sys.exit(_run_apply_tracker_schedule_cli(config, sec))

    if args.tracker_run_now:
        log_event(logger, "tracker_run_now_cli", slug=args.tracker_run_now)
        code = await _run_tracker_run_now_cli(config, args.tracker_run_now.strip())
        sys.exit(code)

    if args.debug_tracker_collector:
        log_event(logger, "debug_tracker_collector_cli", slug=args.debug_tracker_collector)
        code = await _run_debug_tracker_collector_cli(config, args.debug_tracker_collector.strip())
        sys.exit(code)

    if args.scheduled_task_run_now:
        log_event(logger, "scheduled_task_run_now_cli", slug=args.scheduled_task_run_now)
        code = await _run_scheduled_task_run_now_cli(config, args.scheduled_task_run_now.strip())
        sys.exit(code)

    if args.upcoming_sync:
        log_event(logger, "upcoming_sync_cli")
        code = await _run_upcoming_sync_once(config)
        sys.exit(code)

    if args.upcoming_recommend:
        log_event(logger, "upcoming_recommend_cli")
        code = await _run_upcoming_recommend_once(config)
        sys.exit(code)

    if args.upcoming_attach_test:
        log_event(logger, "upcoming_attach_test_cli", path=args.upcoming_attach_test)
        code = await _run_upcoming_attach_test(config, args.upcoming_attach_test)
        sys.exit(code)

    log_event(logger, "startup", llm_endpoint=config.llm.endpoint)

    # Initialize workspace sandbox
    init_workspace(config.agent.workspace, config.agent.allow_read_outside)
    init_terminal(config.terminal, config.agent.workspace)
    init_skills_dir(config.agent.skills_path)

    # Initialize components
    llm = create_llm_client(config.llm)
    memory = MemoryManager(config.memory)

    mcp = MCPManager()
    mcp_config_path = config.root_dir / "mcp_servers.json"
    await mcp.load_servers(mcp_config_path)
    init_tool_registry(mcp, config)

    approval_bridge_handle: Any = None
    try:
        # Choose mode: Signal > Discord > CLI
        if signal_runtime_ready(config.signal):
            from mose.signal_bot import (
                MoseSignalBot,
                _signal_approval_callback,
                _signal_playbook_propose_callback,
                _signal_playbook_run_delivery,
                _signal_playbook_run_reject,
                _signal_skill_propose_callback,
                _signal_skill_recovery_notice,
                _signal_skill_review_notify,
                _signal_task_delivery,
                _signal_task_failure,
                _signal_task_propose_callback,
                _signal_tracker_alert,
                _signal_tracker_propose_callback,
                _signal_upcoming_delivery,
                _signal_upcoming_failure,
                _signal_upcoming_notify,
            )
            init_skill_promotion(_signal_skill_propose_callback)
            init_skill_reminder(None)  # superseded by the consolidated recovery notice
            init_skill_recovery_notice(_signal_skill_recovery_notice)
            init_skill_review(_signal_skill_review_notify)
            init_tracker_propose_callback(_signal_tracker_propose_callback)
            init_tracker_alert_callback(_signal_tracker_alert)
            init_task_propose_callback(_signal_task_propose_callback)
            init_task_delivery_callback(_signal_task_delivery)
            init_task_failure_callback(_signal_task_failure)
            init_playbook_propose_callback(_signal_playbook_propose_callback)
            init_playbook_run_delivery_callback(_signal_playbook_run_delivery)
            init_playbook_run_reject_callback(_signal_playbook_run_reject)
            init_upcoming_notify(_signal_upcoming_notify)
            init_upcoming_failure_notify(_signal_upcoming_failure)
            init_upcoming_delivery_callback(_signal_upcoming_delivery)
            init_approval(_signal_approval_callback)
            approval_bridge_handle = await _maybe_start_portal_approval_bridge(config)
            agent = Agent(config, llm, memory, mcp)
            init_skill_decision_runtime(learner=agent._skill_learner, memory=memory, llm=llm)
            agent.start_skill_review_loop()
            agent.start_trackers_loop()
            agent.start_tracker_compaction_loop()
            agent.start_task_scheduler_loop()
            agent.start_upcoming_loop()
            bot = MoseSignalBot(agent, config.signal)

            async def _signal_startup_recovery() -> None:
                await agent.recover_pending_approvals()
                extra = agent.tracker_recovery_digest(recipient=config.signal.admin_group_id)
                if extra.strip():
                    adm = (config.signal.admin_group_id or "").strip()
                    if adm:
                        await bot._send_message(adm, extra)

            bot.on_ready = _signal_startup_recovery
            log_event(logger, "starting_signal_bot")
            try:
                await bot.start()
            except KeyboardInterrupt:
                pass
            finally:
                await agent.stop_skill_review_loop()
                await agent.stop_tracker_compaction_loop()
                await agent.stop_trackers_loop()
                await agent.stop_task_scheduler_loop()
                await agent.stop_upcoming_loop()
                await bot.close()
        elif config.discord.token:
            from mose.discord_bot import (
                MoseDiscordBot,
                _discord_approval_callback,
                _discord_tracker_alert,
            )
            # Discord skill-proposal UX is not wired; no callback means proposals
            # are rejected immediately and never built (required by policy).
            init_skill_promotion(None)
            init_skill_reminder(None)
            init_skill_recovery_notice(None)
            init_skill_review(None)
            init_tracker_propose_callback(None)
            init_tracker_alert_callback(_discord_tracker_alert)
            init_task_propose_callback(None)
            init_task_delivery_callback(_cli_task_delivery)
            init_task_failure_callback(_cli_task_failure)
            init_playbook_propose_callback(None)
            init_playbook_run_delivery_callback(_cli_playbook_run_delivery)
            init_playbook_run_reject_callback(_cli_playbook_run_reject)
            init_upcoming_notify(_cli_upcoming_notify)
            init_upcoming_failure_notify(_cli_upcoming_failure)
            init_upcoming_delivery_callback(_cli_upcoming_delivery)
            init_approval(_discord_approval_callback)
            approval_bridge_handle = await _maybe_start_portal_approval_bridge(config)
            agent = Agent(config, llm, memory, mcp)
            init_skill_decision_runtime(learner=agent._skill_learner, memory=memory, llm=llm)
            # Discord path has no approval UX: run recovery so the DB still
            # ages out expired rows, but there's no channel to notify.
            await agent.recover_pending_approvals()
            agent.start_skill_review_loop()
            agent.start_trackers_loop()
            agent.start_tracker_compaction_loop()
            agent.start_task_scheduler_loop()
            agent.start_upcoming_loop()
            bot = MoseDiscordBot(agent)
            log_event(logger, "starting_discord_bot")
            try:
                await bot.start(config.discord.token)
            except KeyboardInterrupt:
                pass
            finally:
                await agent.stop_skill_review_loop()
                await agent.stop_tracker_compaction_loop()
                await agent.stop_trackers_loop()
                await agent.stop_task_scheduler_loop()
                await agent.stop_upcoming_loop()
                await bot.close()
        else:
            init_skill_promotion(_cli_skill_propose_callback)
            init_skill_reminder(None)  # CLI reminds through foreground prompts
            init_skill_recovery_notice(_cli_skill_recovery_notice)
            init_skill_review(_cli_skill_review_notify)
            init_tracker_propose_callback(_cli_tracker_propose_callback)
            init_tracker_alert_callback(_cli_tracker_alert)
            init_task_propose_callback(_cli_task_propose_callback)
            init_task_delivery_callback(_cli_task_delivery)
            init_task_failure_callback(_cli_task_failure)
            init_playbook_propose_callback(_cli_playbook_propose_callback)
            init_playbook_run_delivery_callback(_cli_playbook_run_delivery)
            init_playbook_run_reject_callback(_cli_playbook_run_reject)
            init_upcoming_notify(_cli_upcoming_notify)
            init_upcoming_failure_notify(_cli_upcoming_failure)
            init_upcoming_delivery_callback(_cli_upcoming_delivery)
            init_approval(_cli_approval_callback)
            approval_bridge_handle = await _maybe_start_portal_approval_bridge(config)
            log_event(logger, "cli_mode")

            # Suppress console log noise in CLI mode
            for h in logging.getLogger("mose").handlers:
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    h.setLevel(logging.WARNING)

            agent = Agent(config, llm, memory, mcp, tool_callback=_print_tool_call)
            init_skill_decision_runtime(learner=agent._skill_learner, memory=memory, llm=llm)
            await agent.recover_pending_approvals()
            extra_cli = agent.tracker_recovery_digest(recipient=None)
            if extra_cli.strip():
                print(extra_cli)
            agent.start_skill_review_loop()
            agent.start_trackers_loop()
            agent.start_tracker_compaction_loop()
            agent.start_task_scheduler_loop()
            agent.start_upcoming_loop()
            try:
                await _run_cli(agent)
            finally:
                await agent.stop_skill_review_loop()
                await agent.stop_tracker_compaction_loop()
                await agent.stop_trackers_loop()
                await agent.stop_task_scheduler_loop()
                await agent.stop_upcoming_loop()
    finally:
        from mose.approval_bridge import stop_approval_bridge

        await stop_approval_bridge(approval_bridge_handle)
        await mcp.close()
        memory.close()
        log_event(logger, "shutdown")


if __name__ == "__main__":
    asyncio.run(main())
