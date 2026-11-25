"""avsubtitles 来源: HTML 搜索 → 影片页 → 字幕详情页 → 确认页 → 压缩包下载.

站点没有 API, 且下载分两步: 详情页给出 ``revid``, ``download_page.php`` 建立会话, 才算拿到
``download_sub.php`` 的通行证 —— 少了那一步只有 302 到 ``error_403.php``. 会话由 ``WebClient``
的 cookie 罐维持, 所以这里在下载前先取一次确认页.

解析函数是纯函数, 用固定页面做单测; 结构一变就是解析返回空, 报"未搜到", 不会误落位别的文件.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlencode, urljoin

from ..net import RequestError
from .models import (
    SubtitleDownload,
    SubtitleSourceError,
    is_transient_failure,
    normalize_key,
    request_error_message,
)
from .transport import SubtitleHttp, default_headers, download_subtitle

AVSUBTITLES_BASE: Final[str] = "https://www.avsubtitles.com"

_MOVIE_RE: Final[re.Pattern[str]] = re.compile(
    r'<a\b[^>]*href="(/movie\d+/[^"]+?)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_SUBTITLE_RE: Final[re.Pattern[str]] = re.compile(
    r'<a\b[^>]*href="(/movie\d+/[^"]+?/subtitles/([a-z]{2})/(\d+))"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_REVID_RE: Final[re.Pattern[str]] = re.compile(
    r'name="revid"\s+value="(\d+)"',
    re.IGNORECASE,
)
_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_SPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")

_LANGUAGE_PATHS: Final[dict[str, tuple[str, ...]]] = {
    "zh-cn": ("zh",),
    "zh_cn": ("zh",),
    "zh-tw": ("zh",),
    "zh_tw": ("zh",),
    "zh-hans": ("zh",),
    "zh-hant": ("zh",),
}
"""配置里的语言码 → 站点路径里的语言段. 站点只分 ``zh`` / ``ja`` / ``ko`` 三种."""

_DEFAULT_LANGUAGE_PATH: Final[str] = "zh"
"""配置没给中文时仍然只找中文: 本包的用途是"补中文字幕", 日文与韩文属于另一件事."""


@dataclass(frozen=True, slots=True)
class MovieCandidate:
    """搜索结果里的一条影片: 番号与影片页地址."""

    number: str
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class SubtitleLink:
    """影片页里的一条字幕: 语言段 + 详情页地址 + 主键."""

    language: str
    sub_id: str
    url: str


def _strip_tags(fragment: str) -> str:
    return _SPACE_RE.sub(" ", _TAG_RE.sub(" ", fragment)).strip()


def parse_search(html: str, number: str) -> list[MovieCandidate]:
    """搜索页 → 候选影片, 只留标题含番号的条目.

    站点搜索是全文匹配, 会带回同演员/同系列的结果; 番号对不上就是另一个片子.
    """
    key = normalize_key(number)
    seen: set[str] = set()
    candidates: list[MovieCandidate] = []
    for href, raw_title in _MOVIE_RE.findall(html):
        title = _strip_tags(raw_title)
        if not title or key not in normalize_key(title):
            continue
        url = urljoin(AVSUBTITLES_BASE + "/", href)
        if url in seen:
            continue
        seen.add(url)
        candidates.append(MovieCandidate(number=number, title=title, url=url))
    return candidates


def parse_subtitles(html: str) -> list[SubtitleLink]:
    """影片页 → 该片的所有字幕条目, 保留页面顺序."""
    links: list[SubtitleLink] = []
    for href, language, sub_id, _title in _SUBTITLE_RE.findall(html):
        links.append(
            SubtitleLink(
                language=language.casefold(),
                sub_id=sub_id,
                url=urljoin(AVSUBTITLES_BASE + "/", href),
            )
        )
    return links


def pick_subtitle(links: Sequence[SubtitleLink], languages: Sequence[str]) -> SubtitleLink | None:
    """按配置语言挑一条; 站点只给 ``zh``, 因此中文配置全部映射到它."""
    wanted: list[str] = []
    for language in languages:
        wanted.extend(_LANGUAGE_PATHS.get(language.strip().casefold(), ()))
    wanted.append(_DEFAULT_LANGUAGE_PATH)
    for language in dict.fromkeys(wanted):
        for link in links:
            if link.language == language:
                return link
    return None


def parse_revision(html: str) -> str | None:
    """详情页 → ``revid``; 下载接口要求它与 ``subid`` 同时出现."""
    match = _REVID_RE.search(html)
    return match.group(1) if match else None


def download_urls(sub_id: str, revision: str) -> tuple[str, str]:
    """两步下载的地址: 先确认页 (建会话), 再取压缩包."""
    query = urlencode({"subid": sub_id, "revid": revision})
    return (
        f"{AVSUBTITLES_BASE}/download_page.php?{query}",
        f"{AVSUBTITLES_BASE}/download_sub.php?{query}",
    )


async def fetch(
    web: SubtitleHttp,
    number: str,
    *,
    languages: Sequence[str],
    preferred_suffixes: Sequence[str] = (),
) -> SubtitleDownload:
    """按番号取一份中文字幕; 失败抛 ``SubtitleSourceError`` 并标明是否值得重试."""
    headers = default_headers()
    try:
        search_html = await web.get_text(
            f"{AVSUBTITLES_BASE}/search_results.php?{urlencode({'search': number})}",
            headers=headers,
        )
    except RequestError as exc:
        raise SubtitleSourceError(
            request_error_message("avsubtitles 搜索失败", exc), transient=is_transient_failure(exc)
        ) from exc

    movies = parse_search(search_html, number)
    if not movies:
        raise SubtitleSourceError(f"avsubtitles 未搜到 {number}", transient=False)

    movie = movies[0]
    try:
        movie_html = await web.get_text(movie.url, headers=headers)
    except RequestError as exc:
        raise SubtitleSourceError(
            request_error_message("avsubtitles 影片页失败", exc), transient=is_transient_failure(exc)
        ) from exc

    link = pick_subtitle(parse_subtitles(movie_html), languages)
    if link is None:
        raise SubtitleSourceError(f"avsubtitles 没有 {number} 的中文字幕", transient=False)

    try:
        detail_html = await web.get_text(link.url, headers=headers)
    except RequestError as exc:
        raise SubtitleSourceError(
            request_error_message("avsubtitles 详情页失败", exc), transient=is_transient_failure(exc)
        ) from exc

    revision = parse_revision(detail_html)
    if revision is None:
        raise SubtitleSourceError("avsubtitles 详情页没有 revid", transient=False)

    confirm_url, file_url = download_urls(link.sub_id, revision)
    # 确认页会下发会话 cookie, 下载接口凭它放行; 少了这一步只有 302 到 error_403.
    try:
        await web.get_text(confirm_url, headers=headers)
    except RequestError as exc:
        raise SubtitleSourceError(
            request_error_message("avsubtitles 确认页失败", exc), transient=is_transient_failure(exc)
        ) from exc

    return await download_subtitle(
        web,
        file_url,
        provider="avsubtitles",
        description=f"{movie.title} ({link.language})",
        name_hint=number,
        preferred_suffixes=preferred_suffixes,
    )
