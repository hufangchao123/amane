"""字幕来源抓取的共用类型与常量.

抓取器要能脱离网络单测, 因此这里只放纯数据与纯判定: 候选、下载结果、失败语义.
有状态的东西 (token、限速、重试) 留在调用方传进来的 HTTP 通道与配置里.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from ..config.manager import SubtitleProvider
from ..net import FailureKind, RequestError

MAX_SUBTITLE_BYTES: Final[int] = 4 * 1024 * 1024
"""单份字幕的下载上限. 正常字幕几十 KB, 压缩包也不过百 KB; 超限说明来源回了非字幕内容."""

LANGUAGE_TAG: Final[str] = "zh"
"""落位文件名的语言标记.

``organize`` 的默认字幕模板是 ``{link_dir}/{raw_srt_name}.{ext}``, 而 ``place_subtitles`` 传入的是
临时字幕文件, ``raw_srt_name`` 取它的 stem. 所以临时文件必须命名成 ``<视频 stem>.zh<suffix>``,
落位结果才是 ``<视频 stem>.zh.srt``.
"""

SUBTITLE_SUFFIXES: Final[tuple[str, ...]] = (".srt", ".ass", ".ssa", ".vtt")
"""允许落位的字幕后缀.

与 ``library.rules.DEFAULT_SUBTITLE_EXTENSIONS`` 对齐但不取 ``.sub``: ``.sub`` 是图形字幕, 单独
一个文件无法播放, 需要配套的 ``.idx``; 抓取侧拿不到这一对时落位只会制造孤儿文件.
"""

_TRANSIENT_STATUSES: Final[frozenset[int]] = frozenset({408, 425, 429, 500, 502, 503, 504, 509})
"""值得重试的 HTTP 状态: 限流与上游临时故障. 404/403 重试多少次都是同一个答案."""

# 候选标题里的中文与语言标记. 来源标题是自由文本, 只能按标记排序, 不能据此认定语言.
_CHINESE_MARKS: Final[tuple[str, ...]] = ("中文", "中字", "汉化", "简体", "繁体", "双语", "chinese", "chs", "cht")
_MACHINE_MARKS: Final[tuple[str, ...]] = ("机翻", "machine", "whisper", "gpt", " auto ")
_FOREIGN_MARKS: Final[tuple[str, ...]] = ("english", "japanese", "日语", "英语")


@dataclass(frozen=True, slots=True)
class SubtitleCandidate:
    """来源列表页给出的一条候选: 展示名 + 详情页地址."""

    title: str
    detail_url: str


@dataclass(frozen=True, slots=True)
class SubtitleDownload:
    """已下载并校验过的字幕正文."""

    content: bytes
    suffix: str
    """含点, 来自来源给出的文件名或压缩包内条目."""
    provider: SubtitleProvider
    description: str
    """来源与文件摘要, 只用于日志与任务结果; 不含 token 与完整下载地址."""


class SubtitleSourceError(Exception):
    """来源抓取失败; ``transient`` 决定是否值得用 ``max_attempts`` 重试.

    区分临时与确定性失败是为了不把 404 重试三遍: 同一份列表页重试不会变出新字幕,
    而限流与断连重试通常能过.
    """

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


def language_tags(languages: Sequence[str]) -> tuple[str, ...]:
    """把配置语言码翻成来源里的文件名/标题标记, 按配置顺序返回.

    标记只用于排序候选, 不做硬过滤: 来源只提供一种字幕时, 硬过滤等于整个来源白跑.
    """
    tags: list[str] = []
    for language in languages:
        key = language.strip().casefold()
        if key in {"zh-cn", "zh_cn", "zh-hans", "chs"}:
            tags.extend(("zh-cn", "chs", "简"))
        elif key in {"zh-tw", "zh_tw", "zh-hant", "cht"}:
            tags.extend(("zh-tw", "cht", "繁"))
        elif key.startswith("zh"):
            tags.append("zh")
    # 无任何语言后缀的中文字幕排在最后, 但仍优于非中文候选.
    tags.append("zh")
    return tuple(dict.fromkeys(tags))


def score_title(title: str, tags: Sequence[str]) -> int:
    """按标题给候选打分: 中文优先, 其次按配置语言顺序, 机翻与非中文减分."""
    lowered = title.casefold()
    score = 100 if any(mark in lowered for mark in _CHINESE_MARKS) else 0
    for index, tag in enumerate(tags):
        if tag in lowered:
            score += max(0, 40 - index * 10)
            break
    if any(mark in lowered for mark in _MACHINE_MARKS):
        score -= 30
    if score <= 0 and any(mark in lowered for mark in _FOREIGN_MARKS):
        score -= 60
    return score


def normalize_key(text: str) -> str:
    """番号比对口径: 去掉非字母数字后大写. ``STARS-123`` 与 ``stars 123`` 视为同一个."""
    return "".join(ch for ch in text.upper() if ch.isalnum())


def is_transient_failure(exc: RequestError) -> bool:
    """出站失败的临时性判定; 来源据此决定要不要重试."""
    failure = exc.failure
    if failure is None:
        return True
    if failure.kind in {FailureKind.TIMEOUT, FailureKind.CURL}:
        return True
    if failure.kind is FailureKind.HTTP_STATUS and failure.status is not None:
        return failure.status in _TRANSIENT_STATUSES
    return False


def request_error_message(prefix: str, exc: RequestError) -> str:
    """只取 ``failure.message``, 不用 ``str(exc)``: 后者带完整 URL, assrt 的 URL 里含 token."""
    return f"{prefix}: {exc.message}"
