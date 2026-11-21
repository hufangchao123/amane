"""字幕来源抓取与"缺字幕"统计.

本包只负责从来源取回字幕文件; 放置一律交给 ``organize.subtitles``, 库内路径不在这里拼.
"""

from .content import staged_name
from .fetch import SubtitleFetcher, SubtitleFetchSummary, fetch_subtitle, usable_providers
from .models import SubtitleDownload, SubtitleSourceError
from .transport import SubtitleHttp, build_web_client, staging_dir

__all__ = [
    "SubtitleDownload",
    "SubtitleFetchSummary",
    "SubtitleFetcher",
    "SubtitleHttp",
    "SubtitleSourceError",
    "build_web_client",
    "fetch_subtitle",
    "staged_name",
    "staging_dir",
    "usable_providers",
]
