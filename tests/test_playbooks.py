"""Playbook memory, approval, tool guard, and decision tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mose.config import MemoryConfig, SignalConfig
from mose.memory import MemoryManager
from mose.playbook_decision import (
    PLAYBOOK_DELETION_KIND,
    PLAYBOOK_PROPOSAL_KIND,
    PLAYBOOK_RUN_PROPOSAL_KIND,
    PLAYBOOK_UPDATE_KIND,
    format_playbook_run_proposal_message,
    handle_playbook_decision,
    init_playbook_decision_runtime,
)
from mose.tools import (
    call_native_tool,
    enter_scheduled_execution,
    execute_mcp_tool,
    exit_scheduled_execution,
    init_approval,
    init_playbook_tool_context,
    init_workspace,
    scheduled_execution_bypasses_approval,
)


_DELETE_SEARCH_PLAN = {
    "procedure": "Delete episode files then episode search.",
    "allowed_tools": [
        "mcp-portal__portal_codemode_execute",
        "sonarr-diagnostics__sonarr_delete_episodefile",
        "sonarr-diagnostics__sonarr_post_command_episode_search",
    ],
}


@pytest.fixture()
def memory() -> MemoryManager:
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "t.db"
        cfg = MemoryConfig(db_path=str(db))
        mm = MemoryManager(cfg)
        yield mm
        mm.close()


@pytest.fixture()
def mock_config() -> MagicMock:
    cfg = MagicMock()
    cfg.signal = SignalConfig(
        admin_group_id="admin-gid",
        engagement_group_id="eng-gid",
        proposal_timeout_seconds=3600,
    )
    return cfg


def test_playbook_crud(memory: MemoryManager) -> None:
    pid = memory.create_playbook(
        slug="delete-search",
        description="Sonarr delete-then-search",
        execution_plan=_DELETE_SEARCH_PLAN,
        user_prompt_template="Run for {series} S{season}",
    )
    assert pid > 0
    row = memory.get_playbook("delete-search")
    assert row is not None
    assert row.execution_plan["allowed_tools"]
    playbooks = memory.list_playbooks()
    assert len(playbooks) == 1
    memory.update_playbook("delete-search", description="Updated desc")
    assert memory.get_playbook("delete-search").description == "Updated desc"
    memory.delete_playbook("delete-search")
    assert memory.get_playbook("delete-search") is None


@pytest.mark.asyncio
async def test_playbook_proposal_creates_row(memory: MemoryManager) -> None:
    payload = {
        "playbook_slug": "delete-search",
        "description": "Sonarr replace",
        "execution_plan": _DELETE_SEARCH_PLAN,
    }
    memory.save_pending_approval(
        slug="delete-search",
        kind=PLAYBOOK_PROPOSAL_KIND,
        recipient="cli",
        proposal_path="",
        payload=payload,
        expires_at=9_999_999_999.0,
    )
    init_playbook_decision_runtime(memory=memory, get_agent=lambda: None)
    ok = await handle_playbook_decision("delete-search", approved=True)
    assert ok
    assert memory.get_playbook("delete-search") is not None


@pytest.mark.asyncio
async def test_playbook_update_approval(memory: MemoryManager) -> None:
    memory.create_playbook(
        slug="delete-search",
        description="Old",
        execution_plan=_DELETE_SEARCH_PLAN,
    )
    new_plan = {
        "procedure": "Updated procedure",
        "allowed_tools": _DELETE_SEARCH_PLAN["allowed_tools"]
        + ["sonarr-diagnostics__sonarr_get_queue"],
    }
    memory.save_pending_approval(
        slug="playbook-upd-delete-search",
        kind=PLAYBOOK_UPDATE_KIND,
        recipient="cli",
        proposal_path="",
        payload={
            "target_slug": "delete-search",
            "updates": {"description": "New desc", "execution_plan": new_plan},
        },
        expires_at=9_999_999_999.0,
    )
    init_playbook_decision_runtime(memory=memory, get_agent=lambda: None)
    ok = await handle_playbook_decision("playbook-upd-delete-search", approved=True)
    assert ok
    pb = memory.get_playbook("delete-search")
    assert pb.description == "New desc"
    assert "sonarr_get_queue" in pb.execution_plan["allowed_tools"][-1]


@pytest.mark.asyncio
async def test_playbook_deletion_approval(memory: MemoryManager) -> None:
    memory.create_playbook(
        slug="delete-search",
        description="x",
        execution_plan=_DELETE_SEARCH_PLAN,
    )
    memory.save_pending_approval(
        slug="playbook-del-delete-search",
        kind=PLAYBOOK_DELETION_KIND,
        recipient="cli",
        proposal_path="",
        payload={"target_slug": "delete-search"},
        expires_at=9_999_999_999.0,
    )
    init_playbook_decision_runtime(memory=memory, get_agent=lambda: None)
    ok = await handle_playbook_decision("playbook-del-delete-search", approved=True)
    assert ok
    assert memory.get_playbook("delete-search") is None


def test_format_playbook_run_proposal_includes_preflight() -> None:
    body = format_playbook_run_proposal_message(
        {
            "run_slug": "run-delete-search-abcd",
            "playbook_slug": "delete-search",
            "description": "Sonarr replace",
            "invocation_params": {"series": "Landman", "season": 1, "episodes": [4, 5]},
            "preflight_summary": "seriesId=123 fileIds=[1,2] audio=French",
            "user_prompt": "Delete and search",
            "execution_plan": _DELETE_SEARCH_PLAN,
        }
    )
    assert "Preflight findings" in body
    assert "seriesId=123" in body
    assert "Landman" in body
    assert "sonarr_delete_episodefile" in body


@pytest.mark.asyncio
async def test_playbook_run_propose_tool(memory: MemoryManager, mock_config: MagicMock) -> None:
    memory.create_playbook(
        slug="delete-search",
        description="Sonarr replace",
        execution_plan=_DELETE_SEARCH_PLAN,
    )
    init_playbook_tool_context(memory=memory, config=mock_config)
    result = await call_native_tool(
        "playbook_run_propose",
        {
            "playbook_slug": "delete-search",
            "invocation_params": {"series": "Landman", "season": 1, "episodes": [4, 5]},
            "preflight_summary": "seriesId=123",
            "user_prompt": "Delete E04 E05 and search",
        },
        session_id="signal-grp-eng-test",
    )
    assert "Playbook run proposal" in result
    pending = memory.list_pending_approvals(kind=PLAYBOOK_RUN_PROPOSAL_KIND, status="pending")
    assert len(pending) == 1
    assert pending[0].payload.get("reply_session_id") == "signal-grp-eng-test"


@pytest.mark.asyncio
async def test_playbook_run_reject_no_execution(memory: MemoryManager) -> None:
    memory.create_playbook(
        slug="delete-search",
        description="x",
        execution_plan=_DELETE_SEARCH_PLAN,
    )
    run_slug = "run-delete-search-test"
    memory.save_pending_approval(
        slug=run_slug,
        kind=PLAYBOOK_RUN_PROPOSAL_KIND,
        recipient="cli",
        proposal_path="",
        payload={
            "playbook_slug": "delete-search",
            "run_slug": run_slug,
            "user_prompt": "Do it",
            "execution_plan": _DELETE_SEARCH_PLAN,
            "preflight_summary": "ok",
            "invocation_params": {},
        },
        expires_at=9_999_999_999.0,
    )
    agent = MagicMock()
    agent.run_playbook_run = AsyncMock(return_value={"status": "ok", "summary": "x", "tool_trace": []})
    init_playbook_decision_runtime(memory=memory, get_agent=lambda: agent)
    ok = await handle_playbook_decision(run_slug, approved=False)
    assert ok
    agent.run_playbook_run.assert_not_called()
    assert memory.get_playbook_run(run_slug) is None


@pytest.mark.asyncio
async def test_playbook_run_approve_executes(memory: MemoryManager) -> None:
    memory.create_playbook(
        slug="delete-search",
        description="x",
        execution_plan=_DELETE_SEARCH_PLAN,
    )
    run_slug = "run-delete-search-exec"
    payload = {
        "playbook_slug": "delete-search",
        "run_slug": run_slug,
        "user_prompt": "Do it",
        "execution_plan": _DELETE_SEARCH_PLAN,
        "preflight_summary": "seriesId=1",
        "invocation_params": {"series": "Landman"},
    }
    memory.save_pending_approval(
        slug=run_slug,
        kind=PLAYBOOK_RUN_PROPOSAL_KIND,
        recipient="cli",
        proposal_path="",
        payload=payload,
        expires_at=9_999_999_999.0,
    )
    agent = MagicMock()
    agent.run_playbook_run = AsyncMock(
        return_value={"status": "ok", "summary": "Queue updated", "tool_trace": []}
    )
    init_playbook_decision_runtime(memory=memory, get_agent=lambda: agent)
    from mose import playbook_decision as pd

    pd._runtime["await_playbook_runs"] = True
    try:
        ok = await handle_playbook_decision(run_slug, approved=True)
    finally:
        pd._runtime.pop("await_playbook_runs", None)
    assert ok
    agent.run_playbook_run.assert_called_once()
    run_row = memory.get_playbook_run(run_slug)
    assert run_row is not None
    assert run_row.status == "ok"
    assert "Queue" in (run_row.summary or "")


@pytest.mark.asyncio
async def test_playbook_scheduled_tool_guard() -> None:
    with tempfile.TemporaryDirectory() as d:
        init_workspace(d, allow_read_outside=True)
        token = enter_scheduled_execution(
            "run-delete-search",
            frozenset(["sonarr-diagnostics__sonarr_delete_episodefile"]),
        )
        try:
            blocked = await call_native_tool("bash", {"command": "echo hi"})
            assert blocked.startswith("Blocked:")
            assert scheduled_execution_bypasses_approval(
                "sonarr-diagnostics__sonarr_delete_episodefile"
            )
            assert not scheduled_execution_bypasses_approval("bash")
        finally:
            exit_scheduled_execution(token)


@pytest.mark.asyncio
async def test_mutating_mcp_bypasses_approval_in_playbook_run() -> None:
    """Mutating MCP in allowlist skips per-action approval callback."""
    approval = AsyncMock(return_value=False)
    init_approval(approval)

    mock_mcp = MagicMock()
    mock_mcp.call_tool = AsyncMock(return_value=("ok", False))

    import mose.tools as tools_mod

    old = tools_mod._mcp_manager
    tools_mod._mcp_manager = mock_mcp
    token = enter_scheduled_execution(
        "run-test",
        frozenset(["sonarr-diagnostics__sonarr_delete_episodefile"]),
    )
    try:
        text, err = await execute_mcp_tool(
            "sonarr-diagnostics__sonarr_delete_episodefile",
            {"id": 1},
        )
        assert text == "ok"
        assert err is False
        approval.assert_not_called()
    finally:
        exit_scheduled_execution(token)
        tools_mod._mcp_manager = old
        init_approval(None)
