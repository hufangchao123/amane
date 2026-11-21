from typing import TYPE_CHECKING

from .models import SubtitleFetchPayload, SubtitleFetchResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config.manager import HotSettings
    from ..db.repository import Repository


class SubtitleFetchHandler(TaskHandler[SubtitleFetchPayload, SubtitleFetchResult]):
    """按番号向字幕来源取字幕, 放置交给 ``organize.subtitles``; 已有外挂字幕的文件跳过."""

    def __init__(self, repo: Repository, hot: HotSettings) -> None:
        super().__init__(payload_t=SubtitleFetchPayload, result_t=SubtitleFetchResult)
        self._repo = repo
        self._hot = hot

    async def handle(self, payload: SubtitleFetchPayload) -> TaskResult[SubtitleFetchResult]:
        # TODO(phase1): 见 amane-merge/notes/subtitle.md
        return TaskResult(success=False, error="字幕补全尚未实现")
