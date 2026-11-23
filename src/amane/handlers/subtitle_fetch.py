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
from ..library.rules import has_companion_subtitle, is_under_any, is_video_media, publish_roots
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
from ._common import LibraryTaskLocks, media_refresh_followups, refresh_external_subtitle
from .models import SubtitleFetchPayload, SubtitleFetchResult
from .protocol import FollowupTask, TaskHandler, TaskResult

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
    """出错: 落位失败、缺元数据、来源临时故障."""


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

        # success 表示「这一轮跑完了」: 单份取不到的计数在结果里, 由页面显示, 不把整轮标失败.
        result = self._summarize(outcomes)
        return TaskResult(success=True, result=result, followups=await self._refresh_followups(payload, result))

    async def _refresh_followups(
        self, payload: SubtitleFetchPayload, result: SubtitleFetchResult
    ) -> list[FollowupTask]:
        """补到字幕的文件要出现在媒体服务器里; 落了字幕后 Emby 不会自己重读目录."""
        if result.fetched == 0:
            return []
        library_id = payload.library_id
        if library_id is None and payload.media_file_id is not None:
            media = await self._repo.get_media_file(payload.media_file_id)
            library_id = media.library_id if media is not None else None
        if library_id is None:
            return []
        library = await self._repo.get_library(library_id)
        if library is None:
            return []
        return media_refresh_followups(self._hot, library, changed=result.fetched, reason="subtitles")

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
            download = await fetch_subtitle(web, config, number, preferred_suffixes=tuple(extensions))
        except SubtitleSourceError as exc:
            if exc.transient:
                # 限流与断连说明"这一轮没取到", 不说明"来源没有这份字幕": 记成缺口会让人以为
                # 补全跑完了, 实际下一轮重跑还能补上, 因此单独计入 failed.
                logger.warning("subtitles.source_failed", media_file_id=media.id, number=number, reason=str(exc))
                return _TargetOutcome(_Outcome.FAILED, str(exc))
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
        if library.id is None:
            return _TargetOutcome(_Outcome.FAILED, "媒体库尚未落库")
        lock = await self._library_locks.get(library.id)
        async with lock:
            # 拿锁之后再读一次路径: 转移与整理会动同一个文件, 用进入任务时的旧路径会往
            # 已经不存在的位置落字幕.
            fresh = await self._repo.get_media_file(media.id) if media.id is not None else None
            current = fresh or media
            return await self._place_locked(current, library, metadata, download, Path(current.path))

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
            # 成品目录之外的文件(还在输入目录里)按同一套模板算不出位置: 默认模板用绝对路径
            # ``{link_dir}``, 基准目录只有库根, 于是报「路径越界」. 这类文件的字幕先落在它
            # 自己旁边, 随文件一起被转移与整理带走.
            in_library = is_under_any(path, publish_roots(path=library.path, ok_dir=library.scrape_ok_dir))
            safe_dirs: tuple[Path, ...] = () if in_library else (path.parent,)
            expected = resolve_subtitle_path(
                library,
                metadata,
                staged,
                video_dir=path.parent,
                video_dest=path,
                source_path=path,
                file_info=file_info,
                safe_dirs=safe_dirs,
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
                safe_dirs=safe_dirs,
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
        if not media.has_external_subtitle:
            # 落位的扩展名不在库的 subtitle_extensions 里: 文件在磁盘上但库不认, 下一轮还会重抓同一份.
            return _TargetOutcome(
                _Outcome.FAILED,
                f"库的字幕扩展名不含 {download.suffix}, 已落位但不会被识别",
            )
        logger.info(
            "subtitles.placed",
            media_file_id=media.id,
            provider=download.provider,
            description=download.description,
        )
        return _TargetOutcome(_Outcome.PLACED, download.description)
