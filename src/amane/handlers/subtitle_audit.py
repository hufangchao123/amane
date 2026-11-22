"""字幕巡检: 统计缺外挂字幕的文件, 并列出配不上视频的字幕.

只读: 不移动文件, 不写库, 不发网络. 清理由回收站运维另行处理 —— 巡检给出路径, 不替人决定删除.
"""

from typing import TYPE_CHECKING

from ..subtitles.audit import SubtitleFinding, scan_library
from .models import SubtitleAuditPayload, SubtitleAuditResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..db.repository import Repository


class SubtitleAuditHandler(TaskHandler[SubtitleAuditPayload, SubtitleAuditResult]):
    """遍历库内已入库的文件, 逐目录比对视频与字幕.

    只读, 因此不取 ``LibraryTaskLocks``; 列出的是磁盘现状, 与库内 ``has_subtitle`` 标记可能不同步 ——
    ``missing`` 取自库里已有的标记, ``orphans`` 取自磁盘, 两者不互相校正.
    """

    def __init__(self, repo: Repository) -> None:
        super().__init__(payload_t=SubtitleAuditPayload, result_t=SubtitleAuditResult)
        self._repo = repo

    async def handle(self, payload: SubtitleAuditPayload) -> TaskResult[SubtitleAuditResult]:
        if payload.library_id is not None and await self._repo.get_library(payload.library_id) is None:
            return TaskResult(success=False, error=f"媒体库 {payload.library_id} 不存在")

        libraries = await self._repo.list_libraries()
        if payload.library_id is not None:
            libraries = [library for library in libraries if library.id == payload.library_id]

        videos = 0
        subtitles = 0
        missing = 0
        findings: list[SubtitleFinding] = []
        for index, library in enumerate(libraries, start=1):
            await self.report_progress(index - 1, len(libraries) + 1, library.name)
            files = await self._repo.list_media_files(library_id=library.id, limit=None)
            scan = await scan_library([file.path for file in files], library.subtitle_extensions)
            videos += scan.videos
            subtitles += scan.subtitles
            findings.extend(scan.findings)
            missing += await self._repo.count_media_files(library_id=library.id, has_subtitle=False)

        await self.report_progress(len(libraries) + 1, len(libraries) + 1, "done")
        return TaskResult(
            success=True,
            result=SubtitleAuditResult(
                libraries=len(libraries),
                videos=videos,
                subtitles=subtitles,
                missing=missing,
                orphans=len(findings),
                samples=[finding.path for finding in findings[:20]],
            ),
        )
