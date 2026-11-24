"""``SUBTITLE_GENERATE``: 外部站都取不到字幕时, 用 Whisper 转写音轨再译成中文.

转写引擎在外部服务上 (镜像里只有 ffmpeg), 本模块负责: 选目标 → 抽音轨 → 转写 → 翻译 →
按视频位置落一份 ``.zh.srt`` → 刷新媒体库. 生成的字幕带机器标记, 与人工作品区分开.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
import structlog
from aiolimiter import AsyncLimiter

from ..db.models import MediaFileStatus
from ..library.rules import has_companion_subtitle, is_pointer_media, is_video_media
from ..llm.model import build_model
from ..llm.translator import build_model_http_client
from ..subtitles.targets import is_multi_disc
from ..subtitles.translate import TranslationError, TranslationOutcome, translate_lines
from ..subtitles.transport import staging_dir
from ..subtitles.whisper import (
    WhisperError,
    extract_audio,
    ffmpeg_path,
    format_timestamp,
    split_long_segments,
    srt_lines,
    to_srt,
    transcribe,
    with_texts,
)
from ._common import LibraryTaskLocks, media_refresh_followups, refresh_external_subtitle
from .models import SubtitleGeneratePayload, SubtitleGenerateResult
from .protocol import FollowupTask, TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config.manager import HotSettings
    from ..db.models import Library, MediaFile
    from ..db.repository import Repository

logger = structlog.get_logger()

MACHINE_MARK = "机翻"
"""写在字幕首行: ``subtitles/models.py`` 认这个标记, 界面与来源排序据此区分机翻."""


class _Outcome(StrEnum):
    GENERATED = "generated"
    """转写并落位成功."""
    SKIPPED = "skipped"
    """没有可做的事: 已有字幕、分卷、非视频、超时长、库关了字幕发现."""
    FAILED = "failed"
    """出错: 抽轨、转写、翻译或写盘失败."""


@dataclass(frozen=True, slots=True)
class _TargetOutcome:
    outcome: _Outcome
    reason: str = ""
    translated: bool = False


class _TargetError(Exception):
    """payload 指向的目标不存在: 整轮直接失败, 不静默换别的目标."""


class SubtitleGenerateHandler(TaskHandler[SubtitleGeneratePayload, SubtitleGenerateResult]):
    """转写生成字幕; 每份文件独立成败, 单份出错不废整轮."""

    def __init__(
        self,
        repo: Repository,
        hot: HotSettings,
        library_locks: LibraryTaskLocks | None = None,
    ) -> None:
        super().__init__(payload_t=SubtitleGeneratePayload, result_t=SubtitleGenerateResult)
        self._repo = repo
        self._hot = hot
        self._library_locks = library_locks if library_locks is not None else LibraryTaskLocks()

    async def handle(self, payload: SubtitleGeneratePayload) -> TaskResult[SubtitleGenerateResult]:
        config = self._hot.subtitles
        generate = config.generation
        if not generate.enabled:
            return TaskResult(success=False, error="转写生成字幕未启用")
        if not generate.base_url.strip():
            return TaskResult(success=False, error="没有配置转写端点")
        if ffmpeg_path() is None:
            # 镜像缺 ffmpeg 时每份文件都会失败一次, 直接说清楚比刷一屏日志有用.
            return TaskResult(success=False, error="镜像里没有 ffmpeg, 无法抽取音轨")
        if generate.translate and (not self._hot.llm.enabled or not self._hot.llm.api_key):
            return TaskResult(success=False, error="要求翻译, 但大模型未启用或缺 api_key")

        try:
            targets = await self._select_targets(payload)
        except _TargetError as exc:
            return TaskResult(success=False, error=str(exc))
        if not targets:
            return TaskResult(success=True, result=SubtitleGenerateResult())

        outcomes: list[_TargetOutcome] = []
        async with httpx2.AsyncClient() as client:
            total = len(targets)
            for index, media in enumerate(targets, start=1):
                await self.report_progress(index - 1, total, f"转写字幕 {index}/{total}")
                try:
                    outcomes.append(await self._process(client, media))
                except Exception as exc:  # 单份出错只记一笔
                    logger.exception("subtitle generate failed", media_file_id=media.id)
                    outcomes.append(_TargetOutcome(_Outcome.FAILED, str(exc)))
            await self.report_progress(total, total, "转写完成")

        result = self._summarize(outcomes)
        return TaskResult(success=True, result=result, followups=await self._refresh_followups(payload, result))

    async def _refresh_followups(
        self, payload: SubtitleGeneratePayload, result: SubtitleGenerateResult
    ) -> list[FollowupTask]:
        if result.generated == 0:
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
        return media_refresh_followups(self._hot, library, changed=result.generated, reason="subtitles")

    def _summarize(self, outcomes: list[_TargetOutcome]) -> SubtitleGenerateResult:
        generated = sum(1 for item in outcomes if item.outcome is _Outcome.GENERATED)
        translated = sum(1 for item in outcomes if item.translated)
        skipped = sum(1 for item in outcomes if item.outcome is _Outcome.SKIPPED)
        failed = sum(1 for item in outcomes if item.outcome is _Outcome.FAILED)
        logger.info(
            "subtitle generate finished", generated=generated, translated=translated, skipped=skipped, failed=failed
        )
        return SubtitleGenerateResult(generated=generated, translated=translated, skipped=skipped, failed=failed)

    async def _select_targets(self, payload: SubtitleGeneratePayload) -> list[MediaFile]:
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
                limit=payload.limit,
            )
        return await self._repo.list_media_files(
            status=[MediaFileStatus.SCRAPED],
            has_subtitle=False,
            limit=payload.limit,
        )

    async def _process(self, client: httpx2.AsyncClient, media: MediaFile) -> _TargetOutcome:
        path = Path(media.path)
        if not is_video_media(path):
            return _TargetOutcome(_Outcome.SKIPPED, "不是视频文件")
        if is_multi_disc(path):
            return _TargetOutcome(_Outcome.SKIPPED, "分卷不补字幕")
        if is_pointer_media(path):
            return _TargetOutcome(_Outcome.SKIPPED, "指针文件没有音轨")
        library = await self._repo.get_library(media.library_id)
        if library is None:
            return _TargetOutcome(_Outcome.FAILED, "媒体库不存在")
        if not library.subtitle_extensions:
            return _TargetOutcome(_Outcome.SKIPPED, "媒体库关闭了字幕发现")
        if await has_companion_subtitle(path, list(library.subtitle_extensions)):
            await refresh_external_subtitle(self._repo, media, library)
            return _TargetOutcome(_Outcome.SKIPPED, "已有外挂字幕")

        generate = self._hot.subtitles.generation
        limit_seconds = generate.max_file_minutes * 60
        if media.duration is not None and media.duration > limit_seconds:
            return _TargetOutcome(_Outcome.SKIPPED, f"超过 {generate.max_file_minutes} 分钟")

        work = await asyncio.to_thread(_work_dir)
        try:
            audio = work / "audio.wav"
            await extract_audio(path, audio, timeout=float(generate.timeout_seconds))
            try:
                segments = await transcribe(
                    client,
                    base_url=generate.base_url,
                    api_key=generate.api_key,
                    model=generate.model,
                    language=generate.language,
                    audio=audio,
                    timeout=float(generate.timeout_seconds),
                )
            except WhisperError as exc:
                logger.warning("subtitle transcribe failed", media_file_id=media.id, reason=str(exc))
                return _TargetOutcome(_Outcome.FAILED, str(exc))
            # 端点可能在没台词的区间给出一条跨度几十分钟的段: 切开再拼, 免得一条字幕占半部片子.
            segments = split_long_segments(segments)
            translated = False
            if generate.translate:
                try:
                    outcome = await self._translate(srt_lines(segments))
                except TranslationError as exc:
                    return _TargetOutcome(_Outcome.FAILED, str(exc))
                if outcome.nothing_translated:
                    # 端点整体不可用: 落盘只会得到一份日文字幕, 记失败等网关恢复后重跑.
                    return _TargetOutcome(_Outcome.FAILED, "大模型端点没有可用账号, 译文没生成")
                if not outcome.complete:
                    logger.warning(
                        "subtitle translate partially fell back",
                        media_file_id=media.id,
                        failed_batches=outcome.failed_batches,
                        request_failures=outcome.request_failures,
                        total_batches=outcome.total_batches,
                    )
                segments = with_texts(segments, outcome.lines)
                translated = True
            return await self._write(media, library, segments, translated=translated)
        finally:
            await asyncio.to_thread(shutil.rmtree, work, ignore_errors=True)

    async def _translate(self, lines: Sequence[str]) -> TranslationOutcome:
        """转写字幕的翻译: 与字段翻译共用 llm 段, 因此也要走同一个带 proxy 的客户端与同一条限速."""
        hot = self._hot
        if not hot.llm.enabled or not hot.llm.api_key:
            raise TranslationError("要求翻译, 但大模型未启用或缺 api_key")
        rate_limit = max(hot.llm.rate_limit, 0.1)
        async with build_model_http_client(hot.network.proxy) as http_client:
            model = build_model(
                hot.llm.api_type,
                base_url=hot.llm.base_url,
                api_key=hot.llm.api_key,
                model=hot.llm.model,
                http_client=http_client,
            )
            return await translate_lines(model, lines, limiter=AsyncLimiter(1, 1 / rate_limit))

    async def _write(
        self,
        media: MediaFile,
        library: Library,
        segments: list,
        *,
        translated: bool,
    ) -> _TargetOutcome:
        """按视频自身位置落一份字幕; 与移动/整理共用库锁, 免得写到刚被搬走的路径上."""
        if library.id is None:
            return _TargetOutcome(_Outcome.FAILED, "媒体库尚未落库")
        language_tag = "zh" if translated else self._hot.subtitles.generation.language
        async with await self._library_locks.get(library.id):
            fresh = await self._repo.get_media_file(media.id) if media.id is not None else None
            current = fresh or media
            video = Path(current.path)
            target = video.with_suffix("").with_name(f"{video.stem}.{language_tag}.srt")
            if await asyncio.to_thread(target.exists):
                return _TargetOutcome(_Outcome.SKIPPED, "目标字幕已存在")
            marker = f"1\n{format_timestamp(0.0)} --> {format_timestamp(2.0)}\n{MACHINE_MARK} (Whisper)\n\n"
            body = marker + to_srt(segments, start_index=2)
            try:
                await asyncio.to_thread(target.write_text, body, encoding="utf-8")
            except OSError as exc:
                logger.warning("subtitle generate write failed", path=str(target), error=str(exc))
                return _TargetOutcome(_Outcome.FAILED, f"写字幕失败: {exc}")
            await refresh_external_subtitle(self._repo, current, library)
        return _TargetOutcome(_Outcome.GENERATED, translated=translated)


def _work_dir() -> Path:
    """临时工作目录: 每次调用一个, 由调用方整目录删除."""
    return staging_dir()
