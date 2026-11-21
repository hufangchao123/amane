"""115 中转任务: 每轮把网盘源目录里成熟的媒体搬进目标目录.

搬哪些由这里决定 (广告过滤 / 既往结果复用 / 成熟等待 / 单轮上限), 怎么搬由
``transfer/client.py`` 决定 (间隔与退避). 一次任务会连着几十次 HTTP 调用, 因此同步 I/O
一律丢线程: 事件循环的停顿预算是 5 ms.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

from ..db.models import TransferItemState
from ..transfer import (
    CloudEntry,
    Transfer115Client,
    TransferClientLike,
    TransferError,
    TransferNotConfigured,
    TransferService,
    is_ad_file,
    is_media_name,
    pick_folder_ads,
)
from .models import Transfer115Payload, Transfer115Result
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config.manager import HotSettings, TransferConfig
    from ..db.models import TransferItem
    from ..db.repository import Repository

logger = structlog.get_logger()

MAX_MOVES_PER_RUN = 5
"""单轮搬移上限: 一轮搬空会让任务长时间不可收尾, 也会把网盘限流一次撞满."""

MIN_AGE_SECONDS = 120
"""首次见到后等这么久才搬.

115 的离线下载先建文件再灌数据, 半截搬走会把残缺文件带进 STRM 目录; 面板同样等 120 秒.
"""

MAX_ATTEMPTS = 3
"""单文件搬移重试上限; 达上限后停在 ``failed``, 不再每轮重试."""

AD_SKIPPED = "广告文件, 按 ad_action=skip 留在源目录"
AD_TRASHED = "广告文件, 已进 115 回收站"
FOLDER_BLOCKED = "文件夹含广告文件, 按 ad_action=skip 整项不搬"
"""跳过原因落在 ``transfer_items.error``: 表没有单独的原因列, 且它本该与失败原因同处一列."""


class _Outcome(StrEnum):
    """一条候选在本轮的处理结果; 三者之和恒等于扫描数."""

    MOVED = "moved"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(slots=True)
class _Run:
    """一轮搬运的可变状态."""

    config: TransferConfig
    client: TransferClientLike
    keywords: tuple[str, ...]
    budget: int = MAX_MOVES_PER_RUN


class Transfer115Handler(TaskHandler[Transfer115Payload, Transfer115Result]):
    """把网盘源目录里的新增文件搬进本地 STRM 根目录, 逐文件状态落 ``transfer_items``.

    搬移在网盘侧发生, 本地只落 STRM 清单; 网盘客户端在这里构造, 因为只有本任务用到它.
    ``client_factory`` 是测试注入点: 用例断言编排, 不该连 webapi.115.com.
    """

    def __init__(
        self,
        repo: Repository,
        hot: HotSettings,
        client_factory: Callable[[str], TransferClientLike] = Transfer115Client,
    ) -> None:
        super().__init__(payload_t=Transfer115Payload, result_t=Transfer115Result)
        self._repo = repo
        self._hot = hot
        self._client_factory = client_factory

    async def handle(self, payload: Transfer115Payload) -> TaskResult[Transfer115Result]:
        config = self._hot.transfer
        service = TransferService(config)
        if not service.enabled:
            # 未配置不是故障: 说清缺什么就收尾, 不留下一半搬移的现场.
            return TaskResult(success=False, error=service.unavailable_reason())
        try:
            client = self._client_factory(config.cookies or "")
        except TransferNotConfigured as exc:
            return TaskResult(success=False, error=str(exc))
        try:
            return await self._run(payload, config=config, client=client)
        finally:
            client.close()

    async def _run(
        self, payload: Transfer115Payload, *, config: TransferConfig, client: TransferClientLike
    ) -> TaskResult[Transfer115Result]:
        try:
            entries = await asyncio.to_thread(client.list_dir, config.source_dir)
        except TransferError as exc:
            return TaskResult(success=False, error=str(exc))

        candidates = [entry for entry in entries if entry.key and (entry.is_dir or is_media_name(entry.name))]
        if not candidates:
            logger.info("115 transfer source empty", source=config.source_dir)
            return TaskResult(True, result=Transfer115Result())

        snapshots = await self._repo.transfer_snapshots([entry.key for entry in candidates])
        # 首次见到的项只登记不搬: 成熟等待从落库这一刻起算, 下一轮才可能被搬走.
        await self._repo.track_transfer_items(
            [(entry.key, entry.name) for entry in candidates if entry.key not in snapshots]
        )

        run = _Run(config=config, client=client, keywords=tuple(config.ad_keywords))
        moved = skipped = failed = 0
        total = len(candidates)
        for scanned, entry in enumerate(candidates, start=1):
            prior = snapshots.get(entry.key)
            if self._settled(prior, full=payload.full):
                skipped += 1
            else:
                outcome = await self._apply(run, entry, prior)
                if outcome is _Outcome.MOVED:
                    moved += 1
                    run.budget -= 1
                elif outcome is _Outcome.SKIPPED:
                    skipped += 1
                else:
                    failed += 1
            await self.report_progress(scanned, total, entry.name)

        logger.info("115 transfer finished", scanned=total, moved=moved, skipped=skipped, failed=failed)
        return TaskResult(True, result=Transfer115Result(scanned=total, moved=moved, skipped=skipped, failed=failed))

    @staticmethod
    def _settled(prior: TransferItem | None, *, full: bool) -> bool:
        """既往结果能否直接复用: 已搬的、按策略跳过的、重试到上限的都不再动.

        ``full`` 忽略这三类结论, 但仍受成熟等待约束 —— 强制搬半截文件不是重扫的本意.
        """
        if prior is None or full:
            return False
        if prior.state is TransferItemState.MOVED or prior.state is TransferItemState.SKIPPED:
            return True
        return prior.state is TransferItemState.FAILED and prior.attempts >= MAX_ATTEMPTS

    async def _apply(self, run: _Run, entry: CloudEntry, prior: TransferItem | None) -> _Outcome:
        if prior is None:
            return _Outcome.SKIPPED
        if datetime.now(UTC) - prior.updated_at < timedelta(seconds=MIN_AGE_SECONDS):
            return _Outcome.SKIPPED
        if entry.is_dir:
            return await self._process_folder(run, entry)
        if is_ad_file(entry.name, run.keywords):
            return await self._discard_ad(run, entry)
        if run.budget <= 0:
            return _Outcome.SKIPPED
        return await self._move(run, entry.key, entry.name)

    async def _process_folder(self, run: _Run, entry: CloudEntry) -> _Outcome:
        """文件夹整项搬: 先看夹内心, 广告在夹内就地处理, 否则搬过去会把广告带进库."""
        try:
            inner = await asyncio.to_thread(run.client.list_dir, entry.key)
        except TransferError as exc:
            await self._repo.mark_transfer_failed(entry.key, entry.name, str(exc))
            return _Outcome.FAILED
        media = {
            item.fid: item.name
            for item in inner
            if item.fid is not None and not item.is_dir and is_media_name(item.name)
        }
        if not media:
            # 空夹或只有非视频: 不动它, 也不占本轮的搬移额度.
            return _Outcome.SKIPPED
        ads = pick_folder_ads(entry.name, list(media.items()), run.keywords)
        if ads and run.config.ad_action == "skip":
            await self._repo.mark_transfer_skipped(entry.key, entry.name, FOLDER_BLOCKED)
            return _Outcome.SKIPPED
        for ad_fid in ads:
            try:
                await asyncio.to_thread(run.client.trash, ad_fid)
            except TransferError as exc:
                await self._repo.mark_transfer_failed(entry.key, entry.name, str(exc))
                return _Outcome.FAILED
            await self._repo.mark_transfer_skipped(ad_fid, media[ad_fid], AD_TRASHED)
        if run.budget <= 0:
            return _Outcome.SKIPPED
        return await self._move(run, entry.key, entry.name)

    async def _discard_ad(self, run: _Run, entry: CloudEntry) -> _Outcome:
        if run.config.ad_action == "skip":
            await self._repo.mark_transfer_skipped(entry.key, entry.name, AD_SKIPPED)
            return _Outcome.SKIPPED
        try:
            await asyncio.to_thread(run.client.trash, entry.key)
        except TransferError as exc:
            await self._repo.mark_transfer_failed(entry.key, entry.name, str(exc))
            return _Outcome.FAILED
        await self._repo.mark_transfer_skipped(entry.key, entry.name, AD_TRASHED)
        return _Outcome.SKIPPED

    async def _move(self, run: _Run, key: str, name: str) -> _Outcome:
        try:
            await asyncio.to_thread(run.client.move, key, run.config.target_dir)
        except TransferError as exc:
            await self._repo.mark_transfer_failed(key, name, str(exc))
            return _Outcome.FAILED
        await self._repo.mark_transfer_moved(key, name)
        logger.info("115 transfer moved", name=name)
        return _Outcome.MOVED
