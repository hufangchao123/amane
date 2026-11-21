"""字幕补全的状态读取; 抓取是 ``SUBTITLE_FETCH`` 任务."""

from fastapi import APIRouter

from ...subtitles import SubtitleFetcher
from ..deps import ConfigDep
from ..models.subtitles import SubtitleStatusResponse

router = APIRouter(prefix="/subtitles", tags=["subtitles"])


@router.get("/status", response_model=SubtitleStatusResponse)
async def subtitle_status(config: ConfigDep) -> SubtitleStatusResponse:
    fetcher = SubtitleFetcher(config.hot.subtitles)
    summary = await fetcher.summary()
    return SubtitleStatusResponse(enabled=fetcher.enabled, **summary.model_dump())
