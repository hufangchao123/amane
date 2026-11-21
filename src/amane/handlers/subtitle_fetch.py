"""``SUBTITLE_FETCH`` 的执行编排: 选目标 → 抓取 → 交给 ``organize`` 落位.

本模块只做编排: 抓取在 ``subtitles`` 包, 落位在 ``organize.subtitles``. 自己拼库内路径会在
库模板改过之后悄悄写错地方, 自己删文件又会绕开回收站 —— 这两件事都不在这里做.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from ..db.models import MediaFileStatus
from ..enums import MoveMode
from ..library.rules import has_companion_subtitle, is_video_media
from ..organize.path_templates import resolve_subtitle_path
from ..organize.subtitles import place_subtitles
from ..parsing.file_info import parse_file_info
from ..subtitles import (
    SubtitleSourceError,
    build_web_client,
    fetch_subtitle,
    staged_name,
    staging_dir,
    usable_providers,
)
from ._common import LibraryTaskLocks, refresh_external_subtitle
from .models import SubtitleFetchPayload, SubtitleFetchResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config.manager import HotSettings, SubtitleFetchConfig
    from ..db.models import Library, MediaFile, Metadata
    from ..db.repository import Repository
    from ..subtitles import SubtitleDownload, SubtitleHttp

logger = structlog.get_logger()

MAX_TARGETS_PER_RUN: Final[int] = 50
"""单轮最多补几份. 来源有限流, 一次挑太多会让任务跑很久, 也让失败重试变得难以界定."""


class _Outcome(StrEnum):
    PLACED = "placed"
    """抓到了并落位成功."""
    SKIPPED = "skipped"
    """本来就不用补: 已有外挂字幕、非视频、库关了字幕发现."""
    MISSING = "missing"
    """来源没有这份字幕: 记为缺口, 不是故障."""
    FAILED = "failed"
    """出错: 落位失败、缺元数据、来源报错."""


@dataclass(frozen=True, slots=True)
class _TargetOutcome:
    outcome: _Outcome
    reason: str = ""


class _TargetError(Exception):
    """payload 指向的目标不存在: 整轮任务直接失败, 不要静默跑别的目标."""


class SubtitleFetchHandler(TaskHandler[SubtitleFetchPayload, SubtitleFetchResult]):
    """按番号向字幕来源取字幕, 放置交给 ``organize.subtitles``; 已有外挂字幕的文件跳过."""

    def __init__(
        self,
        repo: Repository,
        hot: HotSettings,
        library_locks: LibraryTaskLocks | None = None,
        web: SubtitleHttp | None = None,
    ) -> None:
        super().__init__(payload_t=SubtitleFetchPayload, result_t=SubtitleFetchResult)
        self._repo = repo
        self._hot = hot
        self._library_locks = library_locks if library_locks is not None else LibraryTaskLocks()
        """落位要写库目录, 与 ORGANIZE / DELETE / 洗版共用同库锁, 否则会与移动重命名并发."""
        self._web = web
        """构造函数注入的通道只为用例与将来的复用; 生产走 ``build_web_client``."""

    async def handle(self, payload: SubtitleFetchPayload) -> TaskResult[SubtitleFetchResult]:
        config = self._hot.subtitles
        if not config.enabled:
            return TaskResult(success=False, error="字幕补全未启用")
        if not usable_providers(config):
            return TaskResult(success=False, error="没有可用的字幕来源: 请启用来源或填写 assrt token")

        try:
            targets = await self._select_targets(payload)
        except _TargetError as exc:
            return TaskResult(success=False, error=str(exc))
        if not targets:
            return TaskResult(success=True, result=SubtitleFetchResult())

        web = self._web
        owned = None
        outcomes: list[_TargetOutcome] = []
        try:
            if web is None:
                web = owned = build_web_client(self._hot.network)
            total = len(targets)
            for index, media in enumerate(targets, start=1):
                await self.report_progress(index - 1, total, f"字幕补全 {index}/{total}")
                outcomes.append(await self._process(web, config, media))
            await self.report_progress(total, total, "字幕补全完成")
        finally:
            if owned is not None:
                await owned.close()

        return TaskResult(success=True, result=self._summarize(outcomes))

    def _summarize(self, outcomes: list[_TargetOutcome]) -> SubtitleFetchResult:
        placed = sum(1 for item in outcomes if item.outcome is _Outcome.PLACED)
        skipped = sum(1 for item in outcomes if item.outcome is _Outcome.SKIPPED)
        missing = sum(1 for item in outcomes if item.outcome is _Outcome.MISSING)
        failed = sum(1 for item in outcomes if item.outcome is _Outcome.FAILED)
        if missing or failed:
            # 结果里只带计数, 明细进日志: 任务结果会被路由原样返回, 不该塞一屏来源报错.
            reasons = [
                f"{item.outcome}: {item.reason}"
                for item in outcomes
                if item.outcome in {_Outcome.MISSING, _Outcome.FAILED}
            ]
            logger.info("subtitles.finished", placed=placed, skipped=skipped, missing=missing, failed=failed)
            logger.debug("subtitles.details", reasons=reasons)
        return SubtitleFetchResult(fetched=placed, missing=missing, failed=failed)

    async def _select_targets(self, payload: SubtitleFetchPayload) -> list[MediaFile]:
        """选择本轮要补的文件: 显式指定优先, 否则全库按缺口挑."""
        if payload.media_file_id is not None:
            media = await self._repo.get_media_file(payload.media_file_id)
            if media is None:
                raise _TargetError(f"媒体文件不存在: {payload.media_file_id}")
            return [media]
        if payload.library_id is not None:
            if await self._repo.get_library(payload.library_id) is None:
                raise _TargetError(f"媒体库不存在: {payload.library_id}")
            return await self._repo.list_media_files(
                status=[MediaFileStatus.SCRAPED],
                library_id=payload.library_id,
                has_subtitle=False,
                limit=MAX_TARGETS_PER_RUN,
            )
        return await self._repo.list_media_files(
            status=[MediaFileStatus.SCRAPED],
            has_subtitle=False,
            limit=MAX_TARGETS_PER_RUN,
        )

    async def _process(
        self,
        web: SubtitleHttp,
        config: SubtitleFetchConfig,
        media: MediaFile,
    ) -> _TargetOutcome:
        path = Path(media.path)
        if not is_video_media(path):
            return _TargetOutcome(_Outcome.SKIPPED, "不是视频文件")

        library = await self._repo.get_library(media.library_id)
        if library is None:
            return _TargetOutcome(_Outcome.FAILED, "媒体库不存在")
        extensions = list(library.subtitle_extensions)
        if not extensions:
            return _TargetOutcome(_Outcome.SKIPPED, "媒体库关闭了字幕发现")
        if await has_companion_subtitle(path, extensions):
            # 库内的标记可能过期 (整理后没回扫), 顺手纠正一次, 免得状态卡一直报缺口.
            await refresh_external_subtitle(self._repo, media, library)
            return _TargetOutcome(_Outcome.SKIPPED, "已有外挂字幕")

        number = await self._number_of(media)
        if not number:
            return _TargetOutcome(_Outcome.FAILED, "文件名里没有可用番号")
        metadata = await self._metadata_for(media, number)
        if metadata is None:
            return _TargetOutcome(_Outcome.FAILED, "缺少元数据, 无法按模板落位")

        try:
            download = await fetch_subtitle(web, config, number)
        except SubtitleSourceError as exc:
            logger.info("subtitles.missing", media_file_id=media.id, number=number, reason=str(exc))
            return _TargetOutcome(_Outcome.MISSING, str(exc))
        return await self._place(media, library, metadata, download)

    async def _number_of(self, media: MediaFile) -> str:
        if media.number:
            return media.number.strip()
        info = await asyncio.to_thread(parse_file_info, media.path)
        return (info.number or "").strip()

    async def _metadata_for(self, media: MediaFile, number: str) -> Metadata | None:
        if media.metadata_id is not None:
            found = await self._repo.get_metadata(media.metadata_id)
            if found is not None:
                return found
        return await self._repo.get_metadata_by_number(number)

    async def _place(
        self,
        media: MediaFile,
        library: Library,
        metadata: Metadata,
        download: SubtitleDownload,
    ) -> _TargetOutcome:
        """把下载到的正文先写成临时文件, 再交给 ``place_subtitles`` 决定落位与命名.

        临时文件叫 ``<视频 stem>.zh<suffix>``: 默认字幕模板是 ``{raw_srt_name}.{ext}``, 临时文件
        的 stem 会直接成为成品名的主干, 语言标记只能在这一步加进去.
        """
        path = Path(media.path)
        if library.id is None:
            return _TargetOutcome(_Outcome.FAILED, "媒体库尚未落库")
        lock = await self._library_locks.get(library.id)
        async with lock:
            return await self._place_locked(media, library, metadata, download, path)

    async def _place_locked(
        self,
        media: MediaFile,
        library: Library,
        metadata: Metadata,
        download: SubtitleDownload,
        path: Path,
    ) -> _TargetOutcome:
        directory = await asyncio.to_thread(staging_dir)
        try:
            staged = directory / staged_name(path.stem, download.suffix)
            await asyncio.to_thread(staged.write_bytes, download.content)
            file_info = await asyncio.to_thread(
                parse_file_info,
                media.path,
                has_external_subtitle=media.has_external_subtitle,
            )
            expected = resolve_subtitle_path(
                library,
                metadata,
                staged,
                video_dir=path.parent,
                video_dest=path,
                source_path=path,
                file_info=file_info,
                safe_dirs=(),
            )
            conflicts = await place_subtitles(
                [staged],
                video_source=path,
                video_dest=path,
                library=library,
                metadata=metadata,
                file_info=file_info,
                # 临时文件必须搬走, 不能按库的 move_mode 变成硬链/软链: 临时目录随后会被删.
                mode=MoveMode.MOVE,
            )
            placed = not conflicts and await asyncio.to_thread(expected.exists)
        except OSError as exc:
            logger.warning("subtitles.place_failed", media_file_id=media.id, error=str(exc))
            return _TargetOutcome(_Outcome.FAILED, f"落位失败: {exc}")
        finally:
            await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)

        if not placed:
            return _TargetOutcome(_Outcome.FAILED, "目标已存在同名字幕或落位失败, 未覆盖")
        await refresh_external_subtitle(self._repo, media, library)
        logger.info(
            "subtitles.placed",
            media_file_id=media.id,
            provider=download.provider,
            description=download.description,
        )
        return _TargetOutcome(_Outcome.PLACED, download.description)
