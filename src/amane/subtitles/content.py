"""下载内容的校验与解包.

来源给的常常是压缩包或错误页: assrt 的 ``filelist`` 指向 ``.zip``, subtitlecat 出错时回一页 HTML.
落位之前必须先确认拿到的是字幕文本, 否则会把一张错误页改名成 ``.zh.srt`` 塞进媒体库.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence
from pathlib import PurePosixPath

from .models import LANGUAGE_TAG, MAX_SUBTITLE_BYTES, SUBTITLE_SUFFIXES, SubtitleSourceError

_ZIP_MAGIC = b"PK\x03\x04"
_HTML_MARKS = (b"<!doctype", b"<html", b"<head")
_SAFE_STEM = "字幕"


def unwrap_subtitle(
    payload: bytes, *, name_hint: str = "", preferred_suffixes: Sequence[str] = ()
) -> tuple[bytes, str]:
    """把下载到的东西变成 (字幕正文, 后缀).

    压缩包只取一个可落位的条目: 一份视频只需要一条字幕, 多条目靠候选排序已经选过一次.
    ``preferred_suffixes`` 是媒体库认得的那几种扩展名: 先在这些条目里挑, 挑不到才按包内顺序取 ——
    否则包里的第一个条目可能是库不认的格式, 落位后还得再抓一次.
    任何无法确认是字幕的应答都按确定性失败报错, 重试同一个地址不会变好.
    """
    if not payload.strip():
        raise SubtitleSourceError("字幕内容为空", transient=False)
    if len(payload) > MAX_SUBTITLE_BYTES:
        raise SubtitleSourceError(f"字幕超过大小上限 {MAX_SUBTITLE_BYTES} 字节", transient=False)

    data, suffix = (payload, _suffix_of(name_hint))
    if payload.startswith(_ZIP_MAGIC):
        data, suffix = _first_archive_entry(payload, preferred_suffixes)
    _ensure_text(data)
    return data, suffix if suffix in SUBTITLE_SUFFIXES else ".srt"


def _suffix_of(name: str) -> str:
    return PurePosixPath(name).suffix.casefold()


def _first_archive_entry(payload: bytes, preferred_suffixes: Sequence[str] = ()) -> tuple[bytes, str]:
    preferred = {suffix.casefold() for suffix in preferred_suffixes}
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = [
                (info, _suffix_of(info.filename))
                for info in archive.infolist()
                if not info.is_dir() and _suffix_of(info.filename) in SUBTITLE_SUFFIXES
            ]
            # 库认得的扩展名优先, 同优先级保持包内顺序 (确定性, 便于复现).
            entries.sort(key=lambda pair: 0 if pair[1] in preferred else 1)
            for info, suffix in entries:
                # 先看条目头里的声明大小, 避免解压炸弹把内存吃干净.
                if info.file_size > MAX_SUBTITLE_BYTES:
                    raise SubtitleSourceError(f"压缩包内字幕超过大小上限 {MAX_SUBTITLE_BYTES} 字节", transient=False)
                return archive.read(info), suffix
    except zipfile.BadZipFile as exc:
        raise SubtitleSourceError("下载内容不是有效的压缩包", transient=False) from exc
    raise SubtitleSourceError("压缩包内没有字幕文件", transient=False)


def _ensure_text(data: bytes) -> None:
    """拒二进制与 HTML: 两者都说明来源回错了东西."""
    head = data[:1024].lower()
    if b"\x00" in head:
        raise SubtitleSourceError("下载内容不是字幕文本", transient=False)
    if any(mark in head for mark in _HTML_MARKS):
        raise SubtitleSourceError("下载内容是一个网页而不是字幕", transient=False)


def staged_name(video_stem: str, suffix: str) -> str:
    """临时字幕文件名: 决定落位结果的名字, 见 ``LANGUAGE_TAG`` 的说明."""
    stem = video_stem or _SAFE_STEM
    return f"{stem}.{LANGUAGE_TAG}{suffix}"
