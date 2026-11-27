"""字幕补全的状态读取; 抓取是 ``SUBTITLE_FETCH`` 任务, 转写生成是 ``SUBTITLE_GENERATE``."""

import asyncio
import io
import time
import wave
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import httpx2
import structlog
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from ...handlers._common import media_refresh_followups, refresh_external_subtitle
from ...subtitles import SubtitleFetcher
from ...subtitles.gaps import ATTEMPT_WINDOW_DAYS, GapReason, GapRow, collect_gaps
from ...subtitles.whisper import SAMPLE_RATE, WhisperError, transcribe
from ..deps import ConfigDep, RepoDep, RuntimeDep
from ..models.subtitles import (
    SubtitleFileEntry,
    SubtitleFilesResponse,
    SubtitleGapCounts,
    SubtitleGapItem,
    SubtitleGapsResponse,
    SubtitleGenerationTestResponse,
    SubtitleStatusResponse,
    SubtitleUploadResponse,
)
from ..support.uploads import read_limited

logger = structlog.get_logger()

router = APIRouter(prefix="/subtitles", tags=["subtitles"])


_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


async def _require_same_dir(target: Path, video: Path) -> None:
    """字幕只能与视频同目录: 允许别处的路径会让删除越过库的边界."""
    same_dir = await asyncio.to_thread(lambda: target.parent.resolve() == video.parent.resolve())
    if not same_dir:
        raise HTTPException(status_code=422, detail="只能删除与该视频同目录的字幕")


async def _write_atomic(target: Path, payload: bytes) -> None:
    """先写临时文件再原子替换: 写一半失败时播放器不会读到半份字幕."""

    def _write() -> None:
        temporary = target.with_name(f".{target.name}.upload-tmp")
        try:
            temporary.write_bytes(payload)
            temporary.replace(target)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise

    try:
        await asyncio.to_thread(_write)
    except OSError as error:
        raise HTTPException(status_code=422, detail=f"写字幕失败: {error.strerror or error}") from error


def _subtitle_target(video: Path, language: str, suffix: str) -> Path:
    """与视频同目录的字幕名: ``<视频名>.<语言><扩展名>``; 播放器与库的发现都认这个形态."""
    return video.with_suffix("").with_name(f"{video.stem}.{language}{suffix}")


def _clean_language(language: str) -> str:
    """校验语言标记: 它会被直接拼进文件名, 放开就等于允许任意路径段.

    规则与 ``_language_of_name`` 的解析口径一致 (字母数字与连字符, 最多 8 字符);
    留空按简体中文.
    """
    value = language.strip() or "zh"
    if len(value) > 8 or not all(part.isalnum() for part in value.split("-")):
        raise HTTPException(status_code=422, detail=f"语言标记不合法: {value}")
    return value


async def _read_limited(file: UploadFile) -> bytes:
    """按 20 MB 上限读字幕文件."""
    return await read_limited(
        file,
        limit=_MAX_UPLOAD_BYTES,
        too_large="字幕文件超过 20 MB",
        empty="上传的文件是空的",
    )


def _language_of_name(video: Path, name: str) -> str | None:
    """从 ``<视频名>.<语言><扩展名>`` 里取语言段; 形态对不上就返回空."""
    stem = Path(name).stem
    prefix = f"{video.stem}."
    if not stem.startswith(prefix):
        return None
    language = stem[len(prefix) :]
    if not language or len(language) > 8:
        return None
    if not all(part.isalnum() for part in language.split("-")):
        return None
    return language


async def _media_and_library(repo: RepoDep, media_file_id: int):
    media = await repo.get_media_file(media_file_id)
    if media is None:
        raise HTTPException(status_code=404, detail="媒体文件不存在")
    library = await repo.get_library(media.library_id) if media.library_id is not None else None
    if library is None or library.id is None:
        raise HTTPException(status_code=422, detail="媒体文件不属于任何媒体库")
    return media, library


async def _refresh_media_server(runtime, repo, library, *, reason: str, changed: int) -> None:
    """改过盘上的字幕后让媒体服务器重扫: 与整理、洗版、删除共用同一处判定与同一个后继键."""
    followups = media_refresh_followups(runtime.config.hot, library, changed=changed, reason=reason)
    await repo.create_followups_unless_queued([(item.task_type, item.payload, item.priority) for item in followups])


def _allowed_extensions(library) -> set[str]:
    return {item.lower() for item in library.subtitle_extensions}


def _list_subtitle_files(video: Path, allowed: set[str]) -> list[SubtitleFileEntry]:
    """同步枚举视频同目录的字幕文件; 只在 ``asyncio.to_thread`` 里调用."""
    files: list[SubtitleFileEntry] = []
    if not video.parent.is_dir():
        return files
    try:
        entries = sorted(video.parent.iterdir())
    except OSError:
        return files
    for entry in entries:
        if entry.suffix.lower() not in allowed:
            continue
        try:
            if not entry.is_file():
                continue
            stat = entry.stat()
        except OSError:
            continue
        files.append(
            SubtitleFileEntry(
                name=entry.name,
                path=str(entry),
                extension=entry.suffix.lower(),
                size=stat.st_size,
                language=_language_of_name(video, entry.name),
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            )
        )
    return files


@router.get("/files", response_model=SubtitleFilesResponse)
async def list_subtitle_files(
    repo: RepoDep,
    media_file_id: int,
) -> SubtitleFilesResponse:
    """列出视频旁边现有的字幕, 供替换或删除."""
    media, library = await _media_and_library(repo, media_file_id)
    video = Path(media.path)
    allowed = _allowed_extensions(library)
    # 目录枚举要进线程: 媒体库常挂 SMB/NFS, 抖动时同步 stat 会把整个事件循环卡住.
    files = await asyncio.to_thread(_list_subtitle_files, video, allowed) if allowed else []
    return SubtitleFilesResponse(
        media_file_id=media_file_id,
        video_path=str(video),
        extensions=sorted(allowed),
        files=files,
    )


@router.post("/delete", response_model=SubtitleFilesResponse)
async def delete_subtitle_file(
    repo: RepoDep,
    runtime: RuntimeDep,
    media_file_id: Annotated[int, Form(description="媒体文件的 id")],
    path: Annotated[str, Form(description="要删除的字幕文件路径")],
) -> SubtitleFilesResponse:
    """删掉视频旁边的一份字幕, 路径必须就在该视频所在目录里.

    取库锁再动手: 整理与洗版可能正把这份视频搬去别处, 锁外删会删到刚被搬走的旧路径上.
    """
    _media, library = await _media_and_library(repo, media_file_id)
    target = Path(path)
    if target.suffix.lower() not in _allowed_extensions(library):
        raise HTTPException(status_code=422, detail=f"不是字幕文件: {target.name}")
    async with await runtime.library_locks.get(library.id):
        # 锁内重读: 等锁期间整理 / 洗版可能已把这份视频搬去别处, 按锁外的视频路径判同目录会判错.
        fresh = await repo.get_media_file(media_file_id)
        if fresh is None:
            raise HTTPException(status_code=404, detail="媒体文件不存在")
        current = fresh
        await _require_same_dir(target, Path(current.path))
        # 删盘放到线程里: 网络盘上的 unlink 会阻塞事件循环, 卡住同时进行的任务与请求.
        try:
            await asyncio.to_thread(target.unlink)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="字幕文件已不存在") from None
        except OSError as error:
            raise HTTPException(status_code=422, detail=f"删除失败: {error.strerror or error}") from error
        await refresh_external_subtitle(repo, current, library)
        await _refresh_media_server(runtime, repo, library, reason="subtitles", changed=1)
    logger.info("subtitle deleted", media_id=media_file_id, path=str(target))
    return await list_subtitle_files(repo, media_file_id)


@router.post("/upload", response_model=SubtitleUploadResponse)
async def upload_subtitle(
    repo: RepoDep,
    runtime: RuntimeDep,
    media_file_id: Annotated[int, Form(description="媒体文件的 id")],
    file: Annotated[UploadFile, File(description="字幕文件 (srt/ass/ssa/vtt/sub)")],
    language: Annotated[str, Form(description="语言标记, 用于文件名")] = "zh",
    filename: Annotated[str, Form(description="自定义文件名; 留空按 <视频名>.<语言><扩展名> 命名")] = "",
) -> SubtitleUploadResponse:
    """手动上传一份字幕, 落在视频旁边.

    ``.strm`` 这类指针文件也接受: 字幕放在 ``.strm`` 旁边, 播放器与库都能认出来,
    这是本地没有音轨时唯一能补字幕的路子.
    """
    media = await repo.get_media_file(media_file_id)
    if media is None:
        raise HTTPException(status_code=404, detail="媒体文件不存在")
    library = await repo.get_library(media.library_id) if media.library_id is not None else None
    if library is None or library.id is None:
        raise HTTPException(status_code=422, detail="媒体文件不属于任何媒体库")
    suffix = Path(file.filename or "").suffix.lower()
    allowed = _allowed_extensions(library)
    if suffix not in allowed:
        wanted = ", ".join(sorted(allowed)) or "未配置"
        raise HTTPException(status_code=422, detail=f"字幕格式不支持: {suffix or '无扩展名'} (库接受 {wanted})")
    video = Path(media.path)
    if not await asyncio.to_thread(video.parent.is_dir):
        raise HTTPException(status_code=422, detail=f"视频所在目录不存在: {video.parent}")
    language = _clean_language(language)
    custom = filename.strip()
    if custom:
        if Path(custom).name != custom or custom in {".", ".."}:
            raise HTTPException(status_code=422, detail="文件名里不能带路径分隔符")
        if Path(custom).suffix.lower() != suffix:
            raise HTTPException(status_code=422, detail=f"自定义文件名的扩展名要和上传的文件一致: {suffix}")
    payload = await _read_limited(file)
    async with await runtime.library_locks.get(library.id):
        # 锁内重读并重算目标名: 等锁期间视频可能已被整理任务改名或搬走, 锁外算出的名字会落在旧目录.
        fresh = await repo.get_media_file(media_file_id)
        if fresh is None:
            raise HTTPException(status_code=404, detail="媒体文件不存在")
        current = fresh
        video = Path(current.path)
        target = video.parent / custom if custom else _subtitle_target(video, language, suffix)
        if not await asyncio.to_thread(video.parent.is_dir):
            raise HTTPException(status_code=422, detail=f"视频所在目录不存在: {video.parent}")
        await _write_atomic(target, payload)
        await refresh_external_subtitle(repo, current, library)
        await _refresh_media_server(runtime, repo, library, reason="subtitles", changed=1)
    logger.info("subtitle uploaded", media_id=media_file_id, path=str(target), bytes=len(payload))
    return SubtitleUploadResponse(path=str(target), bytes_written=len(payload), language=language)


@router.get("/status", response_model=SubtitleStatusResponse)
async def subtitle_status(config: ConfigDep, repo: RepoDep) -> SubtitleStatusResponse:
    """只读: 是否可用、缺口数与排队数.

    缺口统计是两条 count 查询; 前端每次进页面都会问一次, 因此这里不做任何磁盘遍历 ——
    "哪些文件真的没字幕"由任务自己按库扫描时判断.
    """
    fetcher = SubtitleFetcher(repo, config.hot.subtitles)
    summary = await fetcher.summary()
    return SubtitleStatusResponse(enabled=fetcher.enabled, **summary.model_dump())


def _probe_wav() -> bytes:
    """1 秒静音 wav: 探测连通性不需要语音, 越短越省端点时间."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(b"\x00\x00" * SAMPLE_RATE)
    return buffer.getvalue()


@router.get("/gaps", response_model=SubtitleGapsResponse)
async def subtitle_gaps(
    repo: RepoDep,
    library_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
    include_multi_disc: Annotated[bool, Query()] = False,
) -> SubtitleGapsResponse:
    """缺字幕的文件与原因: 哪个番号缺、为什么、试过几次、能不能重试.

    ``library_id`` 为空时看全部库. 分卷默认不列 (它不是补全目标), 需要时用
    ``include_multi_disc=true`` 打开, 便于解释「为什么扫描说 18 份分卷没字幕」.
    """
    if library_id is not None and await repo.get_library(library_id) is None:
        raise HTTPException(status_code=404, detail=f"媒体库不存在: {library_id}")
    rows = await collect_gaps(repo, library_id=library_id)
    visible = [row for row in rows if include_multi_disc or row.reason is not GapReason.MULTI_DISC]
    return SubtitleGapsResponse(
        items=[_gap_item(row) for row in visible[:limit]],
        total=len(visible),
        counts=_gap_counts(rows),
        window_days=ATTEMPT_WINDOW_DAYS,
    )


def _gap_item(row: GapRow) -> SubtitleGapItem:
    media = row.media
    media_id = media.id
    # 行都是从库里读出来的, 主键不会为空; ty 只认显式收窄.
    assert media_id is not None
    return SubtitleGapItem(
        media_file_id=media_id,
        number=row.label,
        path=media.path,
        kind=row.kind.value,
        reason=row.reason.value,
        detail=row.detail,
        attempts=row.attempts,
        failed=row.failed,
        last_error=row.last_error,
        last_attempt_at=row.last_attempt_at,
        retryable=row.retryable,
    )


def _gap_counts(rows: list[GapRow]) -> SubtitleGapCounts:
    counts = SubtitleGapCounts()
    for row in rows:
        kind = row.kind.value
        if kind == "multi_disc":
            counts.multi_disc += 1
        elif kind == "pointer":
            counts.pointer += 1
        else:
            counts.missing += 1
        match row.reason:
            case GapReason.FETCH_FAILED:
                counts.failed += 1
            case GapReason.FETCH_EMPTY:
                counts.fetch_empty += 1
            case GapReason.NOT_ATTEMPTED:
                counts.not_attempted += 1
            case GapReason.MULTI_DISC | GapReason.POINTER:
                pass
    return counts


@router.post("/generation/test", response_model=SubtitleGenerationTestResponse)
async def test_subtitle_generation(config: ConfigDep) -> SubtitleGenerationTestResponse:
    """用一段静音打一次转写端点, 报告能不能用与耗时.

    只做读取与一次探测: 不改配置、不写文件、不入任务表. 端点没配或没启用时直接说明原因.
    """
    generate = config.hot.subtitles.generation
    if not generate.enabled:
        return SubtitleGenerationTestResponse(ok=False, model=generate.model, latency_ms=0, detail="转写生成字幕未启用")
    if not generate.base_url.strip():
        return SubtitleGenerationTestResponse(ok=False, model=generate.model, latency_ms=0, detail="没有配置转写端点")
    started = time.monotonic()
    with TemporaryDirectory() as tmp:
        audio = Path(tmp) / "probe.wav"
        audio.write_bytes(_probe_wav())
        async with httpx2.AsyncClient(trust_env=False) as client:
            try:
                await transcribe(
                    client,
                    base_url=generate.base_url,
                    api_key=generate.api_key,
                    model=generate.model,
                    language=generate.language,
                    audio=audio,
                    timeout=min(float(generate.timeout_seconds), 120.0),
                )
            except WhisperError as exc:
                message = str(exc)
                elapsed = int((time.monotonic() - started) * 1000)
                # 静音本来就没有分段: 端点答了 200 就算通, 不算失败.
                if "没有返回分段" in message or "分段全是空" in message:
                    return SubtitleGenerationTestResponse(
                        ok=True, model=generate.model, latency_ms=elapsed, detail="端点可用（静音探测没有产生分段）"
                    )
                return SubtitleGenerationTestResponse(
                    ok=False, model=generate.model, latency_ms=elapsed, detail=message
                )
    elapsed = int((time.monotonic() - started) * 1000)
    return SubtitleGenerationTestResponse(ok=True, model=generate.model, latency_ms=elapsed, detail="端点可用")
