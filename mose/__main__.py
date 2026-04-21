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
from mose.tools import init_workspace, init_tool_registry, init_approval, init_terminal, init_skills_dir


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
        nargs=2,
        metavar=("SLUG", "DECISION"),
        help="Resolve a pending skill proposal from the command line. "
             "DECISION is 'approve' / 'yes' / 'y', 'reject' / 'no' / 'n', "
             "or 'cancel' / 'stop' (abort an approved-but-unbuilt build).",
    )
    parser.add_argument(
        "--sweep-approvals",
        action="store_true",
        help="Run the pending-approvals sweep (expire stale, remind admin) and exit.",
    )
    return parser.parse_args(argv)


async def _run_decide_once(config, slug: str, decision: str) -> int:
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
    init_tool_registry(mcp)

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
        slug, decision = args.decide
        log_event(logger, "skill_decide_cli", slug=slug, decision=decision)
        code = await _run_decide_once(config, slug, decision)
        sys.exit(code)

    if args.sweep_approvals:
        log_event(logger, "skill_sweep_cli")
        code = await _run_sweep_once(config)
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
    init_tool_registry(mcp)

    # Choose mode: Signal > Discord > CLI
    if signal_runtime_ready(config.signal):
        from mose.signal_bot import (
            MoseSignalBot,
            _signal_approval_callback,
            _signal_skill_propose_callback,
            _signal_skill_recovery_notice,
            _signal_skill_review_notify,
        )
        init_skill_promotion(_signal_skill_propose_callback)
        init_skill_reminder(None)  # superseded by the consolidated recovery notice
        init_skill_recovery_notice(_signal_skill_recovery_notice)
        init_skill_review(_signal_skill_review_notify)
        init_approval(_signal_approval_callback)
        agent = Agent(config, llm, memory, mcp)
        init_skill_decision_runtime(learner=agent._skill_learner, memory=memory, llm=llm)
        agent.start_skill_review_loop()
        bot = MoseSignalBot(agent, config.signal)
        # Defer restart recovery until Signal is connected so the consolidated
        # notice can actually reach the admin. Expiration + rejection file
        # moves still happen deterministically inside run_startup_recovery.
        bot.on_ready = agent.recover_pending_approvals
        log_event(logger, "starting_signal_bot")
        try:
            await bot.start()
        except KeyboardInterrupt:
            pass
        finally:
            await agent.stop_skill_review_loop()
            await bot.close()
    elif config.discord.token:
        from mose.discord_bot import MoseDiscordBot, _discord_approval_callback
        # Discord skill-proposal UX is not wired; no callback means proposals
        # are rejected immediately and never built (required by policy).
        init_skill_promotion(None)
        init_skill_reminder(None)
        init_skill_recovery_notice(None)
        init_skill_review(None)
        init_approval(_discord_approval_callback)
        agent = Agent(config, llm, memory, mcp)
        init_skill_decision_runtime(learner=agent._skill_learner, memory=memory, llm=llm)
        # Discord path has no approval UX: run recovery so the DB still
        # ages out expired rows, but there's no channel to notify.
        await agent.recover_pending_approvals()
        agent.start_skill_review_loop()
        bot = MoseDiscordBot(agent)
        log_event(logger, "starting_discord_bot")
        try:
            await bot.start(config.discord.token)
        except KeyboardInterrupt:
            pass
        finally:
            await agent.stop_skill_review_loop()
            await bot.close()
    else:
        init_skill_promotion(_cli_skill_propose_callback)
        init_skill_reminder(None)  # CLI reminds through foreground prompts
        init_skill_recovery_notice(_cli_skill_recovery_notice)
        init_skill_review(_cli_skill_review_notify)
        init_approval(_cli_approval_callback)
        log_event(logger, "cli_mode")

        # Suppress console log noise in CLI mode
        for h in logging.getLogger("mose").handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.WARNING)

        agent = Agent(config, llm, memory, mcp, tool_callback=_print_tool_call)
        init_skill_decision_runtime(learner=agent._skill_learner, memory=memory, llm=llm)
        await agent.recover_pending_approvals()
        agent.start_skill_review_loop()
        try:
            await _run_cli(agent)
        finally:
            await agent.stop_skill_review_loop()

    # Cleanup
    await mcp.close()
    memory.close()
    log_event(logger, "shutdown")


if __name__ == "__main__":
    asyncio.run(main())
