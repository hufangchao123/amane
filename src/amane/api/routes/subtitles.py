"""字幕补全的状态读取; 抓取是 ``SUBTITLE_FETCH`` 任务."""

from fastapi import APIRouter

from ...subtitles import SubtitleFetcher
from ..deps import ConfigDep, RepoDep
from ..models.subtitles import SubtitleStatusResponse

router = APIRouter(prefix="/subtitles", tags=["subtitles"])


@router.get("/status", response_model=SubtitleStatusResponse)
async def subtitle_status(config: ConfigDep, repo: RepoDep) -> SubtitleStatusResponse:
    """只读: 是否可用、缺口数与排队数.

    缺口统计是两条 count 查询; 前端每次进页面都会问一次, 因此这里不做任何磁盘遍历 ——
    "哪些文件真的没字幕"由任务自己按库扫描时判断.
    """
    fetcher = SubtitleFetcher(repo, config.hot.subtitles)
    summary = await fetcher.summary()
    return SubtitleStatusResponse(enabled=fetcher.enabled, **summary.model_dump())
