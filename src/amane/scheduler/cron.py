"""到期 Schedule 创建对应 Task, 并更新 last_run / next_run."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, assert_never

import structlog
from croniter import croniter
from pydantic import BaseModel
from structlog.contextvars import bound_contextvars

from ..config import HotSettings
from ..db import RoutineType, TaskType
from ..handlers import (
    CleanupPayload,
    MediaRefreshPayload,
    NotifyPayload,
    R18ImportPayload,
    RescrapePayload,
    SemanticIndexPayload,
    SubtitleAuditPayload,
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
                try:
                    await self._execute_task(schedule.id, schedule.task_type, schedule.payload)
                except Exception:
                    # 一条坏例行不能挡住它后面的全部: 载荷经 schema 漂移后必然校验失败, 上浮会让整 tick
                    # 中止, 这条自己每 60 秒重犯一次, 而 list_schedules 无排序, 其后的例行永远轮不到.
                    logger.exception("schedule execute failed", schedule_id=schedule.id)

            new_next_run = croniter(schedule.cron, now).get_next(datetime)
            await self._repo.update_schedule(schedule.id, last_run=now, next_run=new_next_run)

    async def _enqueue(self, task_type: TaskType, payload: BaseModel) -> None:
        """入队一条例行任务; 队列里已有同类型同载荷时就跳过.

        整库型例行 (索引 / 清理 / 洗版扫描) 单次耗时常常超过 cron 间隔: 不判重就会稳定堆积同款任务,
        认领后逐条重复劳动, 还要抢同一把库锁.
        """
        created = await self._repo.create_followups_unless_queued([(task_type, payload, 0)])
        if not created:
            logger.info("routine task already queued", task_type=task_type.value)

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
                await self._enqueue(TaskType.R18_IMPORT, R18ImportPayload(force=payload.get("force", False)))
            case RoutineType.RESCRAPE:
                await self._enqueue(TaskType.RESCRAPE, RescrapePayload.model_validate(payload))
            case RoutineType.SEMANTIC_INDEX:
                await self._enqueue(TaskType.SEMANTIC_INDEX, SemanticIndexPayload.model_validate(payload))
            case RoutineType.NOTIFY_DIGEST:
                await self._enqueue(TaskType.NOTIFY, NotifyPayload(kind="digest"))
            case RoutineType.SUBTITLE_AUDIT:
                # 巡检只统计缺口; 指定库时巡检自己会排一轮补全, 这里不再重复排队
                await self._repo.create_task(
                    TaskType.SUBTITLE_AUDIT,
                    SubtitleAuditPayload(library_id=payload.get("library_id")),
                )
            case RoutineType.MEDIA_REFRESH:
                await self._enqueue(TaskType.MEDIA_REFRESH, MediaRefreshPayload())
            case RoutineType.WASH_SCAN:
                # 只入队只读扫描: 洗版是破坏性动作, 定时任务只报告候选, 由人决定何时执行
                await self._enqueue(TaskType.WASH_SCAN, WashScanPayload.model_validate(payload))
            case RoutineType.WASH_PURGE:
                # 只清到期记录: 不重算候选, 免得定时任务顺手洗掉用户还想留的文件
                ids = await expired_record_ids(self._repo)
                if not ids:
                    logger.info("wash purge skipped", reason="无到期记录")
                    return
                await self._enqueue(TaskType.WASH_APPLY, WashApplyPayload(purge_ids=ids))
            case _:
                assert_never(task_type)
