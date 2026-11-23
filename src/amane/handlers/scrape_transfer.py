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
from .models import ScrapeTransferPayload, ScrapeTransferResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..db.repository import Repository
    from ._common import LibraryTaskLocks


class ScrapeTransferHandler(TaskHandler[ScrapeTransferPayload, ScrapeTransferResult]):
    """单文件转移; 一个任务只处理一次刮削的那一个文件."""

    def __init__(self, repo: Repository, library_locks: LibraryTaskLocks) -> None:
        super().__init__(payload_t=ScrapeTransferPayload, result_t=ScrapeTransferResult)
        self._repo = repo
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

        result.destination = str(outcome.destination)
        result.files = outcome.files
        result.bytes = outcome.bytes
        result.failed = len(outcome.failed)
        if outcome.failed:
            return TaskResult(
                success=False,
                result=result,
                error=f"有 {len(outcome.failed)} 个文件未搬动: {', '.join(outcome.failed)}",
            )
        return TaskResult(success=True, result=result)
