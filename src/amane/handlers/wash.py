from typing import TYPE_CHECKING

from ..db.models import WashRecord
from ..library.wash import apply as wash_apply
from ..library.wash.model import WashGroup
from ..library.wash.service import WashKindFilter, WashService, parse_kind
from .models import WashApplyPayload, WashApplyResult, WashScanPayload, WashScanResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config.manager import HotSettings
    from ..db.repository import Repository
    from ._common import LibraryTaskLocks

#: 还原 / 清理的请求编码在 ``group_keys`` 里: 任务类型封闭 (没有 WASH_RESTORE / WASH_PURGE), 而
#: ``WashApplyPayload`` 在共享文件里. 保留前缀的来龙去脉见 ``amane-merge/notes/wash.md``.
RESTORE_PREFIX = "@restore:"
PURGE_PREFIX = "@purge:"


def _split_keys(keys: list[str]) -> tuple[list[int], list[int], list[str]]:
    """把 ``group_keys`` 拆成 还原 id / 清理 id / 待洗分组键.

    认不出的键静默丢弃: 它们既不是记录 id 也不是本库的分组键, 说明请求由旧版本或手写产生,
    当成「没有要洗的组」比抛异常更合适 —— 非法 id 会在查表后计入 ``failed``.
    """
    restore_ids: list[int] = []
    purge_ids: list[int] = []
    group_keys: list[str] = []
    for key in keys:
        if key.startswith(RESTORE_PREFIX):
            tail = key[len(RESTORE_PREFIX) :]
            if tail.isdigit():
                restore_ids.append(int(tail))
        elif key.startswith(PURGE_PREFIX):
            tail = key[len(PURGE_PREFIX) :]
            if tail.isdigit():
                purge_ids.append(int(tail))
        elif key:
            group_keys.append(key)
    return restore_ids, purge_ids, group_keys


class WashScanHandler(TaskHandler[WashScanPayload, WashScanResult]):
    """扫描同番号多份文件, 产出候选分组与可回收体积.

    只读, 不改库内路径, 因此不取 ``LibraryTaskLocks``.
    """

    def __init__(self, repo: Repository, hot: HotSettings) -> None:
        super().__init__(payload_t=WashScanPayload, result_t=WashScanResult)
        self._repo = repo
        self._hot = hot

    async def handle(self, payload: WashScanPayload) -> TaskResult[WashScanResult]:
        service = WashService(self._hot.wash, self._repo)
        if not service.enabled:
            return TaskResult(success=False, error="洗版未启用, 请先在设置里打开洗版")
        if payload.library_id is not None and await self._repo.get_library(payload.library_id) is None:
            return TaskResult(success=False, error=f"媒体库 {payload.library_id} 不存在")

        await self.report_progress(0, 1, "scan")
        groups = await service.groups(kind=parse_kind(payload.kind), library_id=payload.library_id)
        await self.report_progress(1, 1, "done")
        return TaskResult(
            success=True,
            result=WashScanResult(
                groups=len(groups),
                droppable=sum(len(group.drops) for group in groups),
                bytes_freed=sum(group.bytes_freed for group in groups),
            ),
        )


class WashApplyHandler(TaskHandler[WashApplyPayload, WashApplyResult]):
    """同番号多份文件留最优的一份, 其余移入回收站并登记 ``wash_records``.

    会移动库内文件, 因此与整理 / 删除共用 ``LibraryTaskLocks``, 不得绕过锁直接改库内路径.
    同一个 payload 里可以同时带还原 / 清理 / 洗版三种请求, 按这个顺序执行: 还原把文件放回库,
    清理腾出空间, 洗版最后搬 —— 反过来会让刚还原的文件立刻又被当成落选者搬走.
    """

    def __init__(self, repo: Repository, hot: HotSettings, library_locks: LibraryTaskLocks) -> None:
        super().__init__(payload_t=WashApplyPayload, result_t=WashApplyResult)
        self._repo = repo
        self._hot = hot
        self._library_locks = library_locks

    async def handle(self, payload: WashApplyPayload) -> TaskResult[WashApplyResult]:
        service = WashService(self._hot.wash, self._repo)
        if not service.enabled:
            return TaskResult(success=False, error="洗版未启用, 请先在设置里打开洗版")
        if payload.library_id is not None and await self._repo.get_library(payload.library_id) is None:
            return TaskResult(success=False, error=f"媒体库 {payload.library_id} 不存在")

        restore_ids, purge_ids, group_keys = _split_keys(payload.group_keys)
        report = wash_apply.ApplyReport()

        if restore_ids:
            report.absorb(
                await wash_apply.restore_records(
                    self._repo, await self._fetch(restore_ids, report), lock_for=self._library_locks.get
                )
            )
        if purge_ids:
            report.absorb(await wash_apply.purge_records(self._repo, await self._fetch(purge_ids, report)))

        # 只有「一个键都没给」才等于全部候选; 只给了还原 / 清理键时不能顺手把整个库洗一遍.
        groups = await self._select_groups(
            service,
            kind=parse_kind(payload.kind),
            library_id=payload.library_id,
            keys=group_keys if payload.group_keys else None,
            report=report,
        )
        if groups:
            report.absorb(
                await wash_apply.wash_groups(
                    self._repo,
                    groups,
                    keep_days=int(self._hot.wash.keep_days),
                    lock_for=self._library_locks.get,
                    progress=self.report_progress,
                )
            )

        return TaskResult(
            success=report.failed == 0,
            result=WashApplyResult(
                trashed=report.done,
                bytes_freed=report.bytes,
                skipped=report.skipped,
                failed=report.failed,
            ),
            error="; ".join(report.errors) if report.errors else None,
        )

    async def _fetch(self, ids: list[int], report: wash_apply.ApplyReport) -> list[WashRecord]:
        """按 id 取记录; 不存在的 id 记一条失败, 不抛异常."""
        records = await self._repo.get_wash_records(ids)
        known = {record.id for record in records}
        missing = len([record_id for record_id in ids if record_id not in known])
        if missing:
            report.fail(f"{missing} 条回收站记录不存在")
        return records

    async def _select_groups(
        self,
        service: WashService,
        *,
        kind: WashKindFilter,
        library_id: int | None,
        keys: list[str] | None,
        report: wash_apply.ApplyReport,
    ) -> list[WashGroup]:
        """按当前索引重算分组, 再挑出这次要动的组.

        重新计算而不是信任请求: 候选列表可能已经过期, 而「要搬走的路径」必须以此刻的磁盘为准.
        显式给的键里如果有已经不成立的组, 记为跳过而不是失败 —— 上一轮或别的流程已经处理完了.
        """
        groups = await service.groups(kind=kind, library_id=library_id)
        if keys is None:
            return groups
        wanted = set(keys)
        selected = [group for group in groups if group.group_key in wanted]
        found = {group.group_key for group in selected}
        report.skipped += len([key for key in wanted if key not in found])
        return selected
