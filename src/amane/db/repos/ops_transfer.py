"""``transfer_items`` 的读写面: 中转任务独占写, 状态卡与路由只读.

状态是任务与页面之间唯一的沟通渠道, 所以这里不缓存、不批量延后写: 每轮判定都基于最近一次
落库的结果, 页面上看到的就是任务最后写下的.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import col, select

from ..models import TransferItem, TransferItemState
from .base import RepositoryMixinBase, chunks

logger = structlog.get_logger()


class TransferRepoMixin(RepositoryMixinBase):
    """一行一个网盘 ID: 文件用 ``fid``, 文件夹用 ``cid`` (表只有一个 ID 列)."""

    async def transfer_state_counts(self) -> dict[TransferItemState, int]:
        """按状态分组计数; 还没出现过的状态不出现在结果里, 由调用方按 0 处理."""
        async with self._session() as session:
            rows = (
                await session.exec(select(col(TransferItem.state), func.count()).group_by(col(TransferItem.state)))
            ).all()
        return {state: int(count) for state, count in rows}

    async def list_transfer_items(
        self, *, state: TransferItemState | None, offset: int, limit: int
    ) -> tuple[list[TransferItem], int]:
        """最近变动的在前; 返回的 ``total`` 不受本页 ``limit`` 截断."""
        stmt = select(TransferItem)
        total_stmt = select(func.count()).select_from(TransferItem)
        if state is not None:
            stmt = stmt.where(col(TransferItem.state) == state)
            total_stmt = total_stmt.where(col(TransferItem.state) == state)
        stmt = stmt.order_by(col(TransferItem.updated_at).desc(), col(TransferItem.id).desc())
        async with self._session() as session:
            total = int((await session.exec(total_stmt)).one())
            rows = list((await session.exec(stmt.offset(offset).limit(limit))).all())
        return rows, total

    async def transfer_snapshots(self, ids: Sequence[str]) -> dict[str, TransferItem]:
        """一轮开始时读到的行: 判定与写入分开, 避免边扫边读库."""
        result: dict[str, TransferItem] = {}
        if not ids:
            return result
        async with self._session() as session:
            for chunk in chunks(list(ids)):
                rows = (await session.exec(select(TransferItem).where(col(TransferItem.fid).in_(chunk)))).all()
                for row in rows:
                    result[row.fid] = row
        return result

    async def track_transfer_items(self, entries: Sequence[tuple[str, str]]) -> None:
        """把首次见到的项落成 ``pending``.

        已存在的行原样保留: 重复扫到同一个 fid 既不能把 ``moved`` 打回 ``pending``, 也不能
        重置首见时间 —— 成熟等待由 ``updated_at`` 起算, 重置等于永远搬不走.
        """
        if not entries:
            return
        now = datetime.now(UTC)
        params: list[dict[str, object]] = [
            {
                "fid": fid,
                "file_name": name,
                "state": TransferItemState.PENDING,
                "attempts": 0,
                "updated_at": now,
            }
            for fid, name in entries
        ]
        stmt = sqlite_insert(TransferItem).on_conflict_do_nothing(index_elements=["fid"])
        async with self._session() as session:
            for chunk in chunks(params):
                await session.exec(stmt, params=chunk)
            await session.commit()

    async def mark_transfer_moved(self, fid: str, file_name: str) -> None:
        now = datetime.now(UTC)
        await self._apply_transfer(
            fid,
            {
                "file_name": file_name,
                "state": TransferItemState.MOVED,
                "attempts": col(TransferItem.attempts) + 1,
                "error": None,
                "moved_at": now,
                "updated_at": now,
            },
        )

    async def mark_transfer_skipped(self, fid: str, file_name: str, reason: str) -> None:
        """跳过是终态: 下一轮不再重判, 否则广告与含广告的文件夹每轮都要重新判一遍."""
        await self._apply_transfer(
            fid,
            {
                "file_name": file_name,
                "state": TransferItemState.SKIPPED,
                "error": reason,
                "updated_at": datetime.now(UTC),
            },
        )

    async def mark_transfer_failed(self, fid: str, file_name: str, error: str) -> None:
        await self._apply_transfer(
            fid,
            {
                "file_name": file_name,
                "state": TransferItemState.FAILED,
                "attempts": col(TransferItem.attempts) + 1,
                "error": error,
                "updated_at": datetime.now(UTC),
            },
        )

    async def _apply_transfer(self, fid: str, values: dict[str, object]) -> None:
        async with self._session() as session:
            await session.exec(update(TransferItem).where(col(TransferItem.fid) == fid).values(values))
            await session.commit()
