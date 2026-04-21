"""Tests for the durable propose-first skill learning loop and skill review."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mose.config import LearningConfig, MemoryConfig
from mose.learning import (
    SkillLearner,
    handle_skill_decision,
    init_skill_decision_runtime,
    init_skill_promotion,
    init_skill_recovery_notice,
    init_skill_reminder,
    init_skill_review,
)
from mose.llm import LLMResponse
from mose.memory import MemoryManager


def _make_learner(tmp_path: Path, *, timeout: int = 3600) -> SkillLearner:
    cfg = LearningConfig(
        enabled=True,
        pending_dir=str(tmp_path / "skills" / "pending"),
        rejected_dir=str(tmp_path / "skills" / "rejected"),
        min_tools_used=2,
        skill_review_failure_threshold=0.3,
        review_min_samples=2,
        review_log_dir=str(tmp_path / "logs"),
    )
    return SkillLearner(
        cfg,
        skills_dir=tmp_path / "skills",
        log_dir=tmp_path / "logs",
        proposal_timeout_seconds=timeout,
    )


def _make_memory(tmp_path: Path) -> MemoryManager:
    mem = MemoryManager(MemoryConfig(
        db_path=str(tmp_path / "memory.db"),
        embedding_model="nomic-ai/nomic-embed-text-v1.5",
        embedding_dimensions=384,
    ))
    mem.search = MagicMock(return_value=[])  # never load the embedder in tests
    return mem


class TestProposeFirstFlow:
    async def test_not_enough_tools_no_proposal(self, tmp_path):
        init_skill_promotion(None)
        learner = _make_learner(tmp_path)
        llm = MagicMock()
        llm.chat = AsyncMock()
        result = await learner.maybe_propose_skill(
            "s1", "msg", "reply", total_native_tool_calls=1,
            had_tool_error=False, llm=llm, memory=None,
        )
        assert result is None
        llm.chat.assert_not_called()

    async def test_had_error_no_proposal(self, tmp_path):
        init_skill_promotion(None)
        learner = _make_learner(tmp_path)
        llm = MagicMock()
        llm.chat = AsyncMock()
        assert await learner.maybe_propose_skill(
            "s1", "m", "r", 5, True, llm, memory=None,
        ) is None

    async def test_llm_says_no_skip(self, tmp_path):
        init_skill_promotion(None)
        learner = _make_learner(tmp_path)
        llm = MagicMock()
        llm.chat = AsyncMock(return_value=LLMResponse(
            content=json.dumps({"propose": False, "rationale": "one-off"})
        ))
        result = await learner.maybe_propose_skill(
            "s1", "m", "r", 5, False, llm, memory=None,
        )
        assert result is None

    async def test_no_callback_rejects_immediately(self, tmp_path):
        """Policy: no notification channel => row is rejected, no skill built."""
        init_skill_promotion(None)
        learner = _make_learner(tmp_path)
        memory = _make_memory(tmp_path)

        llm = MagicMock()
        llm.chat = AsyncMock(return_value=LLMResponse(content=json.dumps({
            "propose": True,
            "slug": "no-callback",
            "title": "x",
            "description": "d",
            "rationale": "r",
        })))

        await learner.maybe_propose_skill(
            "s", "m", "r", 3, False, llm, memory=memory, recipient="+12",
        )
        assert not (tmp_path / "skills" / "no-callback.md").exists()
        assert (tmp_path / "skills" / "rejected" / "no-callback.proposal.json").exists()
        row = memory.get_pending_approval("no-callback")
        assert row is not None
        assert row.status == "rejected"
        memory.close()

    async def test_propose_persists_and_is_fire_and_forget(self, tmp_path):
        """Callback is notification-only; DB row stays 'pending' until a decision."""
        sent: list[tuple[str, str]] = []

        async def notify(path, slug, title, desc, rationale, expires_at):
            sent.append((slug, title))

        init_skill_promotion(notify)
        learner = _make_learner(tmp_path, timeout=7200)
        memory = _make_memory(tmp_path)

        llm = MagicMock()
        llm.chat = AsyncMock(return_value=LLMResponse(content=json.dumps({
            "propose": True,
            "slug": "restart-nginx",
            "title": "Restart nginx",
            "description": "Graceful restart",
            "rationale": "Recurring runbook task",
        })))

        path = await learner.maybe_propose_skill(
            "sess-1", "renew cert", "reloaded nginx", 4, False, llm,
            memory=memory, recipient="+15551234567",
        )
        assert path is not None
        assert sent == [("restart-nginx", "Restart nginx")]

        # Pending row exists; skill file does NOT exist yet.
        row = memory.get_pending_approval("restart-nginx")
        assert row is not None
        assert row.status == "pending"
        assert row.recipient == "+15551234567"
        assert row.expires_at > time.time() + 3000
        assert not (tmp_path / "skills" / "restart-nginx.md").exists()
        memory.close()

    async def test_handle_decision_approve_builds_skill(self, tmp_path):
        async def notify(*_args, **_kw):
            pass

        init_skill_promotion(notify)
        learner = _make_learner(tmp_path)
        memory = _make_memory(tmp_path)

        llm = MagicMock()
        # Classification then body draft
        llm.chat = AsyncMock(side_effect=[
            LLMResponse(content=json.dumps({
                "propose": True, "slug": "foo", "title": "Foo",
                "description": "d", "rationale": "r",
            })),
            LLMResponse(content="## Steps\n1. do a thing\n"),
        ])

        await learner.maybe_propose_skill(
            "s", "m", "r", 3, False, llm, memory=memory, recipient="+1",
        )
        assert memory.get_pending_approval("foo").status == "pending"

        # Simulate admin approval
        init_skill_decision_runtime(learner=learner, memory=memory, llm=llm)
        applied = await handle_skill_decision("foo", approved=True)
        assert applied is True
        assert (tmp_path / "skills" / "foo.md").exists()
        assert memory.get_pending_approval("foo").status == "approved"

        # Idempotent: second call is a noop.
        assert await handle_skill_decision("foo", approved=True) is False
        memory.close()

    async def test_handle_decision_reject_moves_proposal(self, tmp_path):
        async def notify(*_args, **_kw):
            pass

        init_skill_promotion(notify)
        learner = _make_learner(tmp_path)
        memory = _make_memory(tmp_path)

        llm = MagicMock()
        llm.chat = AsyncMock(return_value=LLMResponse(content=json.dumps({
            "propose": True, "slug": "skip-me", "title": "Skip",
            "description": "d", "rationale": "r",
        })))
        await learner.maybe_propose_skill(
            "s", "m", "r", 3, False, llm, memory=memory, recipient="+1",
        )

        init_skill_decision_runtime(learner=learner, memory=memory, llm=llm)
        applied = await handle_skill_decision("skip-me", approved=False)
        assert applied is True
        assert memory.get_pending_approval("skip-me").status == "rejected"
        assert not (tmp_path / "skills" / "skip-me.md").exists()
        assert (tmp_path / "skills" / "rejected" / "skip-me.proposal.json").exists()
        memory.close()

    async def test_duplicate_proposal_is_suppressed(self, tmp_path):
        """A second propose for a slug with a pending row must be a no-op."""
        async def notify(*_args, **_kw):
            pass

        init_skill_promotion(notify)
        learner = _make_learner(tmp_path)
        memory = _make_memory(tmp_path)

        llm = MagicMock()
        llm.chat = AsyncMock(return_value=LLMResponse(content=json.dumps({
            "propose": True, "slug": "dup", "title": "t", "description": "d", "rationale": "r",
        })))
        first = await learner.maybe_propose_skill(
            "s", "m", "r", 3, False, llm, memory=memory, recipient="+1",
        )
        second = await learner.maybe_propose_skill(
            "s", "m2", "r2", 3, False, llm, memory=memory, recipient="+1",
        )
        assert first is not None
        assert second is None
        memory.close()


class TestSweepExpiredApprovals:
    async def test_sweep_moves_expired_to_rejected(self, tmp_path):
        async def notify(*_args, **_kw):
            pass

        init_skill_promotion(notify)
        init_skill_reminder(None)
        # Timeout=60s but we'll rewrite expires_at to the past.
        learner = _make_learner(tmp_path, timeout=60)
        memory = _make_memory(tmp_path)

        llm = MagicMock()
        llm.chat = AsyncMock(return_value=LLMResponse(content=json.dumps({
            "propose": True, "slug": "stale", "title": "t", "description": "d", "rationale": "r",
        })))
        await learner.maybe_propose_skill(
            "s", "m", "r", 3, False, llm, memory=memory, recipient="+1",
        )

        # Age the row past its expiry.
        memory.db.execute(
            "UPDATE pending_approvals SET expires_at = ? WHERE slug = ?",
            (time.time() - 1, "stale"),
        )
        memory.db.commit()

        expired, reminded = await learner.sweep_expired_approvals(memory, reminder=False)
        assert expired == 1
        assert reminded == 0
        assert memory.get_pending_approval("stale").status == "expired"
        assert (tmp_path / "skills" / "rejected" / "stale.proposal.json").exists()
        memory.close()

    async def test_sweep_reminds_still_pending(self, tmp_path):
        reminders: list[tuple[str, float]] = []

        async def notify(*_args, **_kw):
            pass

        async def remind(slug, title, description, expires_at):
            reminders.append((slug, expires_at))

        init_skill_promotion(notify)
        init_skill_reminder(remind)
        learner = _make_learner(tmp_path, timeout=3600)
        memory = _make_memory(tmp_path)

        llm = MagicMock()
        llm.chat = AsyncMock(return_value=LLMResponse(content=json.dumps({
            "propose": True, "slug": "alive", "title": "t", "description": "d", "rationale": "r",
        })))
        await learner.maybe_propose_skill(
            "s", "m", "r", 3, False, llm, memory=memory, recipient="+1",
        )

        expired, reminded = await learner.sweep_expired_approvals(memory, reminder=True)
        assert expired == 0
        assert reminded == 1
        assert reminders[0][0] == "alive"
        memory.close()


class TestStartupRecovery:
    """Restart-safe behavior: present all outstanding asks in one consolidated notice."""

    async def _seed_proposal(
        self, learner: SkillLearner, memory: MemoryManager, slug: str, *, llm=None
    ) -> None:
        async def notify(*_a, **_kw):
            pass

        init_skill_promotion(notify)
        llm = llm or MagicMock()
        llm.chat = AsyncMock(return_value=LLMResponse(content=json.dumps({
            "propose": True, "slug": slug, "title": f"T-{slug}",
            "description": "d", "rationale": "r",
        })))
        await learner.maybe_propose_skill(
            f"session-{slug}", "m", "r", 3, False, llm, memory=memory, recipient="+1",
        )

    async def test_no_outstanding_sends_no_notice(self, tmp_path):
        received: list = []
        async def notice(still, expired, orphans):
            received.append((list(still), list(expired), list(orphans)))
        init_skill_recovery_notice(notice)
        init_skill_reminder(None)

        learner = _make_learner(tmp_path)
        memory = _make_memory(tmp_path)

        still, expired, orphans = await learner.run_startup_recovery(memory)
        assert still == [] and expired == [] and orphans == []
        assert received == [], "callback must NOT fire when nothing is outstanding"
        memory.close()

    async def test_presents_still_pending_and_expired_in_one_call(self, tmp_path):
        received: list = []
        async def notice(still, expired, orphans):
            received.append((list(still), list(expired), list(orphans)))
        init_skill_recovery_notice(notice)
        init_skill_reminder(None)

        learner = _make_learner(tmp_path, timeout=3600)
        memory = _make_memory(tmp_path)

        await self._seed_proposal(learner, memory, "alive")
        await self._seed_proposal(learner, memory, "stale")
        memory.db.execute(
            "UPDATE pending_approvals SET expires_at = ? WHERE slug = ?",
            (time.time() - 1, "stale"),
        )
        memory.db.commit()

        still, expired, orphans = await learner.run_startup_recovery(memory)
        assert len(received) == 1, "consolidated notice must fire exactly once"
        still_cb, expired_cb, orphan_cb = received[0]
        assert [r.slug for r in still_cb] == ["alive"]
        assert [r.slug for r in expired_cb] == ["stale"]
        assert orphan_cb == []
        assert memory.get_pending_approval("stale").status == "expired"
        assert (tmp_path / "skills" / "rejected" / "stale.proposal.json").exists()
        assert len(still) == 1 and len(expired) == 1 and len(orphans) == 0
        memory.close()

    async def test_expired_items_flagged_informational_not_pending(self, tmp_path):
        """Regression: expired items MUST NOT ask for a decision (user requirement)."""
        received: list = []
        async def notice(still, expired, orphans):
            received.append((list(still), list(expired), list(orphans)))
        init_skill_recovery_notice(notice)
        init_skill_reminder(None)

        learner = _make_learner(tmp_path, timeout=3600)
        memory = _make_memory(tmp_path)

        await self._seed_proposal(learner, memory, "ghost")
        memory.db.execute(
            "UPDATE pending_approvals SET expires_at = ? WHERE slug = ?",
            (time.time() - 60, "ghost"),
        )
        memory.db.commit()

        still, expired, orphans = await learner.run_startup_recovery(memory)
        assert still == []
        assert [r.slug for r in expired] == ["ghost"]
        assert orphans == []
        assert len(received) == 1
        assert received[0][0] == []  # no pending decisions requested
        assert [r.slug for r in received[0][1]] == ["ghost"]
        memory.close()

    async def test_no_callback_still_processes_expirations(self, tmp_path):
        """Durability guarantee: DB aging happens even when no notifier is wired."""
        init_skill_recovery_notice(None)
        init_skill_reminder(None)

        learner = _make_learner(tmp_path, timeout=3600)
        memory = _make_memory(tmp_path)

        await self._seed_proposal(learner, memory, "orphan")
        memory.db.execute(
            "UPDATE pending_approvals SET expires_at = ? WHERE slug = ?",
            (time.time() - 1, "orphan"),
        )
        memory.db.commit()

        still, expired, orphans = await learner.run_startup_recovery(memory)
        assert still == []
        assert [r.slug for r in expired] == ["orphan"]
        assert orphans == []
        assert memory.get_pending_approval("orphan").status == "expired"
        memory.close()


class TestApprovedButUnbuiltGraceWindow:
    """Orphaned approvals (crashed mid-body-draft) get a 15-min grace window."""

    @staticmethod
    def _learner_with_grace(tmp_path: Path, *, grace: int) -> SkillLearner:
        cfg = LearningConfig(
            enabled=True,
            pending_dir=str(tmp_path / "skills" / "pending"),
            rejected_dir=str(tmp_path / "skills" / "rejected"),
            min_tools_used=2,
            skill_review_failure_threshold=0.3,
            review_min_samples=2,
            review_log_dir=str(tmp_path / "logs"),
        )
        return SkillLearner(
            cfg,
            skills_dir=tmp_path / "skills",
            log_dir=tmp_path / "logs",
            proposal_timeout_seconds=3600,
            build_grace_window_seconds=grace,
        )

    async def _seed_approved_orphan(
        self, learner: SkillLearner, memory: MemoryManager, slug: str
    ) -> Path:
        """Seed a pending proposal, flip it to approved, but DO NOT build the skill file."""
        async def notify(*_a, **_kw):
            pass

        init_skill_promotion(notify)
        llm = MagicMock()
        llm.chat = AsyncMock(return_value=LLMResponse(content=json.dumps({
            "propose": True, "slug": slug, "title": f"T-{slug}",
            "description": "d", "rationale": "r",
        })))
        await learner.maybe_propose_skill(
            f"session-{slug}", "m", "r", 3, False, llm, memory=memory, recipient="+1",
        )
        # Simulate: admin approved, agent crashed before build_approved_skill ran.
        row = memory.decide_pending_approval(slug, approved=True)
        assert row is not None
        return Path(memory.get_pending_approval(slug).proposal_path)

    async def test_orphan_detected_and_listed(self, tmp_path):
        received: list = []
        async def notice(still, expired, orphans):
            received.append((list(still), list(expired), list(orphans)))
        init_skill_recovery_notice(notice)

        learner = self._learner_with_grace(tmp_path, grace=300)
        memory = _make_memory(tmp_path)
        await self._seed_approved_orphan(learner, memory, "ghost-skill")

        still, expired, orphans = await learner.run_startup_recovery(memory, llm=MagicMock())
        assert still == []
        assert expired == []
        assert [r.slug for r in orphans] == ["ghost-skill"]
        assert len(received) == 1
        assert [r.slug for r in received[0][2]] == ["ghost-skill"]
        memory.close()

    async def test_orphan_with_existing_skill_file_is_not_listed(self, tmp_path):
        """If the skill .md already exists, the row is NOT an orphan — no re-build."""
        init_skill_recovery_notice(None)
        learner = self._learner_with_grace(tmp_path, grace=300)
        memory = _make_memory(tmp_path)
        await self._seed_approved_orphan(learner, memory, "already-done")
        (tmp_path / "skills").mkdir(exist_ok=True)
        (tmp_path / "skills" / "already-done.md").write_text("# built\n")

        still, expired, orphans = await learner.run_startup_recovery(memory, llm=MagicMock())
        assert orphans == []
        memory.close()

    async def test_grace_build_auto_fires_after_window(self, tmp_path):
        """With a near-zero grace, the deferred build runs and writes the .md file."""
        init_skill_recovery_notice(None)
        learner = self._learner_with_grace(tmp_path, grace=0)
        memory = _make_memory(tmp_path)
        await self._seed_approved_orphan(learner, memory, "auto-build")

        llm = MagicMock()
        llm.chat = AsyncMock(return_value=LLMResponse(
            content="## When to use\n- things\n\n## Steps\n1. run `ls`\n"
        ))

        _still, _expired, orphans = await learner.run_startup_recovery(memory, llm=llm)
        assert [r.slug for r in orphans] == ["auto-build"]
        # Let the scheduled task complete.
        pending = [t for t in asyncio.all_tasks() if t.get_name().startswith("mose-grace-build:")]
        assert pending, "grace-window build task should have been scheduled"
        for t in pending:
            await t
        assert (tmp_path / "skills" / "auto-build.md").exists()
        memory.close()

    async def test_cancel_during_grace_prevents_build(self, tmp_path):
        """Cancel flips DB to rejected, moves proposal, and the deferred task aborts."""
        init_skill_recovery_notice(None)
        # Non-zero grace so we can cancel before it fires.
        learner = self._learner_with_grace(tmp_path, grace=60)
        memory = _make_memory(tmp_path)
        await self._seed_approved_orphan(learner, memory, "abort-me")

        llm = MagicMock()
        llm.chat = AsyncMock(return_value=LLMResponse(content="BODY\n"))

        _still, _expired, orphans = await learner.run_startup_recovery(memory, llm=llm)
        assert [r.slug for r in orphans] == ["abort-me"]

        ok = learner.cancel_approved_build("abort-me", memory)
        assert ok is True
        row = memory.get_pending_approval("abort-me")
        assert row.status == "rejected"
        assert (tmp_path / "skills" / "rejected" / "abort-me.proposal.json").exists()
        assert not (tmp_path / "skills" / "abort-me.md").exists()

        # The deferred task should not build now — simulate its wakeup by
        # allowing it to complete (sleep() is in real time; cancel the task
        # to avoid waiting 60s). We assert the DB guard by re-invoking the
        # pre-build check path directly.
        for t in list(asyncio.all_tasks()):
            if t.get_name().startswith("mose-grace-build:"):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        # Final guarantee: no skill file appeared.
        assert not (tmp_path / "skills" / "abort-me.md").exists()
        memory.close()

    async def test_cancel_without_orphan_is_noop(self, tmp_path):
        learner = self._learner_with_grace(tmp_path, grace=60)
        memory = _make_memory(tmp_path)
        assert learner.cancel_approved_build("nope", memory) is False
        memory.close()


class TestApprovalReplyParser:
    def test_parse_cases(self):
        from mose.signal_bot import _parse_approval_reply
        assert _parse_approval_reply("approve foo") == ("foo", "approve")
        assert _parse_approval_reply("yes foo") == ("foo", "approve")
        assert _parse_approval_reply("y foo-bar") == ("foo-bar", "approve")
        assert _parse_approval_reply("reject foo") == ("foo", "reject")
        assert _parse_approval_reply("no foo") == ("foo", "reject")
        assert _parse_approval_reply("n foo") == ("foo", "reject")
        assert _parse_approval_reply("approve slug=my-skill") == ("my-skill", "approve")
        assert _parse_approval_reply("yes") == (None, "approve")
        assert _parse_approval_reply("no") == (None, "reject")
        assert _parse_approval_reply("hello there") == (None, None)
        assert _parse_approval_reply("") == (None, None)

    def test_cancel_verbs(self):
        from mose.signal_bot import _parse_approval_reply
        assert _parse_approval_reply("stop foo") == ("foo", "cancel")
        assert _parse_approval_reply("cancel my-skill") == ("my-skill", "cancel")
        assert _parse_approval_reply("abort") == (None, "cancel")
        assert _parse_approval_reply("halt slug=bar") == ("bar", "cancel")


class TestReviewSkills:
    async def test_no_skills_no_candidates(self, tmp_path):
        init_skill_review(None)
        learner = _make_learner(tmp_path)
        memory = MagicMock()
        memory.skill_failure_rates.return_value = {}
        memory.skill_usage_counts.return_value = {}
        report = await learner.review_skills(memory, llm=None, notify=False)
        assert report is not None
        text = report.read_text(encoding="utf-8")
        assert "Total skills: **0**" in text

    async def test_flags_high_failure_skill(self, tmp_path):
        init_skill_review(None)
        learner = _make_learner(tmp_path)
        (tmp_path / "skills").mkdir(exist_ok=True)
        (tmp_path / "skills" / "flaky.md").write_text("# flaky\nbody\n")

        memory = MagicMock()
        memory.skill_failure_rates.return_value = {"flaky": 0.6}
        memory.skill_usage_counts.return_value = {"flaky": 10}

        llm = MagicMock()
        llm.chat = AsyncMock(return_value=LLMResponse(content=json.dumps({
            "action": "rewrite",
            "reason": "ambiguous steps",
            "suggested_changes": "add verification",
        })))

        report = await learner.review_skills(memory, llm=llm, notify=False)
        text = report.read_text(encoding="utf-8")
        assert "Candidates for action: **1**" in text
        assert "`flaky`" in text
        assert "rewrite" in text
