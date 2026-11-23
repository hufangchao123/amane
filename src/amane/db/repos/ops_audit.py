"""``audit_records`` 的读写面.

写入方是请求路径上的留痕队列 (见 ``observability/audit.py``), 读取面只有列表与清空.
条数超过上限时由写入方修剪, 因此计数始终有界.
"""

from sqlmodel import col, delete, func, select

from ..models import AuditRecord
from .base import RepositoryMixinBase


class AuditRepoMixin(RepositoryMixinBase):
    """``audit_records`` 的读写面."""

    async def add_audit_record(
        self, *, method: str, path: str, query: str, status: int, duration_ms: int, client: str
    ) -> None:
        """登记一次写操作.

        不回读主键: 留痕的调用方只用成败, 多一次 ``refresh`` 就是每个写请求多一个往返.
        """
        async with self._session() as session:
            session.add(
                AuditRecord(
                    method=method, path=path, query=query, status=status, duration_ms=duration_ms, client=client
                )
            )
            await session.commit()

    async def list_audit_records(self, *, limit: int, offset: int, method: str | None = None) -> list[AuditRecord]:
        """按时间倒序分页; ``method`` 为空表示不限方法."""
        async with self._session() as session:
            stmt = select(AuditRecord).order_by(col(AuditRecord.created_at).desc(), col(AuditRecord.id).desc())
            if method:
                stmt = stmt.where(col(AuditRecord.method) == method)
            return list((await session.exec(stmt.offset(offset).limit(limit))).all())

    async def count_audit_records(self, method: str | None = None) -> int:
        async with self._session() as session:
            stmt = select(func.count()).select_from(AuditRecord)
            if method:
                stmt = stmt.where(col(AuditRecord.method) == method)
            return int((await session.exec(stmt)).one())

    async def prune_audit_records(self, keep: int) -> int:
        """只留最近 ``keep`` 条, 返回删除条数."""
        async with self._session() as session:
            # 用子查询一次删完: 落地 5000 个 id 再回传, 每个留痕批次都多背一遍这个列表.
            stale = (
                select(col(AuditRecord.id))
                .order_by(col(AuditRecord.created_at).desc(), col(AuditRecord.id).desc())
                .offset(keep)
            )
            result = await session.exec(delete(AuditRecord).where(col(AuditRecord.id).in_(stale)))
            await session.commit()
            return int(result.rowcount or 0)
        return len(stale)

    async def clear_audit_records(self) -> int:
        """清空全部留痕, 返回删除条数."""
        async with self._session() as session:
            total = int((await session.exec(select(func.count()).select_from(AuditRecord))).one())
            await session.exec(delete(AuditRecord))
            await session.commit()
        return total
