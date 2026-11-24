"""转写结果的解析与 SRT 组装: 只做纯函数级的判据, 不连网也不跑 ffmpeg."""

from __future__ import annotations

import pytest

from amane.subtitles.whisper import (
    Segment,
    WhisperError,
    format_timestamp,
    parse_segments,
    srt_lines,
    to_srt,
    with_texts,
)


class TestParseSegments:
    def test_reads_verbose_json_segments(self) -> None:
        body = {
            "segments": [{"start": 0.5, "end": 2.25, "text": " こんにちは "}, {"start": 2.25, "end": 4, "text": "元気"}]
        }

        segments = parse_segments(body)

        assert [(item.start, item.end, item.text) for item in segments] == [
            (0.5, 2.25, "こんにちは"),
            (2.25, 4.0, "元気"),
        ]

    def test_missing_segments_is_an_error(self) -> None:
        """没有分段就拼不出时间轴: 只回 text 的端点不满足契约."""
        with pytest.raises(WhisperError, match="没有返回分段"):
            parse_segments({"text": "整段文字"})

    def test_empty_segments_is_an_error(self) -> None:
        with pytest.raises(WhisperError, match="全是空"):
            parse_segments({"segments": [{"start": 0, "end": 1, "text": "   "}]})

    def test_negative_start_is_clamped(self) -> None:
        segments = parse_segments({"segments": [{"start": -1, "end": 1, "text": "a"}]})

        assert segments[0].start == 0.0


class TestSrt:
    def test_timestamp_format(self) -> None:
        assert format_timestamp(0) == "00:00:00,000"
        assert format_timestamp(3661.5) == "01:01:01,500"
        assert format_timestamp(-5) == "00:00:00,000"

    def test_to_srt_numbers_lines(self) -> None:
        body = to_srt([Segment(0.0, 1.5, "第一句"), Segment(1.5, 3.0, "第二句")])

        assert body.splitlines()[:4] == ["1", "00:00:00,000 --> 00:00:01,500", "第一句", ""]
        assert "2\n00:00:01,500 --> 00:00:03,000\n第二句" in body
        assert body.endswith("\n")

    def test_with_texts_replaces_only_text(self) -> None:
        segments = [Segment(0.0, 1.0, "原文")]

        assert with_texts(segments, ["译文"]) == [Segment(0.0, 1.0, "译文")]

    def test_with_texts_keeps_timeline_when_count_mismatches(self) -> None:
        """行数对不上时宁可留原文: 错位字幕比没翻译更糟."""
        segments = [Segment(0.0, 1.0, "原文"), Segment(1.0, 2.0, "第二句")]

        assert with_texts(segments, ["只有一行"]) == segments

    def test_srt_lines_extracts_text(self) -> None:
        assert srt_lines([Segment(0.0, 1.0, "a"), Segment(1.0, 2.0, "b")]) == ["a", "b"]
