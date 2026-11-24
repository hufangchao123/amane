from typing import TYPE_CHECKING

from ..db.models import TaskType, WashRecord
from ..library.wash import apply as wash_apply
from ..library.wash.model import WashGroup, is_unambiguous
from ..library.wash.service import WashKindFilter, WashService, parse_kind
from ..observability import current
from ._common import media_refresh_followups
from .models import WashApplyPayload, WashApplyResult, WashScanPayload, WashScanResult
from .protocol import FollowupTask, TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config.manager import HotSettings
    from ..db.repository import Repository
    from ._common import LibraryTaskLocks


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
        kind = parse_kind(payload.kind)
        every = await service.groups(kind=kind, library_id=payload.library_id)
        # 分组已经不存在的决定留着不会有害, 但会让人在设置里看到一条对不上任何组的记录.
        # 清理范围必须跟着这次扫描的范围走: 只扫一个库、或只扫一侧 (local/strm) 时, 范围之外的
        # 决定根本不是「过期」, 清了会让那些文件失去「都留着」的保护. 分组键里没有实体类型,
        # 所以只扫一侧时干脆不清.
        if kind is None:
            prefixes = (f"lib{payload.library_id}:",) if payload.library_id is not None else None
            dropped = await self._repo.drop_stale_wash_choices({group.group_key for group in every}, prefixes=prefixes)
            if dropped:
                current().info("wash choices dropped", count=dropped)
        # 「都留着」的组不进统计也不给自动执行: 使用者已经表态不洗这两份.
        groups = [group for group in every if not group.ignored]
        await self.report_progress(1, 1, "done")

        # 只把「档位不同」或「同档位体积明显更大」的组交给自动执行; 其余仍在清单里等人挑.
        auto_groups = (
            [
                group
                for group in groups
                if is_unambiguous(group, min_gain_percent=self._hot.wash.auto_execute_min_gain_percent)
            ]
            if self._hot.wash.auto_execute
            else []
        )
        # 按库分开发: 执行侧要求「有分组键或明确库」, 全库扫描时必须把每个组的库带上, 否则整批被拒.
        per_library: dict[int, list[str]] = {}
        for group in auto_groups:
            per_library.setdefault(group.library_id, []).append(group.group_key)
        followups = [
            FollowupTask(
                key=f"wash-apply:{library_id}:" + ",".join(sorted(keys)),
                task_type=TaskType.WASH_APPLY,
                payload=WashApplyPayload(group_keys=keys, kind="local", library_id=library_id).model_dump(mode="json"),
                priority=-1,
            )
            for library_id, keys in sorted(per_library.items())
        ]
        return TaskResult(
            success=True,
            result=WashScanResult(
                groups=len(groups),
                droppable=sum(len(group.drops) for group in groups),
                bytes_freed=sum(group.bytes_freed for group in groups),
                auto_groups=len(auto_groups),
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
        if not (payload.group_keys or payload.restore_ids or payload.purge_ids) and payload.library_id is None:
            # 三个清单都空、又没有库, 等于「所有库按当前策略全洗一遍」: 一次误点会动到用户没在看的库.
            # 全量重算必须显式指定媒体库; 逐组执行要给分组键; 还原与清理要给记录 id.
            return TaskResult(success=False, error="洗版需要指定媒体库或具体分组")

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
        """按 id 取记录当候选清单; 不存在的 id 记一条失败, 不抛异常.

        这里读到的是锁外的快照, 只用来拿分组键、提示缺失的 id: ``state`` 与 ``expires_at``
        由 :mod:`amane.library.wash.apply` 在库锁内重读, 因此等锁期间被还原或真删的记录不会重复处理.
        """
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
        groups = await service.groups(kind=kind, library_id=library_id, include_ignored=False)
        if keys is None:
            return groups
        wanted = set(keys)
        selected = [group for group in groups if group.group_key in wanted]
        found = {group.group_key for group in selected}
        report.skipped += len([key for key in wanted if key not in found])
        return selected
