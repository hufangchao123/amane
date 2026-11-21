from pydantic import BaseModel

from ..config.manager import SubtitleFetchConfig


class SubtitleFetchSummary(BaseModel):
    missing: int = 0
    """库内成片没有外挂字幕的份数."""
    queued: int = 0
    """排队中的 ``SUBTITLE_FETCH`` 任务数."""


class SubtitleFetcher:
    """字幕补全的编排入口: 统计缺口与排队, 抓取与放置都在任务里.

    放置一律走 ``organize.subtitles``, 本包只负责从来源取回文件.
    """

    def __init__(self, config: SubtitleFetchConfig) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled and self._config.providers)

    async def summary(self) -> SubtitleFetchSummary:
        # TODO(phase1): media_files.has_external_subtitle 计数 + tasks 排队数
        return SubtitleFetchSummary()
