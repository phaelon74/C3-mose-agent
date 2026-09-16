"""Human-in-the-loop approval for playbooks and on-demand playbook runs."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from mose.memory import MemoryManager
from mose.observe import get_logger, log_event

logger = get_logger("playbook_decision")

PLAYBOOK_PROPOSAL_KIND = "playbook_proposal"
PLAYBOOK_DELETION_KIND = "playbook_deletion"
PLAYBOOK_UPDATE_KIND = "playbook_update"
PLAYBOOK_RUN_PROPOSAL_KIND = "playbook_run_proposal"

PLAYBOOK_KINDS = frozenset({
    PLAYBOOK_PROPOSAL_KIND,
    PLAYBOOK_DELETION_KIND,
    PLAYBOOK_UPDATE_KIND,
    PLAYBOOK_RUN_PROPOSAL_KIND,
})

_runtime: dict[str, Any] = {}

PlaybookProposeCallback = Callable[[str, str, float, dict[str, Any]], Any | Awaitable[Any]]
PlaybookRunDeliveryCallback = Callable[[str, str, dict[str, Any]], Any | Awaitable[Any]]
PlaybookRunRejectCallback = Callable[[str, dict[str, Any]], Any | Awaitable[Any]]

_playbook_propose_callback: PlaybookProposeCallback | None = None
_playbook_run_delivery_callback: PlaybookRunDeliveryCallback | None = None
_playbook_run_reject_callback: PlaybookRunRejectCallback | None = None


def init_playbook_decision_runtime(
    *,
    memory: MemoryManager,
    get_agent: Callable[[], Any | None],
) -> None:
    _runtime["memory"] = memory
    _runtime["get_agent"] = get_agent


def init_playbook_propose_callback(callback: PlaybookProposeCallback | None) -> None:
    global _playbook_propose_callback
    _playbook_propose_callback = callback


def init_playbook_run_delivery_callback(callback: PlaybookRunDeliveryCallback | None) -> None:
    global _playbook_run_delivery_callback
    _playbook_run_delivery_callback = callback


def init_playbook_run_reject_callback(callback: PlaybookRunRejectCallback | None) -> None:
    global _playbook_run_reject_callback
    _playbook_run_reject_callback = callback


def _format_invocation_params(params: dict[str, Any]) -> str:
    if not params:
        return "(none)"
    parts: list[str] = []
    for k, v in params.items():
        parts.append(f"  {k}: {v}")
    return "\n".join(parts)


def format_playbook_proposal_message(payload: dict[str, Any]) -> str:
    slug = payload.get("playbook_slug") or payload.get("slug") or "?"
    desc = payload.get("description") or slug
    plan = payload.get("execution_plan") or {}
    procedure = plan.get("procedure") or "(none)"
    tools = plan.get("allowed_tools") or []
    tools_s = ", ".join(str(t) for t in tools) if tools else "(none)"
    template = payload.get("user_prompt_template") or "(none)"
    return (
        f"Playbook proposal: {slug}\n"
        f"Description: {desc}\n"
        f"Invocation template: {template}\n\n"
        f"Procedure:\n{procedure}\n\n"
        f"Allowed tools: {tools_s}\n\n"
        f"Approve: approve {slug}\n"
        f"Reject: reject {slug}"
    )


def format_playbook_update_message(payload: dict[str, Any]) -> str:
    target = payload.get("target_slug") or "?"
    updates = payload.get("updates") or {}
    before = payload.get("before") or {}
    lines: list[str] = []
    for k in updates:
        b = before.get(k)
        a = updates.get(k)
        if k == "execution_plan":
            b_plan = b if isinstance(b, dict) else {}
            a_plan = a if isinstance(a, dict) else {}
            b_proc = b_plan.get("procedure") or "(none)"
            a_proc = a_plan.get("procedure") or "(none)"
            lines.append(f"  execution_plan procedure:\n    - was: {b_proc}\n    + now: {a_proc}")
        else:
            lines.append(f"  {k}:\n    - was: {b}\n    + now: {a}")
    changes_block = "\n".join(lines) if lines else "  (none)"
    pending_slug = payload.get("pending_slug") or f"playbook-upd-{target}"
    return (
        f"Playbook update: {target}\n"
        f"Description: {payload.get('description') or f'Update {target}'}\n\n"
        f"Changes:\n{changes_block}\n\n"
        f"Approve: approve {pending_slug}\n"
        f"Reject: reject {pending_slug}"
    )


def format_playbook_run_proposal_message(payload: dict[str, Any]) -> str:
    run_slug = payload.get("run_slug") or "?"
    playbook_slug = payload.get("playbook_slug") or "?"
    desc = payload.get("description") or playbook_slug
    params = payload.get("invocation_params") or {}
    preflight = payload.get("preflight_summary") or "(none)"
    plan = payload.get("execution_plan") or {}
    procedure = plan.get("procedure") or "(none)"
    tools = plan.get("allowed_tools") or []
    tools_s = ", ".join(str(t) for t in tools) if tools else "(none)"
    user_prompt = payload.get("user_prompt") or ""
    prompt_preview = user_prompt[:500] + ("..." if len(user_prompt) > 500 else "")
    return (
        f"Playbook run approval: {run_slug}\n"
        f"Playbook: {playbook_slug}\n"
        f"Description: {desc}\n\n"
        f"Invocation:\n{_format_invocation_params(params if isinstance(params, dict) else {})}\n\n"
        f"Preflight findings:\n{preflight}\n\n"
        f"Run prompt:\n{prompt_preview}\n\n"
        f"Procedure:\n{procedure}\n\n"
        f"Allowed tools: {tools_s}\n\n"
        f"Approve: approve {run_slug}\n"
        f"Reject: reject {run_slug}"
    )


def format_playbook_notification_body(payload: dict[str, Any]) -> str:
    kind = str(payload.get("proposal_kind") or "")
    if kind == PLAYBOOK_RUN_PROPOSAL_KIND:
        return format_playbook_run_proposal_message(payload)
    if payload.get("updates") and payload.get("target_slug"):
        return format_playbook_update_message(payload)
    return format_playbook_proposal_message(payload)


def format_playbook_recovery_message(
    memory: MemoryManager,
    *,
    recipient: str | None = None,
) -> str:
    pending = memory.list_pending_approvals(status="pending")
    if recipient:
        r = recipient.strip()
        pending = [p for p in pending if (p.recipient or "").strip() == r]
    playbook_p = [p for p in pending if p.kind in PLAYBOOK_KINDS]
    lines: list[str] = []
    if playbook_p:
        lines.append("")
        lines.append(f"Playbook approvals pending ({len(playbook_p)}):")
        for row in playbook_p:
            title = (row.payload or {}).get("description") or row.kind
            lines.append(f"  - {row.slug} — {title}")
        lines.append("  Decide with: python -m mose --decide <slug> y|n")
    return "\n".join(lines) if lines else ""


async def _execute_playbook_run(payload: dict[str, Any], *, run_id: int) -> None:
    mem: MemoryManager | None = _runtime.get("memory")
    get_agent = _runtime.get("get_agent")
    if mem is None or not callable(get_agent):
        return
    agent = get_agent()
    if agent is None:
        return

    run_slug = str(payload.get("run_slug") or "playbook-run")
    started = time.time()
    try:
        result = await agent.run_playbook_run(payload)
        status = str(result.get("status") or "ok")
        summary = str(result.get("summary") or "")
        tool_trace = result.get("tool_trace") or []
        mem.update_playbook_run(
            run_id,
            finished_at=time.time(),
            status=status,
            summary=summary[:8000],
            tool_trace=tool_trace if isinstance(tool_trace, list) else [],
        )
        if _playbook_run_delivery_callback is not None:
            try:
                ret = _playbook_run_delivery_callback(run_slug, summary, payload)
                if hasattr(ret, "__await__"):
                    await ret
            except Exception:
                logger.exception("playbook_run_delivery failed", extra={"run_slug": run_slug})
        log_event(logger, "playbook_run_completed", run_slug=run_slug, status=status)
    except Exception as e:
        logger.exception("playbook_run_failed", extra={"run_slug": run_slug})
        mem.update_playbook_run(
            run_id,
            finished_at=time.time(),
            status="failed",
            summary=str(e)[:2000],
        )


async def handle_playbook_decision(slug: str, *, approved: bool) -> bool:
    mem: MemoryManager | None = _runtime.get("memory")
    if mem is None:
        log_event(logger, "playbook_decision_no_runtime", slug=slug)
        return False
    row = mem.get_pending_approval(slug)
    if row is None or row.status != "pending":
        return False
    if row.kind not in PLAYBOOK_KINDS:
        return False

    if not approved:
        mem.decide_pending_approval(slug, approved=False)
        log_event(logger, "playbook_decision_rejected", slug=slug, kind=row.kind)
        if row.kind == PLAYBOOK_RUN_PROPOSAL_KIND and _playbook_run_reject_callback is not None:
            try:
                ret = _playbook_run_reject_callback(slug, row.payload or {})
                if hasattr(ret, "__await__"):
                    await ret
            except Exception:
                logger.exception("playbook_run_reject_callback failed", extra={"slug": slug})
        return True

    if row.kind == PLAYBOOK_DELETION_KIND:
        target = (row.payload or {}).get("target_slug") or slug
        mem.delete_playbook(str(target))
        mem.decide_pending_approval(slug, approved=True)
        log_event(logger, "playbook_deleted_via_approval", pending_slug=slug, target=target)
        return True

    if row.kind == PLAYBOOK_UPDATE_KIND:
        p = row.payload or {}
        target = str(p.get("target_slug") or "")
        if not target or mem.get_playbook(target) is None:
            log_event(logger, "playbook_update_missing_target", slug=slug, target=target)
            return False
        updates = p.get("updates") or {}
        if not updates:
            return False
        if "execution_plan" in updates:
            plan = updates["execution_plan"]
            if not isinstance(plan, dict) or not plan.get("allowed_tools"):
                log_event(logger, "playbook_update_missing_plan", slug=slug, target=target)
                return False
        mem.update_playbook(target, **updates)
        mem.decide_pending_approval(slug, approved=True)
        log_event(logger, "playbook_updated_via_approval", pending_slug=slug, target=target)
        return True

    if row.kind == PLAYBOOK_RUN_PROPOSAL_KIND:
        p = row.payload or {}
        playbook_slug = str(p.get("playbook_slug") or "")
        run_slug = str(p.get("run_slug") or slug)
        playbook = mem.get_playbook(playbook_slug)
        if playbook is None:
            log_event(logger, "playbook_run_missing_playbook", slug=slug, playbook=playbook_slug)
            return False
        user_prompt = str(p.get("user_prompt") or "")
        if not user_prompt:
            log_event(logger, "playbook_run_missing_prompt", slug=slug)
            return False
        started = time.time()
        run_id = mem.insert_playbook_run(
            playbook.id,
            run_slug=run_slug,
            user_prompt=user_prompt,
            invocation_params=p.get("invocation_params") if isinstance(p.get("invocation_params"), dict) else {},
            preflight_summary=str(p.get("preflight_summary") or ""),
            started_at=started,
            status="running",
        )
        mem.decide_pending_approval(slug, approved=True)
        run_coro = _execute_playbook_run(p, run_id=run_id)
        if _runtime.get("await_playbook_runs"):
            await run_coro
        else:
            asyncio.create_task(run_coro, name=f"playbook-run-{run_slug}")
        log_event(logger, "playbook_run_approved", run_slug=run_slug, playbook=playbook_slug)
        return True

    # playbook_proposal (create)
    p = row.payload or {}
    pslug = str(p.get("playbook_slug") or slug)
    if mem.get_playbook(pslug) is not None:
        log_event(logger, "playbook_proposal_duplicate", slug=pslug)
        return False
    plan = p.get("execution_plan") or {}
    if not isinstance(plan, dict) or not plan.get("allowed_tools"):
        log_event(logger, "playbook_missing_plan", slug=pslug)
        return False
    procedure = str(plan.get("procedure") or "").strip()
    if not procedure:
        log_event(logger, "playbook_missing_procedure", slug=pslug)
        return False
    mem.create_playbook(
        slug=pslug,
        description=str(p.get("description") or pslug),
        execution_plan=plan,
        user_prompt_template=p.get("user_prompt_template"),
        system_addendum=p.get("system_addendum"),
        created_by_session=p.get("created_by_session"),
    )
    mem.decide_pending_approval(slug, approved=True)
    log_event(logger, "playbook_created_via_approval", slug=pslug)
    return True


async def notify_playbook_proposal(slug: str, payload: dict[str, Any], expires_at: float) -> None:
    if _playbook_propose_callback is None:
        return
    desc = str(payload.get("description") or slug)
    try:
        ret = _playbook_propose_callback(slug, desc, expires_at, payload)
        if hasattr(ret, "__await__"):
            await ret
    except Exception:
        logger.exception("playbook_propose_callback failed", extra={"slug": slug})
