"""到期 Schedule 创建对应 Task, 并更新 last_run / next_run."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, assert_never

import structlog
from croniter import croniter
from structlog.contextvars import bound_contextvars

from ..config import HotSettings
from ..db import RoutineType, TaskStatus, TaskType
from ..handlers import (
    CleanupPayload,
    MediaRefreshPayload,
    NotifyPayload,
    R18ImportPayload,
    RescrapePayload,
    SemanticIndexPayload,
    Transfer115Payload,
    UpscalePayload,
    WashApplyPayload,
    WashScanPayload,
)
from ..library.wash.apply import expired_record_ids

if TYPE_CHECKING:
    from ..db.repository import Repository

logger = structlog.get_logger()

_CHECK_INTERVAL = 60.0


class CronScheduler:
    def __init__(self, repo: Repository, get_hot: Callable[[], HotSettings] | None = None):
        self._repo = repo
        self._get_hot = get_hot
        self._running = False
        self._stop_signal = asyncio.Event()
        # 自动中转的下次到期时刻; None 表示尚未开始计时或当前关闭
        self._transfer_due_at: datetime | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self._running = True
        self._stop_signal.clear()
        logger.info("cron scheduler started", interval=_CHECK_INTERVAL)

        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("cron scheduler tick error")
            # 可被 stop() 立刻唤醒, 不必等满检查间隔.
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop_signal.wait(), _CHECK_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        self._stop_signal.set()
        logger.info("cron scheduler stopping")

    async def _tick(self) -> None:
        now = datetime.now(UTC)
        await self._maybe_transfer(now)
        schedules = await self._repo.list_schedules()

        for schedule in schedules:
            if not schedule.enabled:
                continue
            if schedule.id is None:
                continue

            if schedule.next_run is None:
                # 尚无 next_run: 只写入下次触发时间
                next_run = croniter(schedule.cron, now).get_next(datetime)
                await self._repo.update_schedule(schedule.id, next_run=next_run)
            else:
                # 无时区则按 UTC
                next_run = schedule.next_run if schedule.next_run.tzinfo else schedule.next_run.replace(tzinfo=UTC)

            if next_run > now:
                continue

            with bound_contextvars(schedule_id=schedule.id, schedule_name=schedule.name, task_type=schedule.task_type):
                logger.info("schedule triggered", payload=schedule.payload)
                await self._execute_task(schedule.id, schedule.task_type, schedule.payload)

            new_next_run = croniter(schedule.cron, now).get_next(datetime)
            await self._repo.update_schedule(schedule.id, last_run=now, next_run=new_next_run)

    async def _maybe_transfer(self, now: datetime) -> None:
        """按 ``transfer.interval_minutes`` 到点入队一次中转.

        中转没有例行类型: 间隔写在中转配置里, 因此关掉开关或把间隔设为 0 就立刻停, 不必再去
        定时任务页删条目. 上一轮还在排队或执行时不重复入队 —— 中转是幂等扫描, 抢跑只会浪费配额.
        """
        if self._get_hot is None:
            return
        hot = self._get_hot()
        interval = hot.transfer.interval_minutes
        if interval <= 0 or not hot.transfer.enabled:
            self._transfer_due_at = None
            return

        if self._transfer_due_at is None:
            self._transfer_due_at = now + timedelta(minutes=interval)
            return
        if now < self._transfer_due_at:
            return
        self._transfer_due_at = now + timedelta(minutes=interval)

        busy = await self._repo.count_tasks(
            statuses=[TaskStatus.QUEUED, TaskStatus.RUNNING],
            task_types=[TaskType.TRANSFER_115],
        )
        if busy:
            logger.info("transfer skipped", reason="上一轮尚未结束")
            return
        await self._repo.create_task(TaskType.TRANSFER_115, Transfer115Payload())
        logger.info("transfer enqueued", interval_minutes=interval)

    async def _execute_task(self, schedule_id: int, task_type: RoutineType, payload: dict) -> None:
        match task_type:
            case RoutineType.CLEANUP:
                await self._repo.create_task(
                    TaskType.CLEANUP,
                    CleanupPayload(
                        remove_missing_files=payload.get("remove_missing_files", True),
                        remove_unreferenced_resources=payload.get("remove_unreferenced_resources", True),
                    ),
                )
            case RoutineType.UPSCALE:
                await self._repo.create_task(
                    TaskType.UPSCALE,
                    UpscalePayload(
                        max_dim_threshold=payload.get("max_dim_threshold"),
                        max_bytes_threshold=payload.get("max_bytes_threshold"),
                        limit=payload.get("limit", 200),
                    ),
                )
            case RoutineType.R18_IMPORT:
                await self._repo.create_task(TaskType.R18_IMPORT, R18ImportPayload(force=payload.get("force", False)))
            case RoutineType.RESCRAPE:
                await self._repo.create_task(TaskType.RESCRAPE, RescrapePayload.model_validate(payload))
            case RoutineType.SEMANTIC_INDEX:
                await self._repo.create_task(TaskType.SEMANTIC_INDEX, SemanticIndexPayload.model_validate(payload))
            case RoutineType.NOTIFY_DIGEST:
                await self._repo.create_task(TaskType.NOTIFY, NotifyPayload(kind="digest"))
            case RoutineType.MEDIA_REFRESH:
                await self._repo.create_task(TaskType.MEDIA_REFRESH, MediaRefreshPayload())
            case RoutineType.WASH_SCAN:
                # 只入队只读扫描: 洗版是破坏性动作, 定时任务只报告候选, 由人决定何时执行
                await self._repo.create_task(TaskType.WASH_SCAN, WashScanPayload.model_validate(payload))
            case RoutineType.WASH_PURGE:
                # 只清到期记录: 不重算候选, 免得定时任务顺手洗掉用户还想留的文件
                ids = await expired_record_ids(self._repo)
                if not ids:
                    logger.info("wash purge skipped", reason="无到期记录")
                    return
                await self._repo.create_task(TaskType.WASH_APPLY, WashApplyPayload(purge_ids=ids))
            case _:
                assert_never(task_type)
