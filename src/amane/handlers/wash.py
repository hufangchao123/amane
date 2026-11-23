from typing import TYPE_CHECKING

from ..db.models import TaskType, WashRecord
from ..library.wash import apply as wash_apply
from ..library.wash.model import WashGroup, is_unambiguous
from ..library.wash.service import WashKindFilter, WashService, parse_kind
from .models import WashApplyPayload, WashApplyResult, WashScanPayload, WashScanResult
from .protocol import FollowupTask, TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config.manager import HotSettings
    from ..db.repository import Repository
    from ._common import LibraryTaskLocks, media_refresh_followups


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

        # 只把「档位不同」或「同档位体积明显更大」的组交给自动执行; 其余仍在清单里等人挑.
        auto_keys = (
            [
                group.group_key
                for group in groups
                if is_unambiguous(group, min_gain_percent=self._hot.wash.auto_execute_min_gain_percent)
            ]
            if self._hot.wash.auto_execute
            else []
        )
        followups: list[FollowupTask] = []
        if auto_keys:
            followups.append(
                FollowupTask(
                    key="wash-apply:" + ",".join(sorted(auto_keys)),
                    task_type=TaskType.WASH_APPLY,
                    payload=WashApplyPayload(
                        group_keys=auto_keys, kind="local", library_id=payload.library_id
                    ).model_dump(mode="json"),
                    priority=-1,
                )
            )
        return TaskResult(
            success=True,
            result=WashScanResult(
                groups=len(groups),
                droppable=sum(len(group.drops) for group in groups),
                bytes_freed=sum(group.bytes_freed for group in groups),
                auto_groups=len(auto_keys),
            ),
            followups=followups,
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

        # 计数按动作分开: 还原往库里写文件, 与洗版释放的字节数不能加在一起.
        report = wash_apply.ApplyReport()
        restored = wash_apply.ApplyReport()
        purged = wash_apply.ApplyReport()
        trashed = wash_apply.ApplyReport()

        if payload.restore_ids:
            restored.absorb(
                await wash_apply.restore_records(
                    self._repo,
                    await self._fetch(payload.restore_ids, report),
                    lock_for=self._library_locks.get,
                    progress=self.report_progress,
                )
            )
        if payload.purge_ids:
            purged.absorb(
                await wash_apply.purge_records(
                    self._repo,
                    await self._fetch(payload.purge_ids, report),
                    lock_for=self._library_locks.get,
                    progress=self.report_progress,
                )
            )

        groups = await self._select_groups(
            service,
            kind=parse_kind(payload.kind),
            library_id=payload.library_id,
            keys=self._wash_keys(payload),
            report=report,
        )
        if groups:
            trashed.absorb(
                await wash_apply.wash_groups(
                    self._repo,
                    groups,
                    keep_days=int(self._hot.wash.keep_days),
                    lock_for=self._library_locks.get,
                    progress=self.report_progress,
                )
            )

        reports = (report, restored, purged, trashed)
        failed = sum(item.failed for item in reports)
        # 洗掉/还原/真删都改了盘上内容, 媒体服务器得重扫; 未指定库时没有可指向的库, 不入队.
        refresh: list[FollowupTask] = []
        if payload.library_id is not None:
            library = await self._repo.get_library(payload.library_id)
            if library is not None:
                refresh = media_refresh_followups(
                    self._hot,
                    library,
                    changed=trashed.done + restored.done + purged.done,
                    reason="wash",
                )
        return TaskResult(
            success=failed == 0,
            result=WashApplyResult(
                trashed=trashed.done,
                restored=restored.done,
                purged=purged.done,
                bytes_freed=trashed.bytes + purged.bytes,
                bytes_restored=restored.bytes,
                skipped=sum(item.skipped for item in reports),
                failed=failed,
            ),
            error="; ".join(error for item in reports for error in item.errors) or None,
            followups=refresh,
        )

    @staticmethod
    def _wash_keys(payload: WashApplyPayload) -> list[str] | None:
        """``None`` 表示重算全部候选; 空列表表示本次不洗任何组.

        只带还原 / 清理 id 时不能顺手把整个库洗一遍, 因此必须区分「没给键」与「不给键」.
        """
        if payload.group_keys:
            return payload.group_keys
        if payload.restore_ids or payload.purge_ids:
            return []
        return None

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
