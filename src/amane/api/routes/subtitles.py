"""字幕补全的状态读取; 抓取是 ``SUBTITLE_FETCH`` 任务, 转写生成是 ``SUBTITLE_GENERATE``."""

import io
import time
import wave
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx2
from fastapi import APIRouter

from ...subtitles import SubtitleFetcher
from ...subtitles.whisper import SAMPLE_RATE, WhisperError, transcribe
from ..deps import ConfigDep, RepoDep
from ..models.subtitles import SubtitleGenerationTestResponse, SubtitleStatusResponse

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
