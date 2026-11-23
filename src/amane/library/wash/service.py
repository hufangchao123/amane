"""洗版的只读视图: 状态卡、候选分组与回收站列表.

本模块只读: 移动、还原、真删都在任务里 (``handlers.wash``), 因为那三步都要先取
``LibraryTaskLocks`` 再改库内路径. 路由拿到的是这里算出来的视图, 不自己遍历文件系统 ——
``docs/dev/ops.md`` 禁止在路由里跑文件遍历.

不导出到 ``amane.library`` 的公共面: 配置层反过来依赖 ``library``, 在这里汇入会形成循环导入.
"""

from __future__ import annotations

from dataclasses import replace
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy import select as sa_select
from sqlmodel import col, select

from ...config.manager import WashConfig
from ...db.models import MediaFile, SortOrder, WashChoiceAction, WashRecordKind, WashRecordState
from ...db.repos.ops_wash import WashRecordSortField
from .collect import collect_entries
from .model import WashGroup, build_groups, sort_key

logger = structlog.get_logger()

if TYPE_CHECKING:
    from ...db.models import WashRecord
    from ...db.repository import Repository

#: 洗版接口里 ``kind`` 的三态: 两种实体加「都要」.
WashKindFilter = WashRecordKind | None
#: 接口与任务 payload 里的 ``kind`` 取值.
WashScope = Literal["local", "strm", "all"]


def parse_kind(scope: WashScope) -> WashKindFilter:
    """三态 → 实体过滤; ``all`` 返回 ``None``, 具体两侧是否都参与由配置决定.

    路由与任务共用这一处映射, 免得两边的 ``local``/``strm`` 认法走样.
    """
    match scope:
        case "local":
            return WashRecordKind.LOCAL
        case "strm":
            return WashRecordKind.STRM
        case _:
            return None


class WashCandidateSortField(StrEnum):
    """候选列表的排序字段."""

    BYTES_FREED = "bytes_freed"
    GROUP_KEY = "group_key"
    KEEP_PATH = "keep_path"


class WashCandidate(BaseModel):
    """一组同番号文件的洗版候选: 留一份, 其余是落选者."""

    group_key: str
    kind: WashRecordKind
    library_id: int
    keep_path: str
    keep_size: int = 0
    drop_paths: list[str] = []
    bytes_freed: int = 0
    ignored: bool = False
    """使用者选了「都留着」, 这一组不计入可回收体积."""
    pinned: bool = False
    """保留者是使用者钉住的, 不是算法选的."""


class WashSummary(BaseModel):
    groups: int = 0
    """存在多份文件的分组数."""
    trashed: int = 0
    """回收站内可还原的记录数."""
    restorable_bytes: int = 0


class WashService:
    """洗版的只读视图; 构造参数只有配置与仓储, 不持有状态."""

    def __init__(self, config: WashConfig, repo: Repository) -> None:
        self._config = config
        self._repo = repo

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled and (self._config.local or self._config.strm))

    def kinds(self, kind: WashKindFilter) -> list[WashRecordKind]:
        """把 ``kind`` 过滤展开成实际参与的实体类型; 关闭的一侧不参与.

        显式指定一侧时同样要过开关: 候选页的筛选、任务载荷与助理提交都能直接给出 ``kind``,
        关掉 STRM 洗版后一个 ``kind=strm`` 就能把播放直链移进回收站.
        """
        if kind is not None:
            enabled = self._config.local if kind is WashRecordKind.LOCAL else self._config.strm
            return [kind] if enabled else []
        out: list[WashRecordKind] = []
        if self._config.local:
            out.append(WashRecordKind.LOCAL)
        if self._config.strm:
            out.append(WashRecordKind.STRM)
        return out

    async def summary(self) -> WashSummary:
        """状态卡: 待处理组数 + 回收站条数与可还原体积.

        待处理组数走一次分组聚合, 口径是「同库同番号同有码标记, 且至少一份不小于
        ``min_size_mb``」. 这是估算: 索引里没有分段 (CD) 与磁盘实况, 权威数字来自 ``wash_scan``
        任务. 状态卡要便宜, 不能在每次打开页面时跑一遍文件遍历.
        """
        trashed, restorable_bytes = await self._repo.count_wash_records(WashRecordState.TRASHED)
        groups = await self._estimate_groups() if self.enabled else 0
        return WashSummary(groups=groups, trashed=trashed, restorable_bytes=restorable_bytes)

    async def candidates(
        self,
        *,
        kind: WashKindFilter = None,
        library_id: int | None = None,
        offset: int = 0,
        limit: int = 50,
        sort_by: WashCandidateSortField = WashCandidateSortField.BYTES_FREED,
        order: SortOrder = SortOrder.DESC,
    ) -> tuple[list[WashCandidate], int]:
        """候选分组的一页. 与执行任务同一套算法, 因此列表看到的就是任务会搬走的东西."""
        groups = await self.groups(kind=kind, library_id=library_id)
        ordered = self._sorted_groups(groups, sort_by=sort_by, order=order)
        page = ordered[offset : offset + limit]
        return [self._candidate(group) for group in page], len(ordered)

    async def groups(
        self, *, kind: WashKindFilter = None, library_id: int | None = None, include_ignored: bool = True
    ) -> list[WashGroup]:
        """当前的候选分组 (未排序), 已套上使用者的显式决定.

        ``include_ignored`` 为假时把「都留着」的组摘掉 —— 执行路径用它, 免得搬走使用者要留的东西;
        列表页用真值, 好让那几组还能被撤销.
        """
        entries = await collect_entries(self._repo, kinds=self.kinds(kind), library_id=library_id)
        groups = build_groups(
            entries,
            min_size_bytes=self._min_size_bytes(),
            prefer=self._config.prefer,
            strm_prefer=self._config.strm_prefer,
        )
        chosen = await self._apply_choices(groups)
        if include_ignored:
            return chosen
        return [group for group in chosen if not group.ignored]

    async def _apply_choices(self, groups: list[WashGroup]) -> list[WashGroup]:
        """把 ``wash_choices`` 套到分组上.

        ``ignore`` 清掉落选者 (组里两份都留着); ``keep`` 把使用者钉的那一份提到保留位. 钉住的那份
        已经不在组里 (被删或被洗掉) 时按算法结果处理, 不因为一条过期决定卡住整组.
        """
        choices = await self._repo.list_wash_choices()
        if not choices:
            return groups
        applied: list[WashGroup] = []
        for group in groups:
            choice = choices.get(group.group_key)
            if choice is None:
                applied.append(group)
                continue
            if choice.action is WashChoiceAction.IGNORE:
                applied.append(replace(group, drops=(), ignored=True))
                continue
            members = (group.keep, *group.drops)
            pinned = next((entry for entry in members if entry.path == choice.keep_path), None)
            if pinned is None:
                applied.append(group)
                continue
            applied.append(
                replace(group, keep=pinned, drops=tuple(entry for entry in members if entry.path != pinned.path))
            )
        return applied

    async def records(
        self,
        *,
        state: WashRecordState | None = None,
        kind: WashRecordKind | None = None,
        offset: int = 0,
        limit: int = 50,
        sort_by: WashRecordSortField = WashRecordSortField.CREATED_AT,
        order: SortOrder = SortOrder.DESC,
    ) -> tuple[list[WashRecord], int]:
        """回收站列表的一页."""
        return await self._repo.list_wash_records(
            state=state, kind=kind, offset=offset, limit=limit, sort_by=sort_by, order=order
        )

    def _min_size_bytes(self) -> int:
        return int(self._config.min_size_mb) * 1024 * 1024

    def _candidate(self, group: WashGroup) -> WashCandidate:
        return WashCandidate(
            group_key=group.group_key,
            kind=group.kind,
            library_id=group.library_id,
            keep_path=group.keep.path,
            keep_size=group.keep.size,
            drop_paths=[entry.path for entry in group.drops],
            bytes_freed=group.bytes_freed,
            ignored=group.ignored,
            pinned=not group.ignored and group.keep.path != self._algorithm_keep_path(group),
        )

    def _algorithm_keep_path(self, group: WashGroup) -> str:
        """不算使用者决定时算法会选的那一份; 用来区分「钉住的保留者」与「算法选的」."""
        if group.ignored or not group.drops:
            return group.keep.path
        members = sorted(
            (group.keep, *group.drops), key=lambda entry: sort_key(entry, self._config.prefer, self._config.strm_prefer)
        )
        return members[0].path

    def _sorted_groups(
        self, groups: list[WashGroup], *, sort_by: WashCandidateSortField, order: SortOrder
    ) -> list[WashGroup]:
        """排序键后一律用分组键兜底, 保证翻页不重不漏."""
        reverse = order is SortOrder.DESC
        match sort_by:
            case WashCandidateSortField.GROUP_KEY:
                return sorted(groups, key=lambda group: (group.group_key, group.kind.value), reverse=reverse)
            case WashCandidateSortField.KEEP_PATH:
                return sorted(groups, key=lambda group: (group.keep.path, group.group_key), reverse=reverse)
            case _:
                return sorted(groups, key=lambda group: (group.bytes_freed, group.group_key), reverse=reverse)

    async def _estimate_groups(self) -> int:
        """按索引估算待处理组数: 同库 + 同番号 + 同有码标记, 且至少一份不小于 ``min_size_mb``.

        与 ``build_groups`` 的差别只在两处: 不区分分段 (索引里没有 CD 字段, 同番号的 CD1/CD2
        会被算成一组, 略微高估), 也不看磁盘实况. 权威数字仍以扫描任务为准.
        """
        # 单列计数套在子查询外面: 多列聚合的 select 在 sqlmodel 的重载里绑不上类型,
        # 而 ``select(func.count()).select_from(<子查询>)`` 是既有仓储里通用的写法.
        floor = self._min_size_bytes()
        grouped = (
            sa_select(col(MediaFile.library_id), col(MediaFile.number), col(MediaFile.mosaic))
            .where(col(MediaFile.number).is_not(None), col(MediaFile.number) != "")
            .group_by(col(MediaFile.library_id), col(MediaFile.number), col(MediaFile.mosaic))
            .having(func.count() > 1, func.max(col(MediaFile.size)) >= floor)
            .subquery()
        )
        stmt = select(func.count()).select_from(grouped)
        async with self._repo._session() as session:
            return int((await session.exec(stmt)).one())


__all__ = [
    "WashCandidate",
    "WashCandidateSortField",
    "WashService",
    "WashSummary",
    "parse_kind",
]
