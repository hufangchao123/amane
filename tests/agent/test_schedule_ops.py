"""schedule-ops Capability 表测试."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
import pytest_asyncio
from pydantic_ai import ApprovalRequired
from pydantic_ai.toolsets import FunctionToolset

from amane.agent.cache import ResultCache
from amane.agent.executor import QueryExecutor
from amane.agent.schedule_ops import AgentScheduleUpdate, build_schedule_ops_capability
from amane.agent.sql import ReadonlySqlSandbox
from amane.agent.tools import TOOL_OK, AgentDeps
from amane.db.models import RoutineType
from amane.db.repository import Repository
from tests.agent.support import ToolCallContext, cap_toolset, tool_fn


def _toolset() -> FunctionToolset[AgentDeps]:
    return cap_toolset(build_schedule_ops_capability())


def _tool_fn(name: str) -> Callable[..., Awaitable[dict[str, Any]]]:
    return tool_fn(build_schedule_ops_capability(), name)


@pytest_asyncio.fixture
async def schedule_deps(tmp_path: Path, repo: Repository) -> AgentDeps:
    db = tmp_path / "ops.db"
    async with aiosqlite.connect(db) as conn:
        await conn.execute("CREATE TABLE metadata (id INTEGER PRIMARY KEY, title TEXT)")
        await conn.commit()
    session = await repo.create_agent_session(title="schedule-ops")
    assert session.id is not None
    return AgentDeps(
        repo=repo,
        executor=QueryExecutor(ReadonlySqlSandbox(db), ResultCache(ttl_s=60, max_entries=8)),
        session_id=session.id,
        sql_timeout_ms=2000,
    )


def test_schedule_ops_capability_contract() -> None:
    assert {
        "list_schedules",
        "get_schedule",
        "create_schedule",
        "update_schedule",
        "trigger_schedule",
        "delete_schedule",
    } <= set(_toolset().tools)


@pytest.mark.asyncio
async def test_create_update_and_trigger_schedule(schedule_deps: AgentDeps) -> None:
    created = await _tool_fn("create_schedule")(
        ToolCallContext(schedule_deps),
        name="nightly",
        cron="0 3 * * *",
        submission={"type": "cleanup", "remove_missing_files": False},
    )
    schedule_id = int(created["schedule_id"])
    stored = await schedule_deps.repo.get_schedule(schedule_id)
    assert stored is not None
    assert stored.name == "nightly"
    assert stored.task_type == RoutineType.CLEANUP
    assert stored.payload == {
        "type": "cleanup",
        "remove_missing_files": False,
        "remove_unreferenced_resources": True,
        "remove_orphan_metadata": False,
    }

    updated = await _tool_fn("update_schedule")(
        ToolCallContext(schedule_deps),
        schedule_id=schedule_id,
        patch=AgentScheduleUpdate(cron="0 4 * * *", enabled=False),
    )
    assert updated == TOOL_OK
    stored = await schedule_deps.repo.get_schedule(schedule_id)
    assert stored is not None
    assert stored.cron == "0 4 * * *"
    assert stored.enabled is False
    assert stored.next_run is not None

    triggered = await _tool_fn("trigger_schedule")(ToolCallContext(schedule_deps), schedule_id=schedule_id)
    assert triggered == TOOL_OK
    stored = await schedule_deps.repo.get_schedule(schedule_id)
    assert stored is not None and stored.next_run is not None
    assert stored.task_type == RoutineType.CLEANUP


@pytest.mark.asyncio
async def test_schedule_supports_rescrape_and_rejects_invalid_changes(schedule_deps: AgentDeps) -> None:
    created = await _tool_fn("create_schedule")(
        ToolCallContext(schedule_deps),
        cron="*/15 * * * *",
        submission={"type": "rescrape", "limit": 25, "min_age_days": 7},
    )
    schedule_id = int(created["schedule_id"])
    stored = await schedule_deps.repo.get_schedule(schedule_id)
    assert stored is not None
    assert stored.task_type == RoutineType.RESCRAPE
    assert stored.payload["limit"] == 25
    assert stored.payload["targets"] == ["metadata"]

    invalid_cron = await _tool_fn("update_schedule")(
        ToolCallContext(schedule_deps), schedule_id=schedule_id, patch=AgentScheduleUpdate(cron="not cron")
    )
    assert invalid_cron == {"error": "Invalid cron expression"}

    invalid_enabled = await _tool_fn("update_schedule")(
        ToolCallContext(schedule_deps), schedule_id=schedule_id, patch=AgentScheduleUpdate(enabled=None)
    )
    assert invalid_enabled == {"error": "enabled 不能为 null"}

    missing = await _tool_fn("get_schedule")(ToolCallContext(schedule_deps), schedule_id=9999)
    assert missing == {"error": "schedule 9999 不存在"}

    invalid_submission = await _tool_fn("create_schedule")(
        ToolCallContext(schedule_deps), cron="0 3 * * *", submission={"type": "cleanup", "remove_missing_files": "x"}
    )
    # 失败时返回出错字段, 可用类型, 以及该类型的字段定义
    assert invalid_submission["error"].startswith("参数无效: cleanup.remove_missing_files:")
    assert "rescrape" in invalid_submission["types"]
    assert invalid_submission["schema"]["properties"]["type"]["const"] == "cleanup"


@pytest.mark.asyncio
async def test_delete_schedule_requires_approval(schedule_deps: AgentDeps) -> None:
    schedule = await schedule_deps.repo.create_schedule(cron="0 0 * * *", task_type=RoutineType.CLEANUP, payload={})
    assert schedule.id is not None
    with pytest.raises(ApprovalRequired) as exc:
        await _tool_fn("delete_schedule")(
            ToolCallContext(schedule_deps, tool_call_id="tc-schedule"), schedule_id=schedule.id
        )
    assert (exc.value.metadata or {})["extra"]["schedule_id"] == schedule.id

    deleted = await _tool_fn("delete_schedule")(
        ToolCallContext(schedule_deps, tool_call_id="tc-schedule", tool_call_approved=True), schedule_id=schedule.id
    )
    assert deleted == TOOL_OK
    assert await schedule_deps.repo.get_schedule(schedule.id) is None


@pytest.mark.asyncio
async def test_create_schedule_wash_purge_requires_approval(schedule_deps: AgentDeps) -> None:
    """回收站真删的例行任务不能由助理直接立: 到点就会删除文件, 与提交一次洗版同等代价."""
    with pytest.raises(ApprovalRequired) as exc:
        await _tool_fn("create_schedule")(
            ToolCallContext(schedule_deps, tool_call_id="tc-purge"),
            cron="0 4 * * *",
            submission={"type": "wash_purge"},
        )
    meta = exc.value.metadata or {}
    assert (meta["tool"], meta["reason"]) == ("create_schedule", "destructive")
    assert meta["extra"] == {"type": "wash_purge", "cron": "0 4 * * *"}
    assert await schedule_deps.repo.list_schedules() == []

    created = await _tool_fn("create_schedule")(
        ToolCallContext(schedule_deps, tool_call_id="tc-purge", tool_call_approved=True),
        cron="0 4 * * *",
        submission={"type": "wash_purge"},
    )
    stored = await schedule_deps.repo.get_schedule(int(created["schedule_id"]))
    assert stored is not None
    assert stored.task_type == RoutineType.WASH_PURGE


@pytest.mark.asyncio
@pytest.mark.parametrize(("armed", "needs_approval"), [(None, True), (True, True), (False, False)])
async def test_create_schedule_wash_scan_follows_cascade(
    schedule_deps: AgentDeps, armed: bool | None, needs_approval: bool
) -> None:
    """洗版扫描的例行任务在自动执行打开时到点会真删; 关闭时它只是报表."""
    deps = replace(schedule_deps, wash_auto_execute=armed)
    if needs_approval:
        with pytest.raises(ApprovalRequired) as exc:
            await _tool_fn("create_schedule")(
                ToolCallContext(deps, tool_call_id="tc-scan"), cron="0 5 * * *", submission={"type": "wash_scan"}
            )
        meta = exc.value.metadata or {}
        assert (meta["tool"], meta["reason"]) == ("create_schedule", "destructive")
        assert "级联" in meta["sql"]

    created = await _tool_fn("create_schedule")(
        ToolCallContext(deps, tool_call_id="tc-scan", tool_call_approved=True),
        cron="0 5 * * *",
        submission={"type": "wash_scan"},
    )
    stored = await deps.repo.get_schedule(int(created["schedule_id"]))
    assert stored is not None
    assert stored.task_type == RoutineType.WASH_SCAN


@pytest.mark.asyncio
@pytest.mark.parametrize("routine", [RoutineType.WASH_PURGE, RoutineType.WASH_SCAN])
async def test_trigger_schedule_destructive_routine_requires_approval(
    schedule_deps: AgentDeps, routine: RoutineType
) -> None:
    """立即触发销毁类例行任务与提交一次删除同等代价, 不能借定时任务的壳绕开提交面审批."""
    schedule = await schedule_deps.repo.create_schedule(
        cron="0 4 * * *", task_type=routine, payload={"type": routine.value}
    )
    assert schedule.id is not None
    with pytest.raises(ApprovalRequired) as exc:
        await _tool_fn("trigger_schedule")(
            ToolCallContext(schedule_deps, tool_call_id="tc-trigger"), schedule_id=schedule.id
        )
    meta = exc.value.metadata or {}
    assert (meta["tool"], meta["reason"]) == ("trigger_schedule", "destructive")
    assert meta["extra"] == {"schedule_id": schedule.id, "type": routine.value}

    triggered = await _tool_fn("trigger_schedule")(
        ToolCallContext(schedule_deps, tool_call_id="tc-trigger", tool_call_approved=True), schedule_id=schedule.id
    )
    assert triggered == TOOL_OK


@pytest.mark.asyncio
async def test_trigger_schedule_wash_scan_without_cascade_runs_without_approval(schedule_deps: AgentDeps) -> None:
    deps = replace(schedule_deps, wash_auto_execute=False)
    schedule = await schedule_deps.repo.create_schedule(
        cron="0 5 * * *", task_type=RoutineType.WASH_SCAN, payload={"type": "wash_scan"}
    )
    assert schedule.id is not None
    triggered = await _tool_fn("trigger_schedule")(ToolCallContext(deps), schedule_id=schedule.id)
    assert triggered == TOOL_OK


@pytest.mark.asyncio
async def test_update_schedule_enabling_wash_purge_requires_approval(schedule_deps: AgentDeps) -> None:
    """把暂停中的销毁类例行任务重新启用等于重新武装; 改名与改表达式不新增删除能力."""
    schedule = await schedule_deps.repo.create_schedule(
        cron="0 4 * * *", task_type=RoutineType.WASH_PURGE, payload={"type": "wash_purge"}, enabled=False
    )
    assert schedule.id is not None

    renamed = await _tool_fn("update_schedule")(
        ToolCallContext(schedule_deps), schedule_id=schedule.id, patch=AgentScheduleUpdate(name="purge")
    )
    assert renamed == TOOL_OK

    with pytest.raises(ApprovalRequired) as exc:
        await _tool_fn("update_schedule")(
            ToolCallContext(schedule_deps, tool_call_id="tc-enable"),
            schedule_id=schedule.id,
            patch=AgentScheduleUpdate(enabled=True),
        )
    meta = exc.value.metadata or {}
    assert (meta["tool"], meta["reason"]) == ("update_schedule", "destructive")
    assert meta["extra"] == {"schedule_id": schedule.id, "type": "wash_purge"}

    enabled = await _tool_fn("update_schedule")(
        ToolCallContext(schedule_deps, tool_call_id="tc-enable", tool_call_approved=True),
        schedule_id=schedule.id,
        patch=AgentScheduleUpdate(enabled=True),
    )
    assert enabled == TOOL_OK
    stored = await schedule_deps.repo.get_schedule(schedule.id)
    assert stored is not None and stored.enabled is True


@pytest.mark.asyncio
async def test_update_schedule_enabling_cleanup_needs_no_approval(schedule_deps: AgentDeps) -> None:
    """不删文件的例行类型照旧免审批启用."""
    schedule = await schedule_deps.repo.create_schedule(
        cron="0 3 * * *", task_type=RoutineType.CLEANUP, payload={"type": "cleanup"}, enabled=False
    )
    assert schedule.id is not None
    enabled = await _tool_fn("update_schedule")(
        ToolCallContext(schedule_deps), schedule_id=schedule.id, patch=AgentScheduleUpdate(enabled=True)
    )
    assert enabled == TOOL_OK
