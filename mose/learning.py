"""Skill learning loop: durable propose-first, human-in-the-loop.

Flow (crash-safe across agent restarts):
    1. After a successful multi-tool session, the agent asks the LLM for a
       *classification only* — is this a reusable SRE pattern? If yes we get a
       slug/title/description/rationale.
    2. The proposal JSON is written to ``skills/pending/{slug}.proposal.json``
       and a row is inserted into the ``pending_approvals`` SQLite table with
       an ``expires_at`` timestamp. This is the durability boundary: from here
       on the decision can arrive in a different process.
    3. The propose callback (typically Signal) sends a notification to the
       admin *and returns immediately*. No asyncio Future is held open.
    4. When the admin replies, the interface calls ``handle_skill_decision``
       which atomically flips the DB row and — if approved — asks the LLM for
       the full Markdown body and writes ``skills/{slug}.md``.
    5. On startup, ``run_startup_recovery`` snapshots every outstanding row,
       moves any that timed out while the agent was down to
       ``skills/rejected/``, and emits a single consolidated "recovery
       notice" to the admin listing still-pending items (which need a
       decision) and expired-while-down items (informational only).
       ``sweep_expired_approvals`` remains available for periodic sweeps
       during normal operation.

The ``SkillLearner`` also runs a periodic review over ``skills/*.md`` that uses
``skill_usage`` statistics to identify skills with high failure rates. The
review produces a detailed Markdown write-up under ``data/logs/`` and emits
structured JSON events so the activity is fully auditable.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from mose.config import LearningConfig
from mose.observe import get_logger, log_event

logger = get_logger("learning")

# Callback signatures:
#   propose(proposal_path, slug, title, description, rationale, expires_at) -> None | Awaitable[None]
#       Fire-and-forget notification. MUST NOT block waiting for a reply.
#   review(report_path, summary_markdown) -> None | Awaitable[None]
#   reminder(slug, title, description, expires_at) -> None | Awaitable[None]
#       Lower-level building block (one proposal at a time). Rarely used now.
#   recovery(still_pending, expired_while_down, approved_unbuilt) -> None | Awaitable[None]
#       Called ONCE on startup with three lists of :class:`PendingApproval`.
#       ``still_pending`` asks the admin to act; ``expired_while_down`` is
#       informational only (already moved to rejected); ``approved_unbuilt``
#       warns that builds will auto-proceed after the grace window unless
#       the admin replies ``stop <slug>`` / ``cancel <slug>``.
ProposeCallback = Callable[[str, str, str, str, str, float], Any]
ReviewCallback = Callable[[str, str], Any]
ReminderCallback = Callable[[str, str, str, float], Any]
RecoveryCallback = Callable[[list[Any], list[Any], list[Any]], Any]

_skill_propose_callback: ProposeCallback | None = None
_skill_review_callback: ReviewCallback | None = None
_skill_reminder_callback: ReminderCallback | None = None
_skill_recovery_callback: RecoveryCallback | None = None


def init_skill_promotion(callback: ProposeCallback | None) -> None:
    """Register the fire-and-forget notification callback for skill proposals.

    The callback must only SEND the notification (e.g. Signal message). It
    must NOT wait for a reply — the decision arrives asynchronously through
    :func:`handle_skill_decision`. Returning early (or failing) is safe; the
    row is already persisted and will be handled on the next restart.
    """
    global _skill_propose_callback
    _skill_propose_callback = callback


def init_skill_review(callback: ReviewCallback | None) -> None:
    """Register the callback used to notify the human about a completed skill review."""
    global _skill_review_callback
    _skill_review_callback = callback


def init_skill_reminder(callback: ReminderCallback | None) -> None:
    """Register a per-proposal reminder callback. Rarely used — prefer the recovery notice."""
    global _skill_reminder_callback
    _skill_reminder_callback = callback


def init_skill_recovery_notice(callback: RecoveryCallback | None) -> None:
    """Register the startup recovery-notice callback.

    Invoked exactly once on startup with
    ``(still_pending, expired_while_down)``, both lists of
    :class:`PendingApproval`. Sends a single consolidated message so the
    admin sees the full outstanding state in one place.
    """
    global _skill_recovery_callback
    _skill_recovery_callback = callback


# Runtime references used by :func:`handle_skill_decision`, which is invoked
# from interface code (Signal bot, CLI) that does not hold a Learner instance.
_runtime: dict[str, Any] = {}


def init_skill_decision_runtime(*, learner: "SkillLearner", memory: Any, llm: Any) -> None:
    """Expose the objects needed to resolve an approval reply."""
    _runtime["learner"] = learner
    _runtime["memory"] = memory
    _runtime["llm"] = llm


async def handle_skill_decision(slug: str, *, approved: bool) -> bool:
    """Resolve an approval reply. Safe to call from any interface.

    Returns True if the decision was applied, False if the slug was already
    decided / unknown (idempotent).
    """
    learner = _runtime.get("learner")
    memory = _runtime.get("memory")
    llm = _runtime.get("llm")
    if learner is None or memory is None or llm is None:
        log_event(logger, "skill_decision_no_runtime", slug=slug, approved=approved)
        return False
    return await learner.handle_decision(slug, approved=approved, memory=memory, llm=llm)


def _strip_code_fence(text: str) -> str:
    """Remove ``` fences that some models wrap around JSON output."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _valid_slug(slug: str) -> bool:
    return bool(re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", slug or ""))


class SkillLearner:
    """Draft and review reusable SRE skills with strict human-in-the-loop control."""

    APPROVAL_KIND = "skill_proposal"

    def __init__(
        self,
        cfg: LearningConfig,
        skills_dir: Path,
        log_dir: Path | None = None,
        proposal_timeout_seconds: int = 43200,
        build_grace_window_seconds: int | None = None,
    ) -> None:
        self._cfg = cfg
        self._skills_dir = Path(skills_dir)
        self._pending = Path(cfg.pending_dir)
        self._rejected = Path(cfg.rejected_dir)
        self._log_dir = Path(log_dir) if log_dir else None
        self._proposal_timeout_seconds = max(60, int(proposal_timeout_seconds))
        # Grace window (seconds) given on startup before auto-building an
        # approved-but-unbuilt skill. The admin can abort with stop/cancel.
        grace = build_grace_window_seconds
        if grace is None:
            grace = getattr(cfg, "build_grace_window_seconds", 900)
        self._build_grace_seconds = max(0, int(grace))

    # ------------------------------------------------------------------ helpers

    def _ensure_dirs(self) -> None:
        self._pending.mkdir(parents=True, exist_ok=True)
        self._rejected.mkdir(parents=True, exist_ok=True)
        self._skills_dir.mkdir(parents=True, exist_ok=True)
        if self._log_dir is not None:
            self._log_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- propose

    async def maybe_propose_skill(
        self,
        session_id: str,
        user_message: str,
        assistant_reply: str,
        total_native_tool_calls: int,
        had_tool_error: bool,
        llm: Any,
        *,
        memory: Any | None = None,
        recipient: str = "",
    ) -> Path | None:
        """Stage 1 — classify, persist the proposal, and notify the admin.

        Durability: a row is inserted into ``pending_approvals`` BEFORE the
        Signal message is sent, so the pending state survives crashes. The
        caller does NOT wait for a reply; the admin's response is handled
        asynchronously through :func:`handle_skill_decision`.

        Returns the proposal path if a proposal was written, else ``None``.
        """
        if not self._cfg.enabled:
            return None
        if had_tool_error:
            return None
        if total_native_tool_calls < self._cfg.min_tools_used:
            return None

        self._ensure_dirs()

        classify_prompt = [
            {
                "role": "system",
                "content": (
                    "You are a skill scout for an SRE/DevOps agent. Decide whether the "
                    "last exchange contains a REUSABLE runbook pattern worth capturing "
                    "as a skill. Do NOT write the skill body — only classify.\n\n"
                    "Reply with JSON only, no prose, no markdown fences.\n"
                    "If reusable: "
                    '{"propose": true, "slug": "kebab-case", "title": "Short title", '
                    '"description": "One-line summary <= 140 chars", '
                    '"rationale": "1-3 sentences on why this is reusable and when to use it"}\n'
                    "Otherwise: {\"propose\": false, \"rationale\": \"why not\"}\n"
                    "Slug must match [a-z0-9]+(-[a-z0-9]+)*."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"session_id={session_id}\n\n"
                    f"User:\n{user_message}\n\n"
                    f"Assistant:\n{assistant_reply}\n"
                ),
            },
        ]

        try:
            response = await llm.chat(classify_prompt, temperature=0.2)
            data = json.loads(_strip_code_fence(response.content or ""))
        except Exception:
            logger.exception("skill classification failed")
            return None

        if not data.get("propose"):
            log_event(
                logger,
                "skill_propose_skipped",
                session_id=session_id,
                rationale=str(data.get("rationale", ""))[:200],
            )
            return None

        slug = str(data.get("slug", "")).strip().lower()
        title = str(data.get("title", "")).strip() or slug
        description = str(data.get("description", "")).strip()
        rationale = str(data.get("rationale", "")).strip()
        if not _valid_slug(slug):
            log_event(logger, "skill_propose_invalid_slug", slug=slug)
            return None

        # If a skill with this slug already exists, do not re-propose.
        if (self._skills_dir / f"{slug}.md").exists():
            log_event(logger, "skill_propose_exists", slug=slug)
            return None

        # If a pending proposal for this slug already exists, do not duplicate.
        if memory is not None:
            existing = memory.get_pending_approval(slug)
            if existing is not None and existing.status == "pending":
                log_event(logger, "skill_propose_already_pending", slug=slug)
                return None

        proposal = {
            "slug": slug,
            "title": title,
            "description": description,
            "rationale": rationale,
            "session_id": session_id,
            "user_message": user_message,
            "assistant_reply": assistant_reply,
            "created_at": time.time(),
        }
        proposal_path = self._pending / f"{slug}.proposal.json"
        try:
            proposal_path.write_text(json.dumps(proposal, indent=2), encoding="utf-8")
        except OSError:
            logger.exception("failed to write skill proposal")
            return None

        expires_at = time.time() + self._proposal_timeout_seconds

        # Durability boundary: persist BEFORE notifying. If the Signal send
        # fails or the agent crashes right after, the proposal survives and
        # will be handled by the next sweep / admin reply.
        if memory is not None:
            try:
                memory.save_pending_approval(
                    slug=slug,
                    kind=self.APPROVAL_KIND,
                    recipient=recipient,
                    proposal_path=str(proposal_path),
                    payload={
                        "title": title,
                        "description": description,
                        "rationale": rationale,
                        "session_id": session_id,
                    },
                    expires_at=expires_at,
                )
            except Exception:
                logger.exception("failed to persist pending approval; rolling back proposal file")
                try:
                    proposal_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return None
        else:
            # No memory handle: fall back to an ephemeral proposal (no durability).
            log_event(logger, "skill_proposal_non_durable", slug=slug)

        log_event(
            logger,
            "skill_proposed",
            slug=slug,
            title=title,
            path=str(proposal_path),
            session_id=session_id,
            expires_at=expires_at,
            recipient=recipient or None,
        )

        if _skill_propose_callback is None:
            # Policy: never auto-build. No notification channel means no human
            # will ever approve, so mark the row rejected immediately to keep
            # the DB clean. (Pre-existing tests exercise this path.)
            log_event(logger, "skill_proposal_no_callback", slug=slug)
            if memory is not None:
                memory.decide_pending_approval(slug, approved=False)
            self._reject(proposal_path, reason="no_notification_channel")
            return proposal_path

        try:
            result = _skill_propose_callback(
                str(proposal_path), slug, title, description, rationale, expires_at
            )
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                await result
        except Exception:
            logger.exception("skill propose notification failed (proposal remains pending)")

        return proposal_path

    async def handle_decision(
        self,
        slug: str,
        *,
        approved: bool,
        memory: Any,
        llm: Any,
    ) -> bool:
        """Apply an admin decision. Atomic via ``memory.decide_pending_approval``.

        Returns True if the decision was applied, False if the row was already
        decided, expired, or unknown.
        """
        existing = memory.decide_pending_approval(slug, approved=approved)
        if existing is None:
            log_event(logger, "skill_decision_noop", slug=slug, approved=approved)
            return False

        log_event(
            logger,
            "skill_proposal_decision",
            slug=slug,
            approved=approved,
            proposal_path=existing.proposal_path,
        )

        proposal_path = Path(existing.proposal_path) if existing.proposal_path else None

        if approved:
            if proposal_path is None or not proposal_path.exists():
                log_event(logger, "skill_build_missing_proposal", slug=slug)
                return True
            try:
                await self.build_approved_skill(proposal_path, llm)
            except Exception:
                logger.exception("skill build after approval failed", extra={"slug": slug})
            return True

        # Rejected.
        if proposal_path is not None and proposal_path.exists():
            self._reject(proposal_path, reason="human_declined")
        return True

    async def run_startup_recovery(
        self, memory: Any, llm: Any | None = None
    ) -> tuple[list[Any], list[Any], list[Any]]:
        """Restart-safe recovery of durable approval state.

        Workflow:
            1. Snapshot every ``status='pending'`` row and partition into
               ``expired_while_down`` (``expires_at <= now``) and
               ``still_pending``.
            2. Flip expired rows to ``expired`` and move their proposal files
               to ``skills/rejected/``.
            3. Snapshot every ``status='approved'`` row whose skill file is
               missing — these are "approved-but-unbuilt" orphans (the agent
               crashed mid-body-draft). Schedule a grace-window build task
               per orphan (no build fires yet — that happens after the
               configured delay unless the admin cancels in the window).
            4. If any list is non-empty and a recovery callback is
               registered, invoke it ONCE with all three lists.

        Returns ``(still_pending, expired_while_down, approved_unbuilt)``.
        """
        self._ensure_dirs()
        now = time.time()
        pending_rows = memory.list_pending_approvals(kind=self.APPROVAL_KIND)
        still_pending = [r for r in pending_rows if r.expires_at > now]
        expired_candidates = [r for r in pending_rows if r.expires_at <= now]

        expired_applied: list[Any] = []
        if expired_candidates:
            expired_applied = memory.expire_pending_approvals(now=now)
            for row in expired_applied:
                if row.proposal_path:
                    p = Path(row.proposal_path)
                    if p.exists():
                        self._reject(p, reason="timeout_across_restart")
                log_event(
                    logger,
                    "skill_proposal_expired_at_startup",
                    slug=row.slug,
                    recipient=row.recipient or None,
                    expires_at=row.expires_at,
                )

        # --- Approved-but-unbuilt orphans ------------------------------------
        approved_rows = memory.list_approved_approvals(kind=self.APPROVAL_KIND)
        approved_unbuilt: list[Any] = []
        for row in approved_rows:
            skill_file = self._skills_dir / f"{row.slug}.md"
            if skill_file.exists():
                continue  # already built in a previous session
            proposal_path = Path(row.proposal_path) if row.proposal_path else None
            if proposal_path is None or not proposal_path.exists():
                # Proposal JSON is gone (archived) but the skill file is missing.
                # Can't rebuild; log and skip — the admin can re-draft by hand.
                log_event(
                    logger,
                    "skill_orphan_missing_proposal",
                    slug=row.slug,
                )
                continue
            approved_unbuilt.append(row)
            log_event(
                logger,
                "skill_orphan_detected",
                slug=row.slug,
                grace_window_seconds=self._build_grace_seconds,
            )
            if llm is not None:
                self._schedule_grace_build(row, proposal_path, memory, llm)

        log_event(
            logger,
            "skill_startup_recovery",
            still_pending=len(still_pending),
            expired_while_down=len(expired_applied),
            approved_unbuilt=len(approved_unbuilt),
        )

        if _skill_recovery_callback is not None and (
            still_pending or expired_applied or approved_unbuilt
        ):
            try:
                result = _skill_recovery_callback(
                    still_pending, expired_applied, approved_unbuilt
                )
                if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                    await result
            except Exception:
                logger.exception("skill recovery notice failed")

        return still_pending, expired_applied, approved_unbuilt

    def _schedule_grace_build(
        self, row: Any, proposal_path: Path, memory: Any, llm: Any
    ) -> asyncio.Task | None:
        """Spawn a background task that builds ``row`` after the grace window.

        The task re-checks row status and skill-file existence BEFORE calling
        the LLM, so a cancel landing in the window always wins. Safe to call
        multiple times per slug (idempotent via the pre-build check).
        """
        slug = row.slug
        grace = self._build_grace_seconds

        async def _deferred_build() -> None:
            try:
                if grace > 0:
                    await asyncio.sleep(grace)
                current = memory.get_pending_approval(slug)
                if current is None or current.status != "approved":
                    log_event(logger, "skill_grace_build_skipped", slug=slug,
                              reason="status_changed")
                    return
                skill_file = self._skills_dir / f"{slug}.md"
                if skill_file.exists():
                    log_event(logger, "skill_grace_build_skipped", slug=slug,
                              reason="already_built")
                    return
                if not proposal_path.exists():
                    log_event(logger, "skill_grace_build_skipped", slug=slug,
                              reason="proposal_gone")
                    return
                log_event(logger, "skill_grace_build_starting", slug=slug)
                await self.build_approved_skill(proposal_path, llm)
            except asyncio.CancelledError:
                log_event(logger, "skill_grace_build_cancelled", slug=slug)
                raise
            except Exception:
                logger.exception("grace-window build failed", extra={"slug": slug})

        try:
            return asyncio.create_task(_deferred_build(), name=f"mose-grace-build:{slug}")
        except RuntimeError:
            # No running loop (e.g. called from a sync operator script).
            logger.warning("skill_grace_build_no_loop", extra={"slug": slug})
            return None

    def cancel_approved_build(self, slug: str, memory: Any) -> bool:
        """Abort an approved-but-unbuilt skill during its grace window.

        Flips the DB row from ``approved`` to ``rejected`` with reason
        ``user_cancelled_build`` and moves the proposal JSON to
        ``skills/rejected/``. Returns True on success, False if the slug
        isn't in the expected state.
        """
        existing = memory.cancel_approved_approval(slug)
        if existing is None:
            return False
        if existing.proposal_path:
            p = Path(existing.proposal_path)
            if p.exists():
                self._reject(p, reason="user_cancelled_build")
        log_event(
            logger,
            "skill_build_cancelled",
            slug=slug,
            recipient=existing.recipient or None,
        )
        return True

    async def sweep_expired_approvals(
        self,
        memory: Any,
        *,
        reminder: bool = True,
    ) -> tuple[int, int]:
        """Run once at startup (and periodically) to handle durable state.

        - Any ``pending`` rows past ``expires_at`` are flipped to ``expired``
          and their proposal files are moved to ``skills/rejected/``.
        - If ``reminder=True`` and a reminder callback is registered, re-pings
          the admin for each proposal that is still pending after the sweep.

        Returns ``(expired_count, reminded_count)``.
        """
        self._ensure_dirs()
        expired_rows = memory.expire_pending_approvals()
        for row in expired_rows:
            log_event(
                logger,
                "skill_proposal_expired",
                slug=row.slug,
                recipient=row.recipient or None,
                expires_at=row.expires_at,
            )
            if row.proposal_path:
                p = Path(row.proposal_path)
                if p.exists():
                    self._reject(p, reason="timeout_across_restart")

        reminded = 0
        if reminder and _skill_reminder_callback is not None:
            still_pending = memory.list_pending_approvals(kind=self.APPROVAL_KIND)
            for row in still_pending:
                try:
                    result = _skill_reminder_callback(
                        row.slug,
                        str(row.payload.get("title", row.slug)),
                        str(row.payload.get("description", "")),
                        row.expires_at,
                    )
                    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                        await result
                    reminded += 1
                except Exception:
                    logger.exception("skill proposal reminder failed", extra={"slug": row.slug})
            if reminded:
                log_event(logger, "skill_proposal_reminders_sent", count=reminded)

        return len(expired_rows), reminded

    def _reject(self, proposal_path: Path, reason: str) -> None:
        try:
            dest = self._rejected / proposal_path.name
            if dest.exists():
                dest.unlink()
            shutil.move(str(proposal_path), str(dest))
            log_event(logger, "skill_proposal_rejected", path=str(dest), reason=reason)
        except OSError:
            logger.exception("failed to move rejected proposal")

    # ----------------------------------------------------------------- build

    async def build_approved_skill(self, proposal_path: Path, llm: Any) -> Path | None:
        """Stage 2 — the human approved; now ask the LLM to draft the full skill body."""
        try:
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("cannot read approved proposal")
            return None

        slug = str(proposal.get("slug", "")).strip()
        if not _valid_slug(slug):
            log_event(logger, "skill_build_bad_slug", slug=slug)
            return None

        title = str(proposal.get("title", slug)).strip()
        description = str(proposal.get("description", "")).strip()
        rationale = str(proposal.get("rationale", "")).strip()

        draft_prompt = [
            {
                "role": "system",
                "content": (
                    "Write a concise, high-quality SRE skill file in Markdown. "
                    "Audience: a senior SRE running against a Linux Docker host. "
                    "Structure the body as:\n"
                    "  # <Title>\n\n"
                    "  ## When to use\n  - bullets\n\n"
                    "  ## Steps\n  1. numbered steps with exact commands in fenced code blocks\n\n"
                    "  ## Verification\n  - how to confirm success\n\n"
                    "  ## Caveats\n  - risks, rollback, approval gates\n\n"
                    "Rules:\n"
                    "  - Never include destructive commands without an explicit approval gate note.\n"
                    "  - Use the `sre_execute` tool for state-changing operations and `bash` for read-only.\n"
                    "  - Do NOT include YAML frontmatter — the caller adds it.\n"
                    "  - Output ONLY the Markdown body."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Slug: {slug}\nTitle: {title}\nDescription: {description}\n"
                    f"Rationale: {rationale}\n\n"
                    f"Source session:\nUser:\n{proposal.get('user_message', '')}\n\n"
                    f"Assistant:\n{proposal.get('assistant_reply', '')}\n"
                ),
            },
        ]

        try:
            response = await llm.chat(draft_prompt, temperature=0.3)
            body = _strip_code_fence(response.content or "").strip()
        except Exception:
            logger.exception("skill body draft LLM call failed")
            return None

        if not body:
            log_event(logger, "skill_build_empty_body", slug=slug)
            return None

        frontmatter = (
            "---\n"
            f"name: {slug}\n"
            f"description: {description or title}\n"
            'version: "0.1.0"\n'
            f"source_session: {proposal.get('session_id', '')}\n"
            f"approved_at: {datetime.now(timezone.utc).isoformat()}\n"
            "---\n\n"
        )
        if body.lstrip().startswith("#"):
            content = frontmatter + body
        else:
            content = frontmatter + f"# {title}\n\n" + body

        skill_path = self._skills_dir / f"{slug}.md"
        try:
            skill_path.write_text(content, encoding="utf-8")
        except OSError:
            logger.exception("failed to write approved skill")
            return None

        # Archive the proposal with a trailing .approved marker.
        try:
            archived = self._rejected.parent / "approved"
            archived.mkdir(parents=True, exist_ok=True)
            shutil.move(str(proposal_path), str(archived / proposal_path.name))
        except OSError:
            logger.warning("could not archive approved proposal", exc_info=True)

        log_event(
            logger,
            "skill_built",
            slug=slug,
            path=str(skill_path),
            bytes=len(content),
        )
        return skill_path

    # ----------------------------------------------------------------- review

    async def review_skills(
        self,
        memory: Any,
        llm: Any | None = None,
        *,
        notify: bool = True,
    ) -> Path | None:
        """Scheduled skill quality review.

        Produces a full Markdown report at ``<log_dir>/skill-review-YYYY-MM-DD.md``
        and emits structured log events. When an LLM is supplied, the report
        includes per-skill recommendations (rewrite / disable / keep). When
        ``notify`` is true and a review callback is registered, the human is
        pinged with a short summary; the human retains full control — the
        learner NEVER edits or deletes skills on its own.
        """
        if not self._cfg.enabled:
            return None
        self._ensure_dirs()

        skill_files = sorted(p for p in self._skills_dir.glob("*.md") if p.is_file())
        failure_rates: dict[str, float] = {}
        usage_counts: dict[str, int] = {}
        try:
            failure_rates = memory.skill_failure_rates()
        except Exception:
            logger.exception("skill_failure_rates unavailable")
        try:
            usage_counts = getattr(memory, "skill_usage_counts", lambda: {})()
        except Exception:
            usage_counts = {}

        threshold = float(self._cfg.skill_review_failure_threshold)
        min_samples = int(getattr(self._cfg, "review_min_samples", 5))

        candidates: list[dict[str, Any]] = []
        overview: list[dict[str, Any]] = []
        for p in skill_files:
            slug = p.stem
            rate = failure_rates.get(slug, 0.0)
            count = usage_counts.get(slug, 0)
            entry = {
                "slug": slug,
                "path": str(p),
                "failure_rate": round(rate, 3),
                "usage_count": count,
            }
            overview.append(entry)
            if count >= min_samples and rate >= threshold:
                candidates.append(entry)

        # Ask the LLM for recommendations on each candidate (optional).
        recommendations: dict[str, dict[str, str]] = {}
        if llm is not None and candidates:
            for cand in candidates:
                slug = cand["slug"]
                try:
                    body = Path(cand["path"]).read_text(encoding="utf-8")
                except OSError:
                    body = ""
                prompt = [
                    {
                        "role": "system",
                        "content": (
                            "You are reviewing an SRE agent skill that is failing more "
                            "often than expected. Return JSON only: "
                            '{"action": "rewrite|disable|keep|delete", '
                            '"reason": "short justification", '
                            '"suggested_changes": "optional bullet list as a single string"}. '
                            "Never recommend delete unless the skill is obviously obsolete."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"slug={slug}\n"
                            f"failure_rate={cand['failure_rate']}\n"
                            f"usage_count={cand['usage_count']}\n\n"
                            f"---\n{body}\n---"
                        ),
                    },
                ]
                try:
                    resp = await llm.chat(prompt, temperature=0.2)
                    recommendations[slug] = json.loads(_strip_code_fence(resp.content or ""))
                except Exception:
                    logger.exception("skill review LLM recommendation failed", extra={"slug": slug})
                    recommendations[slug] = {"action": "keep", "reason": "llm_error"}

        # Render the Markdown write-up.
        report_md = self._render_review_report(overview, candidates, recommendations, threshold, min_samples)

        # Persist the report.
        report_path: Path | None = None
        if self._log_dir is not None:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            report_path = self._log_dir / f"skill-review-{stamp}.md"
            try:
                report_path.write_text(report_md, encoding="utf-8")
            except OSError:
                logger.exception("failed to write skill review report")
                report_path = None

        log_event(
            logger,
            "skill_review_completed",
            skills_total=len(skill_files),
            candidates=len(candidates),
            threshold=threshold,
            min_samples=min_samples,
            report=str(report_path) if report_path else None,
        )

        if notify and _skill_review_callback is not None and report_path is not None:
            try:
                summary = self._render_review_summary(overview, candidates, recommendations)
                result = _skill_review_callback(str(report_path), summary)
                if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                    await result
            except Exception:
                logger.exception("skill review notification failed")

        return report_path

    # ----------------------------------------------------------- report render

    @staticmethod
    def _render_review_report(
        overview: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        recommendations: dict[str, dict[str, str]],
        threshold: float,
        min_samples: int,
    ) -> str:
        now = datetime.now(timezone.utc).isoformat()
        lines: list[str] = []
        lines.append(f"# Skill Review — {now}")
        lines.append("")
        lines.append(
            f"- Total skills: **{len(overview)}**  "
            f"- Candidates for action: **{len(candidates)}**  "
            f"- Failure threshold: **{threshold:.0%}**  "
            f"- Min samples: **{min_samples}**"
        )
        lines.append("")
        lines.append("## Overview")
        lines.append("")
        lines.append("| Skill | Uses | Failure rate |")
        lines.append("|---|---:|---:|")
        for o in sorted(overview, key=lambda e: (-e["usage_count"], e["slug"])):
            lines.append(f"| `{o['slug']}` | {o['usage_count']} | {o['failure_rate']:.0%} |")
        lines.append("")
        if not candidates:
            lines.append("_No skills exceeded the review threshold. No action recommended._")
            return "\n".join(lines)

        lines.append("## Candidates for human review")
        lines.append("")
        lines.append(
            "The agent **will not** modify or delete skills automatically. "
            "Each item below is a recommendation for a human to review and apply."
        )
        lines.append("")
        for cand in candidates:
            slug = cand["slug"]
            rec = recommendations.get(slug, {})
            action = str(rec.get("action", "review")).strip() or "review"
            reason = str(rec.get("reason", "")).strip()
            suggested = str(rec.get("suggested_changes", "")).strip()
            lines.append(f"### `{slug}`")
            lines.append("")
            lines.append(
                f"- Failure rate: **{cand['failure_rate']:.0%}** over "
                f"**{cand['usage_count']}** uses"
            )
            lines.append(f"- Recommended action: **{action}**")
            if reason:
                lines.append(f"- Reason: {reason}")
            if suggested:
                lines.append("- Suggested changes:")
                for line in suggested.splitlines():
                    line = line.strip("- ").strip()
                    if line:
                        lines.append(f"  - {line}")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _render_review_summary(
        overview: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        recommendations: dict[str, dict[str, str]],
    ) -> str:
        if not candidates:
            return (
                f"Skill review: {len(overview)} skills checked. No action recommended. "
                "Full report saved to the log directory."
            )
        top = []
        for cand in candidates[:5]:
            rec = recommendations.get(cand["slug"], {})
            top.append(
                f"- {cand['slug']}: {cand['failure_rate']:.0%} failure over "
                f"{cand['usage_count']} uses -> {rec.get('action', 'review')}"
            )
        body = "\n".join(top)
        return (
            f"Skill review flagged {len(candidates)} skill(s) of {len(overview)}.\n"
            f"{body}\n\n"
            "The agent made NO changes. Approve any actions manually."
        )
