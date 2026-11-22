"""TRASH_PURGE: 永久删除历史回收站 (``.amane_trash``) 里超过保留期的文件.

默认只统计不删除: 这一步不可回滚, 因此由调用方显式关掉 ``dry_run``. 删除发生在库内, 与整理 /
删除 / 洗版共用 ``LibraryTaskLocks``. 只清理 ``.amane_trash``, 洗版回收站 (``.amane_wash``) 由到期清理负责.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from ..library.cleanup import legacy
from ..library.rules import TRASH_DIRNAME
from .models import TrashPurgePayload, TrashPurgeResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..db.models import Library
    from ..db.repository import Repository
    from ._common import LibraryTaskLocks


class TrashPurgeHandler(TaskHandler[TrashPurgePayload, TrashPurgeResult]):
    """按修改时间清理 ``.amane_trash``; 不还原, 也不动库内索引."""

    def __init__(self, repo: Repository, library_locks: LibraryTaskLocks) -> None:
        super().__init__(payload_t=TrashPurgePayload, result_t=TrashPurgeResult)
        self._repo = repo
        self._library_locks = library_locks

    async def handle(self, payload: TrashPurgePayload) -> TaskResult[TrashPurgeResult]:
        if payload.library_id is not None and await self._repo.get_library(payload.library_id) is None:
            return TaskResult(success=False, error=f"媒体库 {payload.library_id} 不存在")

        libraries = await self._repo.list_libraries()
        if payload.library_id is not None:
            libraries = [library for library in libraries if library.id == payload.library_id]

        result = TrashPurgeResult(dry_run=payload.dry_run)
        for library in libraries:
            await self._tally(library, payload, result)
        return TaskResult(success=True, result=result)

    async def _tally(self, library: Library, payload: TrashPurgePayload, result: TrashPurgeResult) -> None:
        trash_dir = Path(library.path) / TRASH_DIRNAME
        if not trash_dir.is_dir():
            return
        if library.id is None:
            return
        if payload.dry_run:
            outcome = await legacy.purge(trash_dir, payload.keep_days, dry_run=True)
        else:
            async with await self._library_locks.get(library.id):
                outcome = await legacy.purge(trash_dir, payload.keep_days, dry_run=False)
        result.libraries += 1
        result.scanned += outcome.scanned
        result.purged += outcome.purged
        result.bytes_freed += outcome.bytes_freed
        result.kept += outcome.kept
        result.failed += outcome.failed
        result.samples.extend(outcome.samples[: max(0, 20 - len(result.samples))])
