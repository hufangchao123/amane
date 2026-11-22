"""``notify_records`` 的读写面.

每条记录对应一次推送: 成功落 ``ok``, 失败带 ``error``; 正文只留摘要 (任务失败的文案可能很长).
退役记录由写入方按 ``NotifyConfig.max_records`` 修剪, 因此计数始终有界.
"""

from sqlmodel import col, delete, select

from ..models import NotifyRecord
from .base import RepositoryMixinBase


class NotifyRepoMixin(RepositoryMixinBase):
    """``notify_records`` 的读写面."""

    async def add_notify_record(
        self, *, kind: str, channel: str, title: str, body: str, ok: bool, error: str | None = None
    ) -> int:
        """登记一次推送; 返回记录 id."""
        record = NotifyRecord(kind=kind, channel=channel, title=title, body=body, ok=ok, error=error)
        async with self._session() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
        return int(record.id or 0)

    async def list_notify_records(self, limit: int = 20) -> list[NotifyRecord]:
        """按时间倒序取最近若干条; 空库返回空表."""
        async with self._session() as session:
            stmt = select(NotifyRecord).order_by(col(NotifyRecord.created_at).desc(), col(NotifyRecord.id).desc())
            return list((await session.exec(stmt.limit(limit))).all())

    async def prune_notify_records(self, keep: int) -> int:
        """只留最近 ``keep`` 条, 返回删除条数."""
        async with self._session() as session:
            ids = (
                await session.exec(
                    select(NotifyRecord.id)
                    .order_by(col(NotifyRecord.created_at).desc(), col(NotifyRecord.id).desc())
                    .offset(keep)
                )
            ).all()
            stale = [int(record_id) for record_id in ids if record_id is not None]
            if not stale:
                return 0
            await session.exec(delete(NotifyRecord).where(col(NotifyRecord.id).in_(stale)))
            await session.commit()
        return len(stale)
