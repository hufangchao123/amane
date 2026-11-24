"""缺字幕统计与按番号抓字的编排.

分成两半: ``fetch_subtitle`` 是纯抓取 (给定番号, 依次问各家来源), ``SubtitleFetcher`` 是
面向 API 的只读入口 (缺口数、排队数、是否可用). 放置与库内写操作都不在这里 —— 那是
``organize.subtitles`` 与 handler 的事, 本包只"取回文件".
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel

from ..db.models import MediaFileStatus, TaskStatus, TaskType
from . import avsubtitles, subtitlecat
from .models import SubtitleDownload, SubtitleSourceError
from .targets import GapKind, classify_gap
from .transport import SubtitleHttp

if TYPE_CHECKING:
    from ..config.manager import SubtitleFetchConfig, SubtitleProvider
    from ..db.repository import Repository

_RETRY_BACKOFF_SECONDS: Final[float] = 0.5
"""两次重试之间的等待: 来源多是限流或瞬时断连, 立刻重发只会再撞一次."""


class SubtitleFetchSummary(BaseModel):
    missing: int = 0
    """库内成片没有外挂字幕的份数 (不含分卷)."""
    multi_disc: int = 0
    """分卷 (``cd`` 非空) 里没有外挂字幕的份数: 整片字幕与拆开的文件对不齐, 不算缺口也不抓."""
    pointer: int = 0
    """指针文件 (``.strm``) 没有外挂字幕的份数: 本地没有音轨, 转写跳过, 只能手动上传."""
    queued: int = 0
    """排队中的 ``SUBTITLE_FETCH`` 任务数."""


def usable_providers(config: SubtitleFetchConfig) -> tuple[SubtitleProvider, ...]:
    """配置里勾选且去重后的来源顺序.

    两个来源都不需要凭据, 因此"能用"就等于"勾了"。保留这个函数是为了把"启用"与"有来源"
    分开判定 —— 勾了来源但总开关关着时, 整轮任务不该跑。
    """
    providers: list[SubtitleProvider] = []
    for provider in config.providers:
        if provider not in providers:
            providers.append(provider)
    return tuple(providers)


async def fetch_subtitle(
    web: SubtitleHttp,
    config: SubtitleFetchConfig,
    number: str,
    *,
    preferred_suffixes: Sequence[str] = (),
) -> SubtitleDownload:
    """按配置顺序问各家来源, 拿到第一份就返回.

    每个来源最多 ``max_attempts`` 次, 且只重试临时失败 (限流/断连); 确定性失败 (未搜到、
    下载地址被拒) 立刻换下一家 —— 同一个来源重试只会得到同一个答案.
    """
    providers = usable_providers(config)
    if not providers:
        raise SubtitleSourceError("字幕来源未启用", transient=False)

    attempts = max(1, config.max_attempts)
    errors: list[str] = []
    transient = True
    for provider in providers:
        for attempt in range(attempts):
            try:
                return await _fetch_from(web, provider, number, config, preferred_suffixes=preferred_suffixes)
            except SubtitleSourceError as exc:
                if exc.transient and attempt + 1 < attempts:
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                errors.append(f"{provider}: {exc}")
                # 整轮是否值得重试, 取所有已记失败的合取: 只要有一次是确定性失败,
                # 再跑一遍也不会变好.
                transient = transient and exc.transient
                break
    message = "; ".join(errors) if errors else f"没有来源给出 {number}"
    raise SubtitleSourceError(message, transient=bool(errors) and transient)


async def _fetch_from(
    web: SubtitleHttp,
    provider: SubtitleProvider,
    number: str,
    config: SubtitleFetchConfig,
    *,
    preferred_suffixes: Sequence[str] = (),
) -> SubtitleDownload:
    if provider == "avsubtitles":
        return await avsubtitles.fetch(web, number, languages=config.languages, preferred_suffixes=preferred_suffixes)
    return await subtitlecat.fetch(web, number, languages=config.languages, preferred_suffixes=preferred_suffixes)


class SubtitleFetcher:
    """字幕补全的编排入口: 统计缺口与排队, 抓取与放置都在任务里.

    放置一律走 ``organize.subtitles``, 本包只负责从来源取回文件.
    """

    def __init__(self, repo: Repository, config: SubtitleFetchConfig) -> None:
        self._repo = repo
        self._config = config

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled and usable_providers(self._config))

    @property
    def config(self) -> SubtitleFetchConfig:
        """任务侧要按同一份配置抓取, 免得路由与 handler 各读一次配置出现偏差."""
        return self._config

    async def summary(self) -> SubtitleFetchSummary:
        """缺口按"已刮削但没有任何字幕"计: 未刮削的条目连番号都未必有, 抓也抓不到."""
        gaps = await self._repo.list_media_files(status=[MediaFileStatus.SCRAPED], has_subtitle=False, limit=None)
        kinds = [classify_gap(media.path) for media in gaps]
        multi_disc = sum(1 for kind in kinds if kind is GapKind.MULTI_DISC)
        pointer = sum(1 for kind in kinds if kind is GapKind.POINTER)
        missing = sum(1 for kind in kinds if kind is GapKind.MISSING)
        queued = await self._repo.count_tasks(
            statuses=[TaskStatus.QUEUED, TaskStatus.RUNNING],
            task_types=[TaskType.SUBTITLE_FETCH],
        )
        return SubtitleFetchSummary(missing=missing, multi_disc=multi_disc, pointer=pointer, queued=queued)
