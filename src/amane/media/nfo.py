"""Kodi ``<movie>`` NFO, 供 Emby / Jellyfin / Kodi 读取."""

import asyncio
import re
from io import StringIO
from typing import TYPE_CHECKING
from uuid import uuid4

import aiofiles
import structlog

from ..utils.text import strip_illegal_xml_chars

if TYPE_CHECKING:
    from pathlib import Path

    from ..db.models import Metadata


logger = structlog.get_logger()


_ACTOR_BLOCK = re.compile(r"<actor>(.*?)</actor>", re.DOTALL)
_ACTOR_NAME = re.compile(r"<name>(.*?)</name>", re.DOTALL)
_ACTOR_THUMB = re.compile(r"<thumb>(.*?)</thumb>", re.DOTALL)

_XML_ESCAPE_MAP: dict[str, str] = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&apos;",
    '"': "&quot;",
}


def _escape_xml(text: str) -> str:
    """写进 XML 文本节点: 先剥非法字符 (存量脏数据兜底), 再转义; 换行原样保留."""
    escaped = strip_illegal_xml_chars(text)
    for char, entity in _XML_ESCAPE_MAP.items():
        escaped = escaped.replace(char, entity)
    return escaped


def _unescape_xml(text: str) -> str:
    """把写进 XML 文本节点的转义还原: 比对演员名时两侧必须同一形式."""
    for char, entity in _XML_ESCAPE_MAP.items():
        text = text.replace(entity, char)
    return text


def _actor_thumbs(text: str) -> dict[str, str]:
    """已有 NFO 里每个演员块的头像地址 (按演员名索引).

    头像由 ``library/nfo/patch.py`` 事后补进 NFO, 本模块整份重写时不带着它走,
    整理一次就把补好的头像抹掉 —— 补得很贵 (要抓演员图片), 抹掉却是免费的.
    """
    thumbs: dict[str, str] = {}
    for block in _ACTOR_BLOCK.findall(text):
        name = _ACTOR_NAME.search(block)
        thumb = _ACTOR_THUMB.search(block)
        if name is not None and thumb is not None:
            thumbs[_unescape_xml(name.group(1).strip())] = _unescape_xml(thumb.group(1).strip())
    return thumbs


def _title_with_number(number: str, title: str) -> str:
    """NFO 显示标题: 「番号 + 空格 + 标题」; 标题已以番号开头时不再重复拼接.

    番号后须为非字母数字, 否则 ``ABF-06`` 会把 ``ABF-062`` 当作自己的前缀.
    """
    text = title.strip()
    if number:
        match = re.match(re.escape(number), text, re.IGNORECASE)
        if match is not None:
            rest = text[match.end() :]
            if not rest or not rest[0].isalnum():
                text = rest.strip()
    return f"{number} {text}".strip()


async def _existing_thumbs(nfo_path: Path) -> dict[str, str]:
    """读已有 NFO 里的演员头像; 文件不存在或读不出来时按没有处理 (重写不该因此失败)."""
    try:
        text = await asyncio.to_thread(nfo_path.read_text, encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return {}
    return _actor_thumbs(text)


async def write_nfo(metadata: Metadata, nfo_path: Path) -> bool:
    try:
        nfo_path.parent.mkdir(parents=True, exist_ok=True)
        thumbs = await _existing_thumbs(nfo_path)

        code = StringIO()
        code.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n')
        code.write("<movie>\n")

        if metadata.plot:
            # 长文本按纯文本写出: 转义后 "]]>" 不可能出现, CDATA 只带来非法 XML 的风险.
            plot_escaped = _escape_xml(metadata.plot)
            code.write(f"  <plot>{plot_escaped}</plot>\n")
            code.write(f"  <outline>{plot_escaped}</outline>\n")

        if metadata.release:
            code.write(f"  <premiered>{metadata.release}</premiered>\n")
            code.write(f"  <releasedate>{metadata.release}</releasedate>\n")
            year_match = re.match(r"(\d{4})", metadata.release)
            if year_match:
                code.write(f"  <year>{year_match.group(1)}</year>\n")

        code.write(f"  <num>{_escape_xml(metadata.number)}</num>\n")

        if metadata.title:
            title = _escape_xml(_title_with_number(metadata.number, metadata.title))
            code.write(f"  <title>{title}</title>\n")
            code.write(f"  <originaltitle>{title}</originaltitle>\n")
            code.write(f"  <sorttitle>{title}</sorttitle>\n")

        code.write("  <mpaa>JP-18+</mpaa>\n")

        if metadata.actors:
            for actor in metadata.actors:
                code.write("  <actor>\n")
                code.write(f"    <name>{_escape_xml(actor)}</name>\n")
                thumb = thumbs.get(actor)
                if thumb:
                    code.write(f"    <thumb>{_escape_xml(thumb)}</thumb>\n")
                code.write("    <type>Actor</type>\n")
                code.write("  </actor>\n")

        if metadata.score is not None:
            code.write(f"  <rating>{metadata.score}</rating>\n")
            code.write(f"  <criticrating>{int(metadata.score * 10)}</criticrating>\n")

        if metadata.runtime is not None:
            code.write(f"  <runtime>{metadata.runtime}</runtime>\n")

        if metadata.series:
            code.write(f"  <series>{_escape_xml(metadata.series)}</series>\n")
            code.write("  <set>\n")
            code.write(f"    <name>{_escape_xml(metadata.series)}</name>\n")
            code.write("  </set>\n")

        if metadata.studio:
            code.write(f"  <studio>{_escape_xml(metadata.studio)}</studio>\n")
            code.write(f"  <maker>{_escape_xml(metadata.studio)}</maker>\n")

        if metadata.publisher:
            code.write(f"  <publisher>{_escape_xml(metadata.publisher)}</publisher>\n")
            code.write(f"  <label>{_escape_xml(metadata.publisher)}</label>\n")

        if metadata.tags:
            for tag in metadata.tags:
                if tag:
                    code.write(f"  <tag>{_escape_xml(tag)}</tag>\n")
                    code.write(f"  <genre>{_escape_xml(tag)}</genre>\n")

        if metadata.poster_url:
            code.write(f"  <poster>{_escape_xml(metadata.poster_url)}</poster>\n")

        if metadata.thumb_url:
            code.write(f"  <cover>{_escape_xml(metadata.thumb_url)}</cover>\n")

        if metadata.trailer_url:
            code.write(f"  <trailer>{_escape_xml(metadata.trailer_url)}</trailer>\n")

        if metadata.directors:
            for director in metadata.directors:
                code.write(f"  <director>{_escape_xml(director)}</director>\n")

        if metadata.external_ids:
            for site, ext_id in metadata.external_ids.items():
                if ext_id:
                    code.write(f"  <{site}id>{_escape_xml(str(ext_id))}</{site}id>\n")

        code.write("</movie>\n")

        # 同目录临时文件 + replace: 中断不会留下半截 XML, 那会被媒体服务器当成损坏的 NFO.
        temporary = nfo_path.with_name(f".{nfo_path.name}.{uuid4().hex}.tmp")
        try:
            async with aiofiles.open(temporary, "w", encoding="UTF-8") as f:
                await f.write(code.getvalue())
            await asyncio.to_thread(temporary.replace, nfo_path)
        finally:
            temporary.unlink(missing_ok=True)

        logger.debug("nfo written", path=str(nfo_path))
        return True

    except Exception:
        logger.exception("nfo write failed", path=str(nfo_path))
        return False
