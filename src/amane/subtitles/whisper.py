"""经外部 Whisper 服务转写音轨: 抽音频 → 调 ``/audio/transcriptions`` → 拼 SRT.

转写引擎不在镜像里: ``ffmpeg`` 抽轨, 端点按 OpenAI 兼容契约要 ``verbose_json`` 的分段.
本模块只做纯计算与 HTTP, 选目标、落位与记账都在 handler 里.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx2
import structlog

logger = structlog.get_logger()

SAMPLE_RATE = 16000
"""Whisper 训练用的采样率; 单声道 + 16 kHz 能把上传体积压到最小."""

_TRANSCRIBE_PATH = "/audio/transcriptions"


class WhisperError(Exception):
    """转写失败: 端点不可达、返回格式不对、ffmpeg 抽轨失败."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient
        """临时失败 (超时/5xx) 值得下一轮再试; 格式不对再试也没用."""


@dataclass(frozen=True, slots=True)
class Segment:
    start: float
    end: float
    text: str


def ffmpeg_path() -> str | None:
    """``ffmpeg`` 可执行文件; 镜像里没装时返回 ``None``."""
    return shutil.which("ffmpeg")


async def extract_audio(video: Path, dest: Path, *, timeout: float) -> None:
    """抽一条 16 kHz 单声道音轨到 ``dest``; 失败抛 ``WhisperError``."""
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise WhisperError("镜像里没有 ffmpeg, 无法抽取音轨")
    command = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise WhisperError("抽取音轨超时", transient=True) from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        raise WhisperError(f"抽取音轨失败: {detail[-1] if detail else process.returncode}")


async def transcribe(
    client: httpx2.AsyncClient,
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    language: str,
    audio: Path,
    timeout: float,
) -> list[Segment]:
    """把音轨交给转写端点, 返回带时间戳的分段."""
    if not base_url.strip():
        raise WhisperError("没有配置转写端点")
    url = base_url.rstrip("/") + _TRANSCRIBE_PATH
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = await asyncio.to_thread(audio.read_bytes)
    files = {"file": (audio.name, payload, "audio/wav")}
    data = {"model": model, "language": language, "response_format": "verbose_json", "temperature": "0"}
    try:
        response = await client.post(url, files=files, data=data, headers=headers, timeout=timeout)
    except httpx2.HTTPError as exc:
        raise WhisperError(f"转写端点不可达: {exc}", transient=True) from exc
    if response.status_code >= 500:
        raise WhisperError(f"转写端点返回 {response.status_code}", transient=True)
    if response.status_code >= 400:
        raise WhisperError(f"转写端点拒绝: {response.status_code} {response.text[:200]}")
    try:
        body = response.json()
    except ValueError as exc:
        raise WhisperError("转写端点没有返回 JSON") from exc
    return parse_segments(body)


def parse_segments(body: object) -> list[Segment]:
    """从 ``verbose_json`` 里取分段; 缺分段就报错, 拼不出时间轴的字幕没有意义."""
    if not isinstance(body, dict):
        raise WhisperError("转写端点返回的不是对象")
    raw = body.get("segments")
    if not isinstance(raw, list):
        raise WhisperError("转写端点没有返回分段 (response_format 需要 verbose_json)")
    segments: list[Segment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        try:
            start = float(item.get("start", 0.0))
            end = float(item.get("end", start))
        except TypeError, ValueError:
            continue
        segments.append(Segment(start=max(0.0, start), end=max(start, end), text=text))
    if not segments:
        raise WhisperError("转写端点返回的分段全是空的")
    return segments


def format_timestamp(seconds: float) -> str:
    """SRT 时间戳: ``HH:MM:SS,mmm``."""
    total_ms = round(max(0.0, seconds) * 1000)
    hours, rest = divmod(total_ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def to_srt(segments: Sequence[Segment]) -> str:
    """把分段拼成 SRT 正文 (Latin-1 之外的字都保留, 文件按 UTF-8 写)."""
    lines: list[str] = []
    for index, segment in enumerate(segments, start=1):
        lines.append(str(index))
        lines.append(f"{format_timestamp(segment.start)} --> {format_timestamp(segment.end)}")
        lines.append(segment.text)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def srt_lines(segments: Sequence[Segment]) -> list[str]:
    """只取正文行: 翻译按行对齐, 时间轴原样保留."""
    return [segment.text for segment in segments]


def with_texts(segments: Sequence[Segment], texts: Sequence[str]) -> list[Segment]:
    """替换正文, 时间轴不动; 行数对不上时原样返回, 宁可留原文也不写错位字幕."""
    if len(texts) != len(segments):
        logger.warning("subtitle translate line count mismatch", segments=len(segments), lines=len(texts))
        return list(segments)
    return [Segment(start=s.start, end=s.end, text=text or s.text) for s, text in zip(segments, texts, strict=True)]
