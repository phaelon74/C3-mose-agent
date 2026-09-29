"""Human-in-the-loop approval for weekly upcoming-media add batches."""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from mose.memory import MemoryManager
from mose.observe import get_logger, log_event
from mose.upcoming import (
    UPCOMING_APPROVAL_KIND,
    canonical_upcoming_slug,
    execute_upcoming_adds,
    parse_line_numbers,
)
from mose.upcoming_store import UpcomingStore

logger = get_logger("upcoming_decision")

_runtime: dict[str, Any] = {}

UpcomingDeliveryFn = Callable[[str, str], Any | Awaitable[Any]]
_delivery: UpcomingDeliveryFn | None = None


def init_upcoming_decision_runtime(
    *,
    memory: MemoryManager,
    store: UpcomingStore,
    config: Any,
    execute_codemode: Callable[[str, int], Awaitable[tuple[str, bool]]] | None = None,
) -> None:
    _runtime["memory"] = memory
    _runtime["store"] = store
    _runtime["config"] = config
    _runtime["execute_codemode"] = execute_codemode


def init_upcoming_delivery_callback(callback: UpcomingDeliveryFn | None) -> None:
    global _delivery
    _delivery = callback


def format_upcoming_recovery_message(
    memory: MemoryManager,
    *,
    recipient: str | None = None,
) -> str:
    pending = memory.list_pending_approvals(status="pending")
    if recipient:
        r = recipient.strip()
        pending = [p for p in pending if (p.recipient or "").strip() == r]
    rows = [p for p in pending if p.kind == UPCOMING_APPROVAL_KIND]
    if not rows:
        return ""
    lines = ["", f"Upcoming-media approvals pending ({len(rows)}):"]
    for row in rows:
        counts = (row.payload or {}).get("counts") or {}
        lines.append(
            f"  - {row.slug} — {counts.get('movie', '?')} movies / "
            f"{counts.get('tv', '?')} TV / {counts.get('anime', '?')} anime"
        )
        lines.append(f"    approve {row.slug}   or   approve {row.slug} 1,4,7   or   reject {row.slug}")
    return "\n".join(lines)


async def _deliver(slug: str, body: str) -> None:
    if _delivery is None:
        return
    ret = _delivery(slug, body)
    if hasattr(ret, "__await__"):
        await ret


async def handle_upcoming_decision(
    slug: str,
    *,
    approved: bool,
    line_spec: str | None = None,
) -> bool:
    mem: MemoryManager | None = _runtime.get("memory")
    store: UpcomingStore | None = _runtime.get("store")
    config = _runtime.get("config")
    if mem is None or store is None or config is None:
        log_event(logger, "upcoming_decision_no_runtime", slug=slug)
        return False
    slug = canonical_upcoming_slug(slug)
    row = mem.get_pending_approval(slug)
    if row is None or row.status != "pending":
        return False
    if row.kind != UPCOMING_APPROVAL_KIND:
        return False

    if not approved:
        mem.decide_pending_approval(slug, approved=False)
        store.set_recommendation_status(slug, "rejected")
        log_event(logger, "upcoming_decision_rejected", slug=slug)
        await _deliver(slug, f"Upcoming list '{slug}' rejected. No titles were added.")
        return True

    decided = mem.decide_pending_approval(slug, approved=True)
    if decided is None:
        return False

    payload = row.payload or {}
    run_id = int(payload.get("recommendation_run_id") or 0)
    run = store.get_recommendation_run(slug)
    if run is not None:
        run_id = int(run["id"])
    items = store.list_recommendation_items(run_id)
    wanted = parse_line_numbers(line_spec)
    if wanted is not None:
        items = [it for it in items if it.line_number in wanted]
    if not items:
        store.set_recommendation_status(slug, "approved")
        await _deliver(slug, f"Upcoming list '{slug}' approved but no matching line numbers to add.")
        return True

    arr_cfg = store.get_arr_config()
    execute_codemode = _runtime.get("execute_codemode")
    store.set_recommendation_status(slug, "adding")
    finished = False
    try:
        results = await execute_upcoming_adds(
            items,
            config.upcoming,
            arr_cfg,
            execute_codemode=execute_codemode,
            record_result=lambda result: store.record_add_result(run_id, result),
        )
        finished = True
    finally:
        if not finished:
            store.set_recommendation_status(slug, "partial")
    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    store.set_recommendation_status(slug, "added" if fail_n == 0 else "partial")
    lines = [f"Upcoming add '{slug}': {ok_n} ok, {fail_n} failed."]
    for r in results:
        mark = "ok" if r.get("ok") else "FAIL"
        lines.append(f"  [{mark}] #{r.get('line')} {r.get('title')}: {r.get('detail', '')[:180]}")
    body = "\n".join(lines)
    log_event(logger, "upcoming_adds_finished", slug=slug, ok=ok_n, failed=fail_n)
    await _deliver(slug, body)
    return True


async def retry_upcoming_adds(slug: str, line_spec: str, *, recipient: str) -> None:
    """Explicit admin retry of selected saved lines, with fresh per-item prompts."""
    slug = canonical_upcoming_slug(slug)
    mem = _runtime.get("memory")
    store = _runtime.get("store")
    config = _runtime.get("config")
    execute = _runtime.get("execute_codemode")
    if mem is None or store is None or config is None or execute is None:
        await _deliver(slug, "Retry unavailable: the approval-gated Code Mode runtime is required.")
        return
    row = mem.get_pending_approval(slug)
    run = store.get_recommendation_run(slug)
    if (row is None or row.kind != UPCOMING_APPROVAL_KIND or row.status != "approved"
            or row.recipient != recipient or run is None or run["status"] != "partial"
            or (row.payload or {}).get("recommendation_run_id") != run["id"]):
        await _deliver(slug, "No matching failed approved batch is available for retry.")
        return
    items = store.list_recommendation_items(run["id"])
    # Validate bounded ranges before using the shared parser. Explicit lines are
    # required because older failed batches did not persist individual outcomes.
    parts = line_spec.split(",")
    if not parts or any(not re.fullmatch(r"\d+(?:-\d+)?", part) for part in parts):
        await _deliver(slug, "Specify saved line numbers such as 1,4,7 or 1-23.")
        return
    valid = {item.line_number for item in items}
    if any(int(n) not in valid for part in parts for n in part.split("-")):
        await _deliver(slug, "Retry contains line numbers outside the saved report.")
        return
    wanted = parse_line_numbers(line_spec)
    if not wanted or not wanted <= valid:
        await _deliver(slug, "Retry contains line numbers outside the saved report.")
        return
    selected = [item for item in items if item.line_number in wanted
                and not item.payload.get("_add_result", {}).get("ok")]
    if not selected:
        await _deliver(slug, "All selected lines already have recorded successful additions.")
        return
    if not store.claim_recommendation_retry(slug):
        await _deliver(slug, "This batch is already being processed or is no longer retryable.")
        return
    try:
        await _deliver(slug, f"Retrying {len(selected)} selected titles from '{slug}'. Each add-and-search requires a fresh admin approval.")
        results = await execute_upcoming_adds(
            selected, config.upcoming, store.get_arr_config(), execute_codemode=execute,
            record_result=lambda result: store.record_add_result(run["id"], result),
        )
        ok_n = sum(bool(result.get("ok")) for result in results)
        lines = [f"Upcoming retry '{slug}': {ok_n} ok, {len(results) - ok_n} failed."]
        for result in results:
            mark = "ok" if result.get("ok") else "FAIL"
            lines.append(f"  [{mark}] #{result['line']} {result['title']}: {result.get('detail', '')[:180]}")
        await _deliver(slug, "\n".join(lines))
    finally:
        # Preserve partial status if any earlier failures remain or their outcomes
        # are unknown (legacy batches). Successful lines are skipped on retries.
        saved = store.list_recommendation_items(run["id"])
        complete = bool(saved) and all(item.payload.get("_add_result", {}).get("ok") for item in saved)
        store.set_recommendation_status(slug, "added" if complete else "partial")
