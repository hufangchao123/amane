"""字幕补全的状态读取; 抓取是 ``SUBTITLE_FETCH`` 任务, 转写生成是 ``SUBTITLE_GENERATE``."""

import io
import time
import wave
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import httpx2
import structlog
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ...handlers._common import refresh_external_subtitle
from ...subtitles import SubtitleFetcher
from ...subtitles.whisper import SAMPLE_RATE, WhisperError, transcribe
from ..deps import ConfigDep, RepoDep
from ..models.subtitles import (
    SubtitleFileEntry,
    SubtitleFilesResponse,
    SubtitleGenerationTestResponse,
    SubtitleStatusResponse,
    SubtitleUploadResponse,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/subtitles", tags=["subtitles"])


_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _subtitle_target(video: Path, language: str, suffix: str) -> Path:
    """与视频同目录的字幕名: ``<视频名>.<语言><扩展名>``; 播放器与库的发现都认这个形态."""
    return video.with_suffix("").with_name(f"{video.stem}.{language}{suffix}")


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
    if library is None:
        raise HTTPException(status_code=422, detail="媒体文件不属于任何媒体库")
    return media, library


def _allowed_extensions(library) -> set[str]:
    return {item.lower() for item in library.subtitle_extensions}


@router.get("/files", response_model=SubtitleFilesResponse)
async def list_subtitle_files(
    repo: RepoDep,
    media_file_id: int,
) -> SubtitleFilesResponse:
    """列出视频旁边现有的字幕, 供替换或删除."""
    media, library = await _media_and_library(repo, media_file_id)
    video = Path(media.path)
    allowed = _allowed_extensions(library)
    files: list[SubtitleFileEntry] = []
    if video.parent.is_dir() and allowed:
        for entry in sorted(video.parent.iterdir()):
            if not entry.is_file() or entry.suffix.lower() not in allowed:
                continue
            try:
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
    return SubtitleFilesResponse(
        media_file_id=media_file_id,
        video_path=str(video),
        extensions=sorted(allowed),
        files=files,
    )


@router.post("/delete", response_model=SubtitleFilesResponse)
async def delete_subtitle_file(
    repo: RepoDep,
    media_file_id: Annotated[int, Form(description="媒体文件的 id")],
    path: Annotated[str, Form(description="要删除的字幕文件路径")],
) -> SubtitleFilesResponse:
    """删掉视频旁边的一份字幕, 路径必须就在该视频所在目录里."""
    media, library = await _media_and_library(repo, media_file_id)
    video = Path(media.path)
    target = Path(path)
    if target.parent.resolve() != video.parent.resolve():
        raise HTTPException(status_code=422, detail="只能删除与该视频同目录的字幕")
    if target.suffix.lower() not in _allowed_extensions(library):
        raise HTTPException(status_code=422, detail=f"不是字幕文件: {target.name}")
    try:
        target.unlink()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="字幕文件已不存在") from None
    except OSError as error:
        raise HTTPException(status_code=422, detail=f"删除失败: {error.strerror or error}") from error
    await refresh_external_subtitle(repo, media, library)
    logger.info("subtitle deleted", media_id=media_file_id, path=str(target))
    return await list_subtitle_files(repo, media_file_id)


@router.post("/upload", response_model=SubtitleUploadResponse)
async def upload_subtitle(
    repo: RepoDep,
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
    if library is None:
        raise HTTPException(status_code=422, detail="媒体文件不属于任何媒体库")
    suffix = Path(file.filename or "").suffix.lower()
    allowed = {item.lower() for item in library.subtitle_extensions}
    if suffix not in allowed:
        wanted = ", ".join(sorted(allowed)) or "未配置"
        raise HTTPException(status_code=422, detail=f"字幕格式不支持: {suffix or '无扩展名'} (库接受 {wanted})")
    video = Path(media.path)
    if not video.parent.is_dir():
        raise HTTPException(status_code=422, detail=f"视频所在目录不存在: {video.parent}")
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=422, detail="上传的文件是空的")
    if len(payload) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="字幕文件超过 20 MB")
    language = language.strip() or "zh"
    custom = filename.strip()
    if custom:
        if Path(custom).name != custom or custom in {".", ".."}:
            raise HTTPException(status_code=422, detail="文件名里不能带路径分隔符")
        if Path(custom).suffix.lower() != suffix:
            raise HTTPException(status_code=422, detail=f"自定义文件名的扩展名要和上传的文件一致: {suffix}")
        target = video.parent / custom
    else:
        target = _subtitle_target(video, language, suffix)
    temporary = target.with_name(f".{target.name}.upload-tmp")
    temporary.write_bytes(payload)
    temporary.replace(target)
    await refresh_external_subtitle(repo, media, library)
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
        async with httpx2.AsyncClient() as client:
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
