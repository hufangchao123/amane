"""SCRAPE_TRANSFER: 刮削结束后把该文件搬到对应的目的目录.

由 ``ScrapeHandler`` 扇出: 成功时随完成事务写链, 失败时由 worker 在判失败后单独入队.
落点由 ``library.transfer.scrape_transfer_destination`` 判定: 成功侧取成功目录 (没配但填了输入
目录则落库根), 失败侧取失败目录; 两边都可能没有落点, 那说明文件已经在它该在的地方, 只记一条说明.

搬运与整理 / 删除 / 洗版共用 ``LibraryTaskLocks``. 搬走的是视频与它同前缀的附属文件, 库内记录改指
新路径, 因此文件仍在库里被跟踪 (可再次刮削、可再整理). 落点就是库根且库开了自动整理时, 顺带排一次
该文件的整理: 刮削链在开启转移后不再排整理, 少了这一步文件会以原名平铺在库根里.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from ..db.models import TaskType
from ..library.delete import same_path
from ..library.transfer import scrape_transfer_destination, transfer_scrape_result
from ..utils.threads import path_exists
from ._common import media_refresh_followups, refresh_external_subtitle, wash_scan_followups
from .models import OrganizePayload, ScrapeTransferPayload, ScrapeTransferResult
from .protocol import FollowupTask, TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config import HotSettings
    from ..db.models import Library, MediaFile
    from ..db.repository import Repository
    from ..library import InventoryStore
    from ._common import LibraryTaskLocks


def configured_roots(library: Library) -> tuple[Path, ...]:
    """库里配置的目录 (输入 / 成功 / 失败): 搬空后也不删它们本身."""
    roots = (library.scrape_input_dir, library.scrape_ok_dir, library.scrape_fail_dir)
    return tuple(Path(root) for root in roots if root)


class ScrapeTransferHandler(TaskHandler[ScrapeTransferPayload, ScrapeTransferResult]):
    """单文件转移; 一个任务只处理一次刮削的那一个文件."""

    def __init__(
        self,
        repo: Repository,
        hot: HotSettings,
        library_locks: LibraryTaskLocks,
        inventory_store: InventoryStore | None = None,
    ) -> None:
        super().__init__(payload_t=ScrapeTransferPayload, result_t=ScrapeTransferResult)
        self._repo = repo
        self._hot = hot
        # 搬动改变了库内路径, 清理清单里的条目随即过期.
        self._inventory_store = inventory_store
        self._library_locks = library_locks

    async def handle(self, payload: ScrapeTransferPayload) -> TaskResult[ScrapeTransferResult]:
        result = ScrapeTransferResult(outcome=payload.outcome)
        file = await self._repo.get_media_file(payload.media_file_id)
        if file is None or file.id is None:
            return TaskResult(success=False, error=f"媒体文件 {payload.media_file_id} 不存在")
        library = await self._repo.get_library(file.library_id)
        if library is None:
            return TaskResult(success=False, error=f"媒体库 {file.library_id} 不存在")
        if not library.scrape_transfer:
            result.note = "该媒体库未开启刮削后转移"
            return TaskResult(success=True, result=result)
        destination = scrape_transfer_destination(library, payload.outcome)
        if not destination or library.id is None:
            result.note = "文件已在原处, 无需转移" if not destination else result.note
            return TaskResult(success=True, result=result)

        # 路径判定必须与搬移在同一把锁里: 锁外读到的路径可能已被整理任务改掉, 拿旧路径去搬会把
        # 刚整理好的成品再搬出库根.
        async with await self._library_locks.get(library.id):
            return await self._transfer_locked(payload, file, library, Path(destination), result)

    def _organize_followups(
        self, library: Library, file: MediaFile, destination: Path
    ) -> list[FollowupTask]:
        """落点就是库根时补一次整理, 让搬进来的文件按模板重命名归位.

        落点是单独的成品目录 (显式 ``scrape_ok_dir``) 时不整理: 那是使用者已经排好的树, 再按模板搬
        一次会把它打散. 整理排在转移自己的后继里, 与转移顺序执行, 不会争同一把库锁.
        """
        if not library.auto_organize or file.id is None or library.id is None:
            return []
        if not same_path(destination, Path(library.path)):
            return []
        return [
            FollowupTask(
                key=f"organize:{file.id}",
                task_type=TaskType.ORGANIZE,
                payload=OrganizePayload(library_id=library.id, media_file_ids=[file.id]).model_dump(
                    mode="json"
                ),
                priority=-1,
            )
        ]

    async def _transfer_locked(
        self,
        payload: ScrapeTransferPayload,
        file: MediaFile,
        library: Library,
        destination: Path,
        result: ScrapeTransferResult,
    ) -> TaskResult[ScrapeTransferResult]:
        """持锁之后再读一次当前路径, 再决定搬什么; 视频已在目的目录时只补搬附属文件."""
        assert file.id is not None
        fresh = await self._repo.get_media_file(file.id) or file
        assert fresh.id is not None
        video = Path(fresh.path)
        result.destination = str(destination)
        already_there = Path(destination).resolve() == video.parent.resolve()
        if not already_there and not await path_exists(video):
            return TaskResult(success=False, result=result, error=f"源文件已不在: {fresh.path}")

        outcome = await transfer_scrape_result(
            video,
            destination,
            library.subtitle_extensions,
            keep_dir=Path(library.path),
            keep_source_dirs=library.keep_source_dirs,
            stop_dirs=configured_roots(library),
            sidecars_only=already_there,
            sidecar_dir=Path(payload.source_dir) if payload.source_dir else None,
        )
        if outcome.video_path != video:
            await self._repo.update_media_file(fresh.id, path=str(outcome.video_path))
            # 字幕跟着搬过去了, 角标要按新目录重算; 清单里的旧路径也已失效.
            await refresh_external_subtitle(self._repo, fresh, library)
            if self._inventory_store is not None and library.id is not None:
                self._inventory_store.drop_library(library.id)
        elif already_there and outcome.files == 0 and not outcome.failed:
            result.note = "文件已在目的目录, 未搬动"

        result.files = outcome.files
        result.bytes = outcome.bytes
        result.failed = len(outcome.failed)
        # 成品刚搬进库根: 与整理同口径, 让媒体服务器重扫一次.
        refresh = [
            *(self._organize_followups(library, fresh, destination) if outcome.video_path != video else []),
            *media_refresh_followups(self._hot, library, changed=outcome.files, reason="transfer"),
            *wash_scan_followups(self._hot, library, changed=outcome.files),
        ]
        if outcome.failed:
            return TaskResult(
                success=False,
                result=result,
                error=f"有 {len(outcome.failed)} 个文件未搬动: {', '.join(outcome.failed)}",
                followups=refresh,
            )
        return TaskResult(success=True, result=result, followups=refresh)
