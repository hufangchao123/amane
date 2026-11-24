"""``wash_records`` 的读写面.

洗版一行记录对应一次「留一个、移一个」: 落选文件搬进回收站时插入一行 ``trashed``, 还原与
到期清理各自把状态推进到 ``restored`` / ``purged``. 本模块只在数据库里记账, 不碰磁盘 ——
搬移、还原、真删都在 ``amane.library.wash.trash`` 里, 由任务编排, 顺序是「先动文件、再改状态」.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import func
from sqlmodel import col, select

from ..models import SortOrder, WashChoice, WashChoiceAction, WashRecord, WashRecordKind, WashRecordState
from ..repo_types import _order_clause, _utcnow
from .base import RepositoryMixinBase, chunks

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from sqlalchemy.orm import Mapped
    from sqlmodel.sql.expression import SelectOfScalar


class WashRecordSortField(StrEnum):
    """回收站列表的排序字段."""

    CREATED_AT = "created_at"
    EXPIRES_AT = "expires_at"
    SIZE_BYTES = "size_bytes"
    GROUP_KEY = "group_key"


# 显式枚举 → 列, 不用反射: 类型检查要能验证列存在.
_WASH_RECORD_SORT_COLUMNS: dict[WashRecordSortField, Mapped[Any]] = {
    WashRecordSortField.CREATED_AT: col(WashRecord.created_at),
    WashRecordSortField.EXPIRES_AT: col(WashRecord.expires_at),
    WashRecordSortField.SIZE_BYTES: col(WashRecord.size_bytes),
    WashRecordSortField.GROUP_KEY: col(WashRecord.group_key),
}


class WashRepoMixin(RepositoryMixinBase):
    """``wash_records`` 的读写面."""

    async def add_wash_record(
        self,
        *,
        kind: WashRecordKind,
        group_key: str,
        kept_path: str,
        removed_path: str,
        size_bytes: int,
        expires_at: datetime | None,
    ) -> int:
        """登记一条「留一个、移一个」; 返回记录 id (真删与还原都按 id 找到落点).

        ``expires_at`` 为空表示这条记录不会因到期被清理, 只能手工还原或清理.
        """
        record = WashRecord(
            kind=kind,
            group_key=group_key,
            kept_path=kept_path,
            removed_path=removed_path,
            size_bytes=size_bytes,
            state=WashRecordState.TRASHED,
            expires_at=expires_at,
        )
        async with self._session() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            assert record.id is not None
            return record.id

    async def trashed_wash_paths(self) -> set[str]:
        """仍在回收站里的 ``removed_path`` 集合: 执行前用它做幂等判定, 重复跑不会重复记一行."""
        async with self._session() as session:
            rows = (
                await session.exec(select(WashRecord.removed_path).where(WashRecord.state == WashRecordState.TRASHED))
            ).all()
        return set(rows)

    async def get_wash_records(self, ids: Sequence[int]) -> list[WashRecord]:
        """按 id 取记录; 不存在的 id 直接缺席, 由调用方判定为非法."""
        if not ids:
            return []
        records: list[WashRecord] = []
        async with self._session() as session:
            for chunk in chunks(list(ids)):
                records.extend((await session.exec(select(WashRecord).where(col(WashRecord.id).in_(chunk)))).all())
        return records

    async def list_wash_records(
        self,
        *,
        state: WashRecordState | None = None,
        kind: WashRecordKind | None = None,
        offset: int = 0,
        limit: int = 50,
        sort_by: WashRecordSortField = WashRecordSortField.CREATED_AT,
        order: SortOrder = SortOrder.DESC,
    ) -> tuple[list[WashRecord], int]:
        """一页回收站记录与总数; 排序键后按 id 兜底, 保证翻页不重不漏."""
        stmt: SelectOfScalar[WashRecord] = select(WashRecord)
        if state is not None:
            stmt = stmt.where(col(WashRecord.state) == state)
        if kind is not None:
            stmt = stmt.where(col(WashRecord.kind) == kind)
        count_stmt = select(func.count()).select_from(stmt.subquery())
        ordering = _order_clause(_WASH_RECORD_SORT_COLUMNS[sort_by], order)
        paged = stmt.order_by(ordering, col(WashRecord.id).asc()).offset(offset).limit(limit)
        async with self._session() as session:
            items: list[WashRecord] = list((await session.exec(paged)).all())
            total = (await session.exec(count_stmt)).one()
        return items, int(total)

    async def list_expired_wash_records(self, now: datetime) -> list[WashRecord]:
        """回收站里已到期的记录: 定时清理据此入队, 无到期记录时返回空表."""
        stmt = select(WashRecord).where(
            col(WashRecord.state) == WashRecordState.TRASHED,
            col(WashRecord.expires_at).is_not(None),
            col(WashRecord.expires_at) <= now,
        )
        async with self._session() as session:
            return list((await session.exec(stmt.order_by(col(WashRecord.id).asc()))).all())

    async def set_wash_record_state(self, ids: Sequence[int], state: WashRecordState) -> int:
        """把一批记录推进到新状态, 返回实际改动的行数."""
        if not ids:
            return 0
        changed = 0
        async with self._session() as session:
            for chunk in chunks(list(ids)):
                records: list[WashRecord] = list(
                    (await session.exec(select(WashRecord).where(col(WashRecord.id).in_(chunk)))).all()
                )
                for record in records:
                    record.state = state
                    session.add(record)
                changed += len(records)
            await session.commit()
        return changed

    async def count_wash_records(self, state: WashRecordState) -> tuple[int, int]:
        """指定状态的记录数与总体积; 状态卡要的行数与可还原体积都从这里取."""
        async with self._session() as session:
            count = (
                await session.exec(select(func.count()).select_from(WashRecord).where(col(WashRecord.state) == state))
            ).one()
            total_bytes = (
                await session.exec(
                    select(func.coalesce(func.sum(WashRecord.size_bytes), 0)).where(col(WashRecord.state) == state)
                )
            ).one()
        return int(count), int(total_bytes)

    async def list_wash_choices(self) -> dict[str, WashChoice]:
        """全部显式决定, 以分组键为索引; 扫描与执行每轮都读一次, 量级是「人挑过的组数」."""
        async with self._session() as session:
            rows = (await session.exec(select(WashChoice))).all()
            return {choice.group_key: choice for choice in rows}

    async def set_wash_choice(self, group_key: str, action: WashChoiceAction, keep_path: str = "") -> None:
        """写入或覆盖一组的决定; 同一组只保留一条, 改主意就是覆盖."""
        async with self._session() as session:
            existing = await session.scalar(select(WashChoice).where(col(WashChoice.group_key) == group_key))
            if existing is None:
                session.add(WashChoice(group_key=group_key, action=action, keep_path=keep_path))
            else:
                existing.action = action
                existing.keep_path = keep_path
                existing.updated_at = _utcnow()
                session.add(existing)
            await session.commit()

    async def clear_wash_choice(self, group_key: str) -> bool:
        """撤销一组的决定; 返回是否真删掉了一条."""
        async with self._session() as session:
            existing = await session.scalar(select(WashChoice).where(col(WashChoice.group_key) == group_key))
            if existing is None:
                return False
            await session.delete(existing)
            await session.commit()
            return True

    async def drop_stale_wash_choices(self, valid_keys: set[str], *, prefixes: tuple[str, ...] | None = None) -> int:
        """清掉分组已经不存在的决定; 文件被洗掉或删掉后这些键不会再出现.

        ``prefixes`` 限定清理范围: 局部扫描 (只扫一个库) 只能清该库的决定, 否则扫一次库 1
        就会把库 2 的「都留着」与「钉住」一起删掉, 那些文件随后会被自动执行洗走.
        """
        async with self._session() as session:
            rows = (await session.exec(select(WashChoice))).all()
            scoped = [choice for choice in rows if prefixes is None or str(choice.group_key).startswith(prefixes)]
            stale = [choice for choice in scoped if choice.group_key not in valid_keys]
            for choice in stale:
                await session.delete(choice)
            if stale:
                await session.commit()
            return len(stale)
