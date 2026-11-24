"""subtitlecat 来源: HTML 搜索页 → 详情页 → 直链 ``.srt``.

站点没有 API, 只能按 HTML 结构取链接. 解析函数是纯函数, 用固定页面做单测;
结构一变就是解析返回空列表, 抓取报"未搜到", 不会误落位别的文件.

候选排序照抄面板的启发式 (中文优先、机翻减分), 但语言偏好取自配置而不是写死:
面板默认"简体优先、繁体减 15 分", 而 ``subtitles.languages`` 就是给人改这个顺序用的.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final
from urllib.parse import quote, urljoin

from ..net import RequestError
from .models import (
    SubtitleCandidate,
    SubtitleDownload,
    SubtitleSourceError,
    is_transient_failure,
    language_tags,
    normalize_key,
    request_error_message,
    score_title,
)
from .transport import SubtitleHttp, default_headers, download_subtitle

SUBTITLECAT_BASE: Final[str] = "https://www.subtitlecat.com"

_MAX_DETAIL_PAGES: Final[int] = 3
"""与面板一致: 只进前三名的详情页, 更靠后的候选命中率极低, 多开只是多打站点."""

_CANDIDATE_RE: Final[re.Pattern[str]] = re.compile(
    r'<a\b[^>]*href="(subs/\d+/[^"]+?\.html)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_DOWNLOAD_RE: Final[re.Pattern[str]] = re.compile(
    r'href="(/subs/\d+/[^"]+?\.(?:srt|ass|ssa|vtt))"',
    re.IGNORECASE,
)
_TAG_RE: Final[re.Pattern[str]] = re.compile(r"[-.](zh-cn|zh-tw|zh)(?:\.|$)", re.IGNORECASE)
_TITLE_BREAK_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_SPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


def _strip_tags(fragment: str) -> str:
    return _SPACE_RE.sub(" ", _TITLE_BREAK_RE.sub(" ", fragment)).strip()


def parse_search(html: str, number: str, tags: Sequence[str]) -> list[SubtitleCandidate]:
    """搜索页 → 候选列表, 按打分降序.

    只保留标题里含番号的候选: subtitlecat 的搜索是全文匹配, 会带回一堆同演员/同厂牌的无关结果.
    """
    key = normalize_key(number)
    candidates: list[SubtitleCandidate] = []
    for href, raw_title in _CANDIDATE_RE.findall(html):
        title = _strip_tags(raw_title)
        if key and key not in normalize_key(title):
            continue
        candidates.append(SubtitleCandidate(title=title, detail_url=urljoin(SUBTITLECAT_BASE + "/", href)))
    return sorted(candidates, key=lambda c: score_title(c.title, tags), reverse=True)


def pick_download_path(html: str, tags: Sequence[str]) -> str | None:
    """详情页 → 下载路径 (站内相对地址), 按 ``tags`` 顺序挑语言变体.

    面板只认 ``-zh-CN.srt`` / ``-zh-TW.srt`` / ``-zh.srt`` 三种; 这里放宽到任意
    ``zh`` / ``zh-CN`` / ``zh-TW`` 前缀加 ``.srt/.ass/.ssa/.vtt``, 并保留无语言标记的最后兜底.
    """
    paths = list(dict.fromkeys(_DOWNLOAD_RE.findall(html)))
    if not paths:
        return None
    ranked: list[tuple[int, int, str]] = []
    for order, path in enumerate(paths):
        match = _TAG_RE.search(path)
        tag = match.group(1).casefold() if match else ""
        rank = tags.index(tag) if tag in tags else len(tags)
        ranked.append((rank, order, path))
    ranked.sort()
    return ranked[0][2]


async def fetch(
    web: SubtitleHttp,
    number: str,
    *,
    languages: Sequence[str],
    preferred_suffixes: Sequence[str] = (),
) -> SubtitleDownload:
    """按番号取一份字幕."""
    tags = language_tags(languages)
    search_url = f"{SUBTITLECAT_BASE}/index.php?search={quote(number)}"
    try:
        html = await web.get_text(search_url, headers=default_headers())
    except RequestError as exc:
        raise SubtitleSourceError(
            request_error_message("subtitlecat 搜索失败", exc),
            transient=is_transient_failure(exc),
        ) from exc

    candidates = parse_search(html, number, tags)
    if not candidates:
        raise SubtitleSourceError(f"subtitlecat 未搜到 {number}", transient=False)

    last_error: SubtitleSourceError | None = None
    for candidate in candidates[:_MAX_DETAIL_PAGES]:
        try:
            detail = await web.get_text(candidate.detail_url, headers=default_headers())
        except RequestError as exc:
            last_error = SubtitleSourceError(
                request_error_message("subtitlecat 详情页读取失败", exc),
                transient=is_transient_failure(exc),
            )
            continue
        path = pick_download_path(detail, tags)
        if path is None:
            continue
        return await download_subtitle(
            web,
            SUBTITLECAT_BASE + quote(path, safe="/"),
            provider="subtitlecat",
            description=f"{number} ← {candidate.title}",
            name_hint=path,
            preferred_suffixes=preferred_suffixes,
        )
    if last_error is not None:
        raise last_error
    raise SubtitleSourceError(f"subtitlecat 详情页没有中文字幕: {number}", transient=False)
