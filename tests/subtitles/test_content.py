"""下载内容的校验与解包: 来源回 HTML 或压缩包时不能把垃圾落成字幕."""

from __future__ import annotations

import io
import zipfile

import pytest

from amane.subtitles import SubtitleSourceError, staged_name
from amane.subtitles.content import unwrap_subtitle
from amane.subtitles.models import MAX_SUBTITLE_BYTES

SRT = b"1\n00:00:01,000 --> 00:00:02,000\n\xe4\xbd\xa0\xe5\xa5\xbd\n"


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class TestUnwrap:
    def test_plain_subtitle_keeps_its_suffix(self):
        assert unwrap_subtitle(SRT, name_hint="AD-1.ass") == (SRT, ".ass")

    def test_unknown_suffix_falls_back_to_srt(self):
        assert unwrap_subtitle(SRT, name_hint="AD-1") == (SRT, ".srt")

    def test_archive_yields_first_playable_entry(self):
        """assrt 常回压缩包: 取第一个可落位的条目, 跳过目录与说明文件."""
        payload = _zip({"readme.txt": b"hi", "AD-1.zh.srt": SRT})
        assert unwrap_subtitle(payload) == (SRT, ".srt")

    @pytest.mark.parametrize(
        ("payload", "mark"),
        [
            (b"", "为空"),
            (b"   \n", "为空"),
            (b"\x00\x01\x02binary", "不是字幕文本"),
            (b"<!DOCTYPE html><html><body>error</body></html>", "网页"),
        ],
    )
    def test_non_subtitle_payload_is_rejected(self, payload: bytes, mark: str):
        """错误页与二进制都要拦下: 落位成 ``.zh.srt`` 之后播放器只会报字幕损坏."""
        with pytest.raises(SubtitleSourceError) as excinfo:
            unwrap_subtitle(payload)
        assert mark in str(excinfo.value)
        assert excinfo.value.transient is False

    def test_oversized_plain_payload_is_rejected(self):
        with pytest.raises(SubtitleSourceError):
            unwrap_subtitle(b"1\n" + b"x" * (MAX_SUBTITLE_BYTES + 1))

    def test_archive_entry_declaring_huge_size_is_rejected_before_reading(self):
        """解压炸弹: 按条目头里的声明大小先拒, 不等解压完看内存."""
        payload = _zip({"AD-1.srt": b"x" * (MAX_SUBTITLE_BYTES + 1)})
        with pytest.raises(SubtitleSourceError) as excinfo:
            unwrap_subtitle(payload)
        assert "大小上限" in str(excinfo.value)

    def test_archive_without_subtitle_is_rejected(self):
        with pytest.raises(SubtitleSourceError) as excinfo:
            unwrap_subtitle(_zip({"readme.txt": b"hi"}))
        assert "没有字幕文件" in str(excinfo.value)


class TestStagedName:
    def test_staged_name_carries_video_stem_and_language(self):
        """落位名由临时文件 stem 决定, 因此语言标记必须在临时名里."""
        assert staged_name("AD-1", ".srt") == "AD-1.zh.srt"
        assert staged_name("AD-1", ".ass") == "AD-1.zh.ass"

    def test_staged_name_survives_empty_stem(self):
        assert staged_name("", ".srt") == "字幕.zh.srt"
