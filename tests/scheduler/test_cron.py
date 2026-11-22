"""定时任务: 到期的例行动作各自提交对应任务, 载荷原样转换."""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from amane.db import RoutineType, TaskType
from amane.db.models import WashRecordKind
from amane.handlers import BackupPayload, SemanticIndexPayload, Transfer115Payload, WashApplyPayload
from amane.scheduler.cron import CronScheduler

if TYPE_CHECKING:
    from amane.db.repository import Repository


@pytest.mark.asyncio
async def test_due_semantic_schedule_enqueues_index_task(repo: Repository) -> None:
    schedule = await repo.create_schedule(
        cron="0 3 * * *",
        task_type=RoutineType.SEMANTIC_INDEX,
        payload={"type": "semantic_index", "limit": 50, "force": True},
        next_run=datetime.now(UTC) - timedelta(minutes=1),
    )
    assert schedule.id is not None

    await CronScheduler(repo)._tick()

    tasks = await repo.list_tasks(task_types=[TaskType.SEMANTIC_INDEX])
    assert len(tasks) == 1
    payload = SemanticIndexPayload.model_validate(tasks[0].payload)
    assert (payload.limit, payload.force) == (50, True)
    updated = await repo.get_schedule(schedule.id)
    assert updated is not None and updated.last_run is not None


@pytest.mark.asyncio
async def test_future_semantic_schedule_stays_idle(repo: Repository) -> None:
    await repo.create_schedule(
        cron="0 3 * * *",
        task_type=RoutineType.SEMANTIC_INDEX,
        payload={"type": "semantic_index"},
        next_run=datetime.now(UTC) + timedelta(hours=1),
    )

    await CronScheduler(repo)._tick()

    assert await repo.list_tasks(task_types=[TaskType.SEMANTIC_INDEX]) == []


@pytest.mark.asyncio
async def test_due_transfer_schedule_enqueues_transfer_task(repo: Repository) -> None:
    await repo.create_schedule(
        cron="0 5 * * *",
        task_type=RoutineType.TRANSFER_115,
        payload={"type": "transfer_115", "full": True},
        next_run=datetime.now(UTC) - timedelta(minutes=1),
    )

    await CronScheduler(repo)._tick()

    tasks = await repo.list_tasks(task_types=[TaskType.TRANSFER_115])
    assert len(tasks) == 1
    assert Transfer115Payload.model_validate(tasks[0].payload).full is True


@pytest.mark.asyncio
async def test_due_backup_schedule_enqueues_backup_task(repo: Repository) -> None:
    await repo.create_schedule(
        cron="0 4 * * *",
        task_type=RoutineType.BACKUP,
        payload={"type": "backup", "kind": "db"},
        next_run=datetime.now(UTC) - timedelta(minutes=1),
    )

    await CronScheduler(repo)._tick()

    tasks = await repo.list_tasks(task_types=[TaskType.BACKUP])
    assert len(tasks) == 1
    assert BackupPayload.model_validate(tasks[0].payload).kind == "db"


@pytest.mark.asyncio
async def test_due_wash_purge_schedule_only_enqueues_expired_records(repo: Repository) -> None:
    """定时清理只对到期记录入队: 未到期的记录留给用户自己决定."""
    expired = await repo.add_wash_record(
        kind=WashRecordKind.LOCAL,
        group_key="g1",
        kept_path="/lib/a/keep.mp4",
        removed_path="/lib/a/drop.mp4",
        size_bytes=10,
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    await repo.add_wash_record(
        kind=WashRecordKind.LOCAL,
        group_key="g2",
        kept_path="/lib/b/keep.mp4",
        removed_path="/lib/b/drop.mp4",
        size_bytes=10,
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    await repo.create_schedule(
        cron="0 6 * * *",
        task_type=RoutineType.WASH_PURGE,
        payload={"type": "wash_purge"},
        next_run=datetime.now(UTC) - timedelta(minutes=1),
    )

    await CronScheduler(repo)._tick()

    tasks = await repo.list_tasks(task_types=[TaskType.WASH_APPLY])
    assert len(tasks) == 1
    payload = WashApplyPayload.model_validate(tasks[0].payload)
    assert (payload.purge_ids, payload.group_keys) == ([expired], [])


@pytest.mark.asyncio
async def test_wash_purge_schedule_without_expired_records_enqueues_nothing(repo: Repository) -> None:
    await repo.create_schedule(
        cron="0 6 * * *",
        task_type=RoutineType.WASH_PURGE,
        payload={"type": "wash_purge"},
        next_run=datetime.now(UTC) - timedelta(minutes=1),
    )

    await CronScheduler(repo)._tick()

    assert await repo.list_tasks(task_types=[TaskType.WASH_APPLY]) == []
