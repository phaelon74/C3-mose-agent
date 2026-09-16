"""Tests for pending_approvals_list and skill_proposal_get tools."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mose.config import MemoryConfig
from mose.memory import MemoryManager
from mose.tools import (
    _tool_pending_approvals_list,
    _tool_skill_proposal_get,
    init_tracker_tool_context,
)


@pytest.fixture
def memory(tmp_path):
    mem = MemoryManager(MemoryConfig(
        db_path=str(tmp_path / "memory.db"),
        embedding_model="nomic-ai/nomic-embed-text-v1.5",
        embedding_dimensions=384,
    ))
    mem.search = MagicMock(return_value=[])
    init_tracker_tool_context(memory=mem, config=MagicMock(), get_scheduler=lambda: None)
    return mem


class TestPendingApprovalsList:
    async def test_empty_list(self, memory):
        result = await _tool_pending_approvals_list({})
        assert json.loads(result) == []

    async def test_skill_proposal_row(self, memory, tmp_path):
        proposal_path = tmp_path / "skills" / "pending" / "foo-bar.proposal.json"
        proposal_path.parent.mkdir(parents=True)
        proposal_path.write_text(
            json.dumps({"slug": "foo-bar", "tool_trace": [{"tool": "bash"}]}),
            encoding="utf-8",
        )
        expires = time.time() + 3600
        memory.save_pending_approval(
            slug="foo-bar",
            kind="skill_proposal",
            recipient="signal:admin",
            proposal_path=str(proposal_path),
            payload={
                "title": "Foo Bar Skill",
                "description": "Does foo things",
                "rationale": "Reusable pattern",
            },
            expires_at=expires,
        )
        result = json.loads(await _tool_pending_approvals_list({}))
        assert len(result) == 1
        assert result[0]["slug"] == "foo-bar"
        assert result[0]["kind"] == "skill_proposal"
        assert result[0]["title"] == "Foo Bar Skill"
        assert result[0]["description"] == "Does foo things"
        assert "expires_at" in result[0]

    async def test_kind_filter(self, memory):
        expires = time.time() + 3600
        memory.save_pending_approval(
            slug="skill-one",
            kind="skill_proposal",
            recipient="signal:admin",
            proposal_path="",
            payload={"title": "S"},
            expires_at=expires,
        )
        memory.save_pending_approval(
            slug="task-one",
            kind="scheduled_task_proposal",
            recipient="signal:admin",
            proposal_path="",
            payload={"title": "T"},
            expires_at=expires,
        )
        skills = json.loads(await _tool_pending_approvals_list({"kind": "skill_proposal"}))
        assert [r["slug"] for r in skills] == ["skill-one"]
        tasks = json.loads(
            await _tool_pending_approvals_list({"kind": "scheduled_task_proposal"})
        )
        assert [r["slug"] for r in tasks] == ["task-one"]

    async def test_invalid_kind(self, memory):
        result = await _tool_pending_approvals_list({"kind": "not_a_kind"})
        assert result.startswith("Error:")

    async def test_include_payload_tool_trace_count(self, memory, tmp_path):
        proposal_path = tmp_path / "pending" / "trace-skill.proposal.json"
        proposal_path.parent.mkdir(parents=True)
        proposal_path.write_text(
            json.dumps({"tool_trace": [{"tool": "a"}, {"tool": "b"}]}),
            encoding="utf-8",
        )
        memory.save_pending_approval(
            slug="trace-skill",
            kind="skill_proposal",
            recipient="signal:admin",
            proposal_path=str(proposal_path),
            payload={"title": "T", "rationale": "Because"},
            expires_at=time.time() + 3600,
        )
        result = json.loads(
            await _tool_pending_approvals_list({"include_payload": True})
        )
        assert result[0]["tool_trace_count"] == 2
        assert result[0]["rationale"] == "Because"


class TestSkillProposalGet:
    async def test_not_found(self, memory):
        result = await _tool_skill_proposal_get({"slug": "missing"})
        assert "no pending skill proposal" in result

    async def test_wrong_kind(self, memory):
        memory.save_pending_approval(
            slug="task-upd-foo",
            kind="scheduled_task_update",
            recipient="signal:admin",
            proposal_path="",
            payload={"title": "Update"},
            expires_at=time.time() + 3600,
        )
        result = await _tool_skill_proposal_get({"slug": "task-upd-foo"})
        assert "not a skill proposal" in result

    async def test_reads_proposal_json(self, memory, tmp_path):
        proposal_path = tmp_path / "radarr-queue.proposal.json"
        proposal_path.write_text(
            json.dumps({
                "slug": "radarr-queue",
                "title": "Queue Purge",
                "tool_trace": [{"tool": "portal_codemode_execute"}],
            }),
            encoding="utf-8",
        )
        memory.save_pending_approval(
            slug="radarr-queue",
            kind="skill_proposal",
            recipient="signal:admin",
            proposal_path=str(proposal_path),
            payload={
                "title": "Queue Purge",
                "description": "Purge incomplete downloads",
                "rationale": "Safe validation pattern",
            },
            expires_at=time.time() + 3600,
        )
        result = json.loads(await _tool_skill_proposal_get({"slug": "radarr-queue"}))
        assert result["title"] == "Queue Purge"
        assert result["rationale"] == "Safe validation pattern"
        assert result["tool_trace_count"] == 1

    async def test_skill_update_kind(self, memory, tmp_path):
        proposal_path = tmp_path / "skill-upd-purge.proposal.json"
        proposal_path.write_text(
            json.dumps({
                "slug": "purge-queue-samples",
                "is_update": True,
                "draft_markdown": "# Revised\n\nUse statusMessages.\n",
            }),
            encoding="utf-8",
        )
        memory.save_pending_approval(
            slug="skill-upd-purge-queue-samples",
            kind="skill_update",
            recipient="signal:admin",
            proposal_path=str(proposal_path),
            payload={
                "title": "Update: Purge Queue Samples",
                "description": "Fix sample flag",
                "rationale": "Wrong field",
                "target_slug": "purge-queue-samples",
                "is_update": True,
                "has_draft": True,
            },
            expires_at=time.time() + 3600,
        )
        result = json.loads(
            await _tool_skill_proposal_get({"slug": "skill-upd-purge-queue-samples"})
        )
        assert result["kind"] == "skill_update"
        assert result["target_slug"] == "purge-queue-samples"
        assert result["has_draft"] is True
        assert "statusMessages" in result["draft_preview"]
