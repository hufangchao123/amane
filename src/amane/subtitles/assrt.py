"""assrt 来源: REST 搜索 + 详情拿下载地址.

接口形状 (两跳): ``/v1/sub/search?q=<番号>&token=…`` 给候选, ``/v1/sub/detail?id=…&token=…``
给 ``filelist``. 解析与网络分开, 解析函数是纯函数, 才能拿固定应答做单测.

token 只能走查询串: 接口没有头部认证的口子, 已知客户端 (bazarr / mpv 插件) 也都用 ``?token=``,
因此这里不改协议, 改为保证地址不外泄 —— 错误消息只取 ``failure.message`` (不用 ``str(exc)``),
取到之后再脱敏 (见 ``models.redact_message``), 日志与任务结果里就不会出现明文 token.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

from ..net import RequestError
from .models import (
    SubtitleDownload,
    SubtitleSourceError,
    is_transient_failure,
    language_tags,
    normalize_key,
    request_error_message,
    score_title,
)
from .transport import SubtitleHttp, default_headers, download_subtitle

ASSRT_API: Final[str] = "https://api.assrt.net"

_MAX_DETAIL_REQUESTS: Final[int] = 3
"""最多打开几个候选的详情页: 列表页常有同名不同版本, 只试前几个能省掉整轮跳转."""


@dataclass(frozen=True, slots=True)
class AssrtCandidate:
    """搜索结果里的一条: 主键 + 展示名."""

    sub_id: int
    title: str


@dataclass(frozen=True, slots=True)
class AssrtFile:
    """详情页 ``filelist`` 里的一条."""

    url: str
    name: str


def _field(payload: object, key: str) -> object:
    if not isinstance(payload, dict):
        return None
    return payload.get(key)


def parse_search(payload: object) -> list[AssrtCandidate]:
    """``/sub/search`` 应答 → 候选.

    应答里 ``sub.subs`` 有时是列表, 有时又被包一层 ``{"subs": [...]}``; 两种都收,
    否则接口一改就得跟着发版.
    """
    subs: object = _field(_field(payload, "sub"), "subs")
    if isinstance(subs, dict):
        subs = subs.get("subs")
    if not isinstance(subs, list):
        return []
    candidates: list[AssrtCandidate] = []
    for item in subs:
        sub_id = _field(item, "id")
        if not isinstance(sub_id, int):
            continue
        names = [text for key in ("videoname", "title", "native_name") if isinstance(text := _field(item, key), str)]
        candidates.append(AssrtCandidate(sub_id=sub_id, title=" / ".join(names)))
    return candidates


def parse_detail(payload: object) -> AssrtFile | None:
    """``/sub/detail`` 应答 → 第一个可下载文件.

    文件条目里成品地址在 ``url``, 少数条目把同名字段塞在 ``f``; 两者都认, 但没有地址的条目跳过.
    """
    subs = _field(_field(payload, "sub"), "subs")
    if not isinstance(subs, list) or not subs:
        return None
    filelist = _field(subs[0], "filelist")
    if not isinstance(filelist, list):
        return None
    for item in filelist:
        url = _field(item, "url")
        if not isinstance(url, str) or not url:
            continue
        name = _field(item, "f")
        if not isinstance(name, str):
            name = ""
        return AssrtFile(url=url, name=name)
    return None


def rank_candidates(candidates: Sequence[AssrtCandidate], number: str, tags: Sequence[str]) -> list[AssrtCandidate]:
    """只留标题含番号的候选.

    接口的模糊搜索会带回别的作品, 标题里没有番号就无从确认, 落位后无法发现错配 ——
    宁可这一轮没有字幕, 也不拿别的片子顶上.
    """
    key = normalize_key(number)
    matched = [c for c in candidates if key and key in normalize_key(c.title)]
    return sorted(matched, key=lambda c: score_title(c.title, tags), reverse=True)


async def fetch(web: SubtitleHttp, number: str, *, token: str, languages: Sequence[str]) -> SubtitleDownload:
    """按番号取一份字幕; 调用方已确保 ``token`` 非空."""
    results = await _search(web, number, token=token)
    candidates = rank_candidates(parse_search(results), number, language_tags(languages))
    if not candidates:
        raise SubtitleSourceError(f"assrt 未搜到 {number}", transient=False)

    last_error: SubtitleSourceError | None = None
    for candidate in candidates[:_MAX_DETAIL_REQUESTS]:
        try:
            remote = parse_detail(await _detail(web, candidate.sub_id, token=token))
        except SubtitleSourceError as exc:
            # 单个候选的详情页挂了不代表整轮失败: 换下一个候选继续.
            last_error = exc
            continue
        if remote is None:
            continue
        return await download_subtitle(
            web,
            remote.url,
            provider="assrt",
            description=f"{number} ← {remote.name or candidate.title}",
            name_hint=remote.name,
        )
    if last_error is not None:
        raise last_error
    raise SubtitleSourceError(f"assrt 没有给出可下载的字幕: {number}", transient=False)


async def _search(web: SubtitleHttp, number: str, *, token: str) -> object:
    url = f"{ASSRT_API}/v1/sub/search?q={quote(number)}&token={quote(token)}"
    try:
        return await web.get_json(url, headers=default_headers())
    except RequestError as exc:
        raise SubtitleSourceError(
            request_error_message("assrt 搜索失败", exc),
            transient=is_transient_failure(exc),
        ) from exc


async def _detail(web: SubtitleHttp, sub_id: int, *, token: str) -> object:
    url = f"{ASSRT_API}/v1/sub/detail?id={sub_id}&token={quote(token)}"
    try:
        return await web.get_json(url, headers=default_headers())
    except RequestError as exc:
        raise SubtitleSourceError(
            request_error_message("assrt 详情失败", exc),
            transient=is_transient_failure(exc),
        ) from exc
