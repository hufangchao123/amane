"""MEDIA_PURGE: 片库里勾一批条目, 连文件一起删掉.

由条目出发而不是由路径清单出发: 一个条目可能对应多份文件 (分卷、多画质), 逐个文件处理,
最后把条目本身删掉. 复验口径与 ``delete.py`` 一致 —— 读不到的文件只记 missing 不报错;
路径落在回收站里的拒绝删除, 那里的东西还能还原.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..library.delete import ancestor_dirs, prune_empty_dirs, same_path
from ..library.rules import TRASH_DIRNAME, WASH_TRASH_DIRNAME, delete_roots, landing_roots, publish_roots
from ..utils.path import is_descendant
from ..utils.threads import disk_probe, in_thread, path_is_dir
from ._common import LibraryTaskLocks, media_refresh_followups
from .models import MediaPurgePayload, MediaPurgeResult
from .protocol import FollowupTask, TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config import HotSettings
    from ..db.models import Library, MediaFile
    from ..db.repository import Repository

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class _FileOutcome:
    """单份文件的处理结果; 计数与「删掉的路径」一起给出, 后者用于剪枝."""

    deleted: int = 0
    indexed: int = 0
    missing: int = 0
    refused: int = 0
    failed: int = 0
    freed_bytes: int = 0
    path: Path | None = None


class MediaPurgeHandler(TaskHandler[MediaPurgePayload, MediaPurgeResult]):
    """删文件 → 删索引行 → 删片库条目 → 按需剪空目录.

    与同库的整理、转移、洗版共用一把锁: 删除按路径判定, 期间路径被搬走就会删错.
    ``delete_files=False`` 时只摘条目与索引, 磁盘保持原样 (等同把片子退回未刮削状态).
    """

    def __init__(
        self,
        repo: Repository,
        config: HotSettings | None = None,
        *,
        library_locks: LibraryTaskLocks | None = None,
    ):
        super().__init__(payload_t=MediaPurgePayload, result_t=MediaPurgeResult)
        self._repo = repo
        self._config = config
        self._library_locks = library_locks if library_locks is not None else LibraryTaskLocks()

    async def handle(self, payload: MediaPurgePayload) -> TaskResult[MediaPurgeResult]:
        library = await self._repo.get_library(payload.library_id)
        if library is None:
            return TaskResult(success=False, error=f"媒体库 {payload.library_id} 不存在")
        if library.id is None:
            return TaskResult(success=False, error="媒体库尚未落库")

        result = MediaPurgeResult()
        removed: list[Path] = []
        blocked: set[int] = set()
        # 索引行必须在锁内读: 等锁期间整理或转移可能把文件搬走, 按锁外读到的旧路径去 unlink 会落空,
        # 却照样把索引行删掉, 留下盘上没人认领的文件 (三审 C-4).
        async with await self._library_locks.get(library.id):
            files = await self._repo.list_media_files(
                library_id=library.id, metadata_ids=payload.metadata_ids, limit=None
            )
            for media in files:
                outcome = await self._purge_file(library, media, delete_files=payload.delete_files)
                result.files_deleted += outcome.deleted
                result.indexed += outcome.indexed
                result.missing += outcome.missing
                result.refused += outcome.refused
                result.failed += outcome.failed
                result.freed_bytes += outcome.freed_bytes
                if outcome.path is not None:
                    removed.append(outcome.path)
                if (outcome.failed or outcome.refused) and media.metadata_id is not None:
                    blocked.add(media.metadata_id)
            deletable = await self._deletable_metadata(library, payload.metadata_ids, blocked=blocked)
            deleted, _missing_entries = await self._repo.batch_delete_metadata(deletable)
            result.metadata_deleted = deleted
            if payload.prune_empty_dirs and removed and not library.keep_source_dirs:
                result.pruned_dirs = await self._prune(library, removed)

        logger.info(
            "media purge finished",
            metadata_deleted=result.metadata_deleted,
            files_deleted=result.files_deleted,
            indexed=result.indexed,
            missing=result.missing,
            refused=result.refused,
            failed=result.failed,
        )
        return TaskResult(
            success=result.failed == 0 and result.refused == 0,
            result=result,
            followups=self._followups(library, result.files_deleted),
        )

    async def _deletable_metadata(self, library: Library, ids: list[int], *, blocked: set[int]) -> list[int]:
        """筛出可以连同条目一起删的 metadata id.

        两条都不满足就留着条目: 有文件没处理成功 (``blocked``), 或别的媒体库里还有文件引用它
        —— 级联删除会把那些库的索引行重置成待刮削, 等于替另一个库决定「这片子没了」.
        """
        candidates = [metadata_id for metadata_id in dict.fromkeys(ids) if metadata_id not in blocked]
        if not candidates:
            return []
        leftover = await self._repo.list_media_files(metadata_ids=candidates, limit=None)
        busy = {file.metadata_id for file in leftover if file.library_id != library.id}
        return [metadata_id for metadata_id in candidates if metadata_id not in busy]

    async def _purge_file(self, library: Library, media: MediaFile, *, delete_files: bool) -> _FileOutcome:
        path = Path(media.path)
        if not _authorized(path, library):
            logger.warning("media purge refused", path=str(path), reason="路径不在本库的授权目录内")
            return _FileOutcome(refused=1)
        if _in_recycle(path):
            logger.warning("media purge refused", path=str(path), reason="路径在回收站里")
            return _FileOutcome(refused=1)
        probe = await disk_probe(path, follow_symlinks=False)
        if probe.unreadable:
            # 读不到不等于没有: 挂载抖动时删掉索引行与片库条目, 盘上会留一份谁也认不出的文件.
            logger.warning("media purge skipped", path=str(path), reason="路径读不到")
            return _FileOutcome(failed=1)
        if probe.path is None:
            if media.id is not None:
                await self._repo.delete_media_file(media.id)
            return _FileOutcome(missing=1, indexed=1, path=path)
        if await path_is_dir(path):
            logger.warning("media purge refused", path=str(path), reason="目标不是文件")
            return _FileOutcome(refused=1)
        if not delete_files:
            if media.id is not None:
                await self._repo.delete_media_file(media.id)
            return _FileOutcome(indexed=1, path=path)

        try:
            size = await _size(path)
            await _unlink(path)
        except OSError as exc:
            logger.warning("media purge failed", path=str(path), error=str(exc))
            return _FileOutcome(failed=1)
        if media.id is not None:
            await self._repo.delete_media_file(media.id)
        return _FileOutcome(deleted=1, indexed=1, freed_bytes=size, path=path)

    async def _prune(self, library: Library, paths: list[Path]) -> int:
        library_root = Path(library.path)
        bases = tuple(
            root
            for root in publish_roots(path=library.path, ok_dir=library.scrape_ok_dir)
            if not same_path(root, library_root)
        )
        candidates: list[Path] = []
        for path in paths:
            candidates.extend(ancestor_dirs(path, library_root=library_root, bases=bases))
        unique = list(dict.fromkeys(candidates))
        if not unique:
            return 0
        pruned = await prune_empty_dirs(unique, library_root=library_root, bases=bases)
        return pruned.removed

    def _followups(self, library: Library, changed: int) -> list[FollowupTask]:
        if self._config is None or changed <= 0:
            return []
        return media_refresh_followups(self._config, library, changed=changed, reason="delete")


@in_thread
def _size(path: Path) -> int:
    # lstat: 符号链接本身也要能删掉, 跟随链接会去 stat 目标 (坏链直接 FileNotFoundError).
    return path.lstat().st_size


@in_thread
def _unlink(path: Path) -> None:
    path.unlink()


def _authorized(path: Path, library: Library) -> bool:
    """文件是否落在本库允许删除的目录里: 库根、成功目录, 以及三个落点目录.

    符号链接按真实路径比: 库里有指向库外的链接时, 按字面前缀判定会放行到真正的目标.
    """
    roots = [
        *delete_roots(path=library.path, ok_dir=library.scrape_ok_dir),
        *landing_roots(
            input_dir=library.scrape_input_dir,
            ok_dir=library.scrape_ok_dir,
            fail_dir=library.scrape_fail_dir,
        ),
    ]
    return any(is_descendant(path, root) for root in roots)


def _in_recycle(path: Path) -> bool:
    """回收站里的东西还能还原, 删除任务不碰; 目录名判定与清理任务一致."""
    return any(part in (TRASH_DIRNAME, WASH_TRASH_DIRNAME) for part in path.parts)
