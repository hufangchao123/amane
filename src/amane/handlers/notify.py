"""NOTIFY: 把一段消息或一份周报推到配置的渠道.

推送失败不回滚任何东西, 因此这里把失败折进结果并把 `error` 写清; 每次推送都留一条记录,
退役记录按 `NotifyConfig.max_records` 修剪. 周报只读数据库计数, 不扫盘, 因此可以在例行里跑.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..db.models import TaskStatus, TransferItemState, WashRecordState
from ..notify import Notifier
from .models import NotifyPayload, NotifyResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config.manager import HotSettings, NotifyConfig
    from ..db.repository import Repository

DIGEST_DAYS = 7
DIGEST_FAILED_SAMPLES = 5
BODY_LIMIT = 2000


class NotifyHandler(TaskHandler[NotifyPayload, NotifyResult]):
    """``kind=message`` 原样转发, ``kind=digest`` 现算一份统计摘要."""

    def __init__(
        self,
        repo: Repository,
        hot: HotSettings,
        *,
        notifier_factory: Callable[[NotifyConfig], Notifier] = Notifier,
    ) -> None:
        super().__init__(payload_t=NotifyPayload, result_t=NotifyResult)
        self._repo = repo
        self._hot = hot
        self._notifier_factory = notifier_factory

    async def handle(self, payload: NotifyPayload) -> TaskResult[NotifyResult]:
        if payload.kind == "digest":
            title = payload.title or "Amane 周报"
            body = payload.body or await self._digest_body()
        else:
            title = payload.title or "Amane 通知"
            body = payload.body
        outcome = await self._notifier_factory(self._hot.notify).send(title, body)
        await self._repo.add_notify_record(
            kind=payload.kind,
            channel=outcome.channel,
            title=title,
            body=body[:BODY_LIMIT],
            ok=outcome.ok,
            error=outcome.error,
        )
        await self._repo.prune_notify_records(self._hot.notify.max_records)
        result = NotifyResult(
            kind=payload.kind,
            channel=outcome.channel,
            ok=outcome.ok,
            status=outcome.status,
            digest_days=DIGEST_DAYS if payload.kind == "digest" else 0,
        )
        return TaskResult(success=outcome.ok, result=result, error=outcome.error)

    async def _digest_body(self) -> str:
        """任务计数与最近失败; 只查库, 不碰磁盘."""
        queued = await self._repo.count_tasks(statuses=[TaskStatus.QUEUED])
        running = await self._repo.count_tasks(statuses=[TaskStatus.RUNNING])
        done = await self._repo.count_tasks(statuses=[TaskStatus.DONE])
        failed = await self._repo.count_tasks(statuses=[TaskStatus.FAILED])
        wash_count, wash_bytes = await self._repo.count_wash_records(state=WashRecordState.TRASHED)
        transfer = await self._repo.transfer_state_counts()
        pending = transfer.get(TransferItemState.PENDING, 0)
        lines = [
            f"最近 {DIGEST_DAYS} 天的队列: 排队 {queued}, 执行中 {running}, 完成 {done}, 失败 {failed}",
            f"洗版回收站: {wash_count} 份可还原, 占用 {wash_bytes / 1024 / 1024:.0f} MiB",
            f"115 中转: {pending} 个文件等待成熟",
        ]
        recent = await self._repo.list_tasks(statuses=[TaskStatus.FAILED], limit=DIGEST_FAILED_SAMPLES)
        for task in recent:
            detail = (task.error or "").splitlines()[0][:120] if task.error else "无错误文案"
            lines.append(f"失败 #{task.id} {task.type}: {detail}")
        return "\n".join(lines)
