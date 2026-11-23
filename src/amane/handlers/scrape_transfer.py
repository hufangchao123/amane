"""SCRAPE_TRANSFER: 刮削结束后把该文件搬到对应的目的目录.

由 ``ScrapeHandler`` 扇出: 成功时随完成事务写链, 失败时由 worker 在判失败后单独入队.
落点由 ``library.transfer.scrape_transfer_destination`` 判定: 成功侧取成功目录 (没配但填了输入
目录则落库根), 失败侧取失败目录; 两边都可能没有落点, 那说明文件已经在它该在的地方, 只记一条说明.

搬运与整理 / 删除 / 洗版共用 ``LibraryTaskLocks``. 搬走的是视频与它同前缀的附属文件, 库内记录改指
新路径, 因此文件仍在库里被跟踪 (可再次刮削、可再整理).
"""

from pathlib import Path
from typing import TYPE_CHECKING

from ..library.transfer import scrape_transfer_destination, transfer_scrape_result
from ..utils.threads import path_exists
from ._common import media_refresh_followups, refresh_external_subtitle, wash_scan_followups
from .models import ScrapeTransferPayload, ScrapeTransferResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config import HotSettings
    from ..db.repository import Repository
    from ..library import InventoryStore
    from ._common import LibraryTaskLocks


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
        file = await self._repo.get_media_file(payload.media_file_id)
        if file is None or file.id is None:
            return TaskResult(success=False, error=f"媒体文件 {payload.media_file_id} 不存在")
        library = await self._repo.get_library(file.library_id)
        if library is None:
            return TaskResult(success=False, error=f"媒体库 {file.library_id} 不存在")

        result = ScrapeTransferResult(outcome=payload.outcome)
        if not library.scrape_transfer:
            result.note = "该媒体库未开启刮削后转移"
            return TaskResult(success=True, result=result)
        destination = scrape_transfer_destination(library, payload.outcome)
        if not destination:
            result.note = "文件已在原处, 无需转移"
            return TaskResult(success=True, result=result)

        video = Path(file.path)
        if not await path_exists(video):
            result.destination = destination
            return TaskResult(success=False, result=result, error=f"源文件已不在: {file.path}")

        if Path(destination).resolve() == video.parent.resolve():
            result.note = "文件已在目的目录, 未搬动"
            return TaskResult(success=True, result=result)

        if library.id is None:
            return TaskResult(success=False, error="媒体库尚未落库")
        async with await self._library_locks.get(library.id):
            outcome = await transfer_scrape_result(
                video,
                Path(destination),
                library.subtitle_extensions,
                keep_dir=Path(library.path),
                keep_source_dirs=library.keep_source_dirs,
            )
            if outcome.video_path != video:
                await self._repo.update_media_file(file.id, path=str(outcome.video_path))
                # 字幕跟着搬过去了, 角标要按新目录重算; 清单里的旧路径也已失效.
                await refresh_external_subtitle(self._repo, file, library)
                if self._inventory_store is not None and library.id is not None:
                    self._inventory_store.drop_library(library.id)

        result.destination = str(outcome.destination)
        result.files = outcome.files
        result.bytes = outcome.bytes
        result.failed = len(outcome.failed)
        # 成品刚搬进库根: 与整理同口径, 让媒体服务器重扫一次.
        refresh = [
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
