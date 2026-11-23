"""定时任务: 到期的例行动作各自提交对应任务, 载荷原样转换."""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from amane.config import HotSettings, TransferConfig
from amane.db import RoutineType, TaskType
from amane.db.models import WashRecordKind
from amane.handlers import (
    SemanticIndexPayload,
    Transfer115Payload,
    WashApplyPayload,
    WashScanPayload,
)
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


def _transfer_hot(*, interval: int = 10, enabled: bool = True) -> HotSettings:
    transfer = TransferConfig(
        enabled=enabled,
        cookies="UID=1; CID=2",
        source_cid="111",
        target_cid="222",
        interval_minutes=interval,
    )
    return HotSettings(transfer=transfer)


@pytest.mark.asyncio
async def test_transfer_interval_enqueues_after_it_elapses(repo: Repository) -> None:
    """间隔写在中转配置里: 第一次 tick 只开始计时, 到期后才入队."""
    hot = _transfer_hot(interval=10)
    scheduler = CronScheduler(repo, get_hot=lambda: hot)

    await scheduler._tick()
    assert await repo.list_tasks(task_types=[TaskType.TRANSFER_115]) == []

    scheduler._transfer_due_at = datetime.now(UTC) - timedelta(seconds=1)
    await scheduler._tick()

    tasks = await repo.list_tasks(task_types=[TaskType.TRANSFER_115])
    assert len(tasks) == 1
    assert Transfer115Payload.model_validate(tasks[0].payload).full is False
    assert scheduler._transfer_due_at is not None and scheduler._transfer_due_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_transfer_interval_zero_keeps_it_manual(repo: Repository) -> None:
    hot = _transfer_hot(interval=0)
    scheduler = CronScheduler(repo, get_hot=lambda: hot)
    scheduler._transfer_due_at = datetime.now(UTC) - timedelta(seconds=1)

    await scheduler._tick()

    assert await repo.list_tasks(task_types=[TaskType.TRANSFER_115]) == []
    assert scheduler._transfer_due_at is None


@pytest.mark.asyncio
async def test_transfer_interval_skips_while_previous_round_runs(repo: Repository) -> None:
    hot = _transfer_hot(interval=5)
    scheduler = CronScheduler(repo, get_hot=lambda: hot)
    await repo.create_task(TaskType.TRANSFER_115, Transfer115Payload())

    scheduler._transfer_due_at = datetime.now(UTC) - timedelta(seconds=1)
    await scheduler._tick()

    assert len(await repo.list_tasks(task_types=[TaskType.TRANSFER_115])) == 1


@pytest.mark.asyncio
async def test_due_wash_scan_schedule_enqueues_read_only_scan(repo: Repository) -> None:
    """定时扫描只读候选: 到点入队的是 wash_scan, 不是 wash_apply."""
    await repo.create_schedule(
        cron="0 2 * * *",
        task_type=RoutineType.WASH_SCAN,
        payload={"type": "wash_scan", "kind": "strm"},
        next_run=datetime.now(UTC) - timedelta(minutes=1),
    )

    await CronScheduler(repo)._tick()

    assert await repo.list_tasks(task_types=[TaskType.WASH_APPLY]) == [], "定时扫描不得顺手执行洗版"
    tasks = await repo.list_tasks(task_types=[TaskType.WASH_SCAN])
    assert len(tasks) == 1
    payload = WashScanPayload.model_validate(tasks[0].payload)
    assert (payload.kind, payload.library_id) == ("strm", None)


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


@pytest.mark.asyncio
async def test_due_media_refresh_schedule_enqueues_refresh_task(repo: Repository) -> None:
    await repo.create_schedule(
        cron="0 5 * * *",
        task_type=RoutineType.MEDIA_REFRESH,
        payload={"type": "media_refresh"},
        next_run=datetime.now(UTC) - timedelta(minutes=1),
    )

    await CronScheduler(repo)._tick()

    tasks = await repo.list_tasks(task_types=[TaskType.MEDIA_REFRESH])
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_due_notify_digest_schedule_enqueues_digest_task(repo: Repository) -> None:
    """周报例行只入队 digest 任务, 摘要由任务现算."""
    await repo.create_schedule(
        cron="0 9 * * 1",
        task_type=RoutineType.NOTIFY_DIGEST,
        payload={"type": "notify_digest"},
        next_run=datetime.now(UTC) - timedelta(minutes=1),
    )

    await CronScheduler(repo)._tick()

    tasks = await repo.list_tasks(task_types=[TaskType.NOTIFY])
    assert len(tasks) == 1
    assert tasks[0].payload["kind"] == "digest"
