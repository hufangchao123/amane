"""字幕巡检: 统计缺外挂字幕的文件, 并列出配不上视频的字幕.

只读: 不移动文件, 不写库, 不发网络. 清理由回收站运维另行处理 —— 巡检给出路径, 不替人决定删除.
"""

from typing import TYPE_CHECKING

from ..db import TaskType
from ..db.models import MediaFileStatus
from ..subtitles.audit import SubtitleFinding, scan_library
from ..subtitles.targets import is_multi_disc
from .models import SubtitleAuditPayload, SubtitleAuditResult, SubtitleFetchPayload
from .protocol import FollowupTask, TaskHandler, TaskResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..config import HotSettings
    from ..db.models import Library
    from ..db.repository import Repository


class SubtitleAuditHandler(TaskHandler[SubtitleAuditPayload, SubtitleAuditResult]):
    """遍历库内已入库的文件, 逐目录比对视频与字幕.

    只读, 因此不取 ``LibraryTaskLocks``; 列出的是磁盘现状, 与库内 ``has_subtitle`` 标记可能不同步 ——
    ``missing`` 取自库里已有的标记, ``orphans`` 取自磁盘, 两者不互相校正.
    """

    def __init__(self, repo: Repository, hot: HotSettings) -> None:
        super().__init__(payload_t=SubtitleAuditPayload, result_t=SubtitleAuditResult)
        self._repo = repo
        self._hot = hot

    async def handle(self, payload: SubtitleAuditPayload) -> TaskResult[SubtitleAuditResult]:
        if payload.library_id is not None and await self._repo.get_library(payload.library_id) is None:
            return TaskResult(success=False, error=f"媒体库 {payload.library_id} 不存在")

        libraries = await self._repo.list_libraries()
        if payload.library_id is not None:
            libraries = [library for library in libraries if library.id == payload.library_id]

        videos = 0
        subtitles = 0
        missing = 0
        multi_disc = 0
        findings: list[SubtitleFinding] = []
        for index, library in enumerate(libraries, start=1):
            await self.report_progress(index - 1, len(libraries) + 1, library.name)
            files = await self._repo.list_media_files(library_id=library.id, limit=None)
            scan = await scan_library([file.path for file in files], library.subtitle_extensions)
            videos += scan.videos
            subtitles += scan.subtitles
            findings.extend(scan.findings)
            # 只算已刮削的行: 未刮削与失败的行本来就不在补全目标里.
            for file in files:
                if file.status is not MediaFileStatus.SCRAPED or file.has_subtitle:
                    continue
                if is_multi_disc(file.path):
                    multi_disc += 1
                else:
                    missing += 1

        await self.report_progress(len(libraries) + 1, len(libraries) + 1, "done")
        return TaskResult(
            success=True,
            result=SubtitleAuditResult(
                libraries=len(libraries),
                videos=videos,
                subtitles=subtitles,
                missing=missing,
                multi_disc=multi_disc,
                orphans=len(findings),
                samples=[finding.path for finding in findings[:20]],
            ),
            followups=self._fetch_followups(libraries, missing),
        )

    def _fetch_followups(self, libraries: Sequence[Library], missing: int) -> list[FollowupTask]:
        """巡检算出缺口就顺手排补全: 否则缺口数字永远要人手点一次才会动.

        只在字幕补全启用、且巡检范围是单库时排队 —— 多库巡检不知道哪一库缺得多, 逐库排一轮
        会把额度一次花光.
        """
        if missing == 0 or not self._hot.subtitles.enabled or len(libraries) != 1:
            return []
        library = libraries[0]
        return [
            FollowupTask(
                key=f"subtitle-fetch:{library.id}",
                task_type=TaskType.SUBTITLE_FETCH,
                payload=SubtitleFetchPayload(library_id=library.id, limit=200).model_dump(mode="json"),
                priority=-1,
            )
        ]
