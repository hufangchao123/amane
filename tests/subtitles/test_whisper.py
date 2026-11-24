"""转写结果的解析与 SRT 组装: 只做纯函数级的判据, 不连网也不跑 ffmpeg."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from amane.subtitles import whisper as module
from amane.subtitles.whisper import (
    MAX_SEGMENT_SECONDS,
    Segment,
    WhisperError,
    extract_audio,
    format_timestamp,
    parse_segments,
    split_long_segments,
    srt_lines,
    to_srt,
    transcribe,
    with_texts,
)

if TYPE_CHECKING:
    from pathlib import Path


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


class _FakeResponse:
    status_code = 200

    def json(self) -> dict[str, Any]:
        return {"segments": [{"start": 0.0, "end": 1.0, "text": "こんにちは"}]}


class _FakeClient:
    """只记下 POST 的参数: 这里验的是请求契约, 不连网."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return _FakeResponse()


class TestTranscribeRequest:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_asks_for_vad_without_context(self, tmp_path: Path) -> None:
        """气声段会让模型反复吐同一个词: 必须开人声检测并关掉上下文继承."""
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        client = _FakeClient()

        await transcribe(
            client,  # type: ignore[arg-type]
            base_url="http://whisper.local/v1/",
            api_key=None,
            model="medium",
            language="ja",
            audio=audio,
            timeout=30.0,
        )

        data = client.calls[0]["data"]
        assert client.calls[0]["url"] == "http://whisper.local/v1/audio/transcriptions"
        assert data["response_format"] == "verbose_json"
        assert data["vad_filter"] == "true"
        assert data["condition_on_previous_text"] == "false"
        assert data["temperature"] == "0"


class TestExtractAudio:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_normalizes_loudness(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """素材录得轻时人声检测判不出, 抽轨阶段先拉到 -16 LUFS."""
        seen: list[list[str]] = []

        class _FakeProcess:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

        async def fake_exec(*args: str, **kwargs: Any) -> _FakeProcess:
            seen.append(list(args))
            return _FakeProcess()

        monkeypatch.setattr(module, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

        await extract_audio(tmp_path / "v.mp4", tmp_path / "a.wav", timeout=30.0)

        command = seen[0]
        assert command[command.index("-af") + 1] == module._LOUDNESS_FILTER
        assert command[-1] == str(tmp_path / "a.wav")


class TestSplitLongSegments:
    def test_short_segments_are_kept_as_is(self) -> None:
        segments = [Segment(start=0.0, end=3.0, text="こんにちは")]

        assert split_long_segments(segments) == segments

    def test_long_silence_spanning_segment_is_cut(self) -> None:
        """没台词的区间会被端点返回成一条跨度几十分钟的段: 必须切开, 否则一条字幕占半部片子."""
        segments = [Segment(start=95.0, end=1900.0, text="午饭是蛋包饭 我知道了 就这样吧")]

        pieces = split_long_segments(segments)

        assert len(pieces) > 1
        assert all(piece.end - piece.start <= MAX_SEGMENT_SECONDS + 1e-6 for piece in pieces)
        assert pieces[0].start == 95.0
        assert pieces[-1].end <= 1900.0
        assert "".join(piece.text for piece in pieces).replace(" ", "") == segments[0].text.replace(" ", "")

    def test_short_text_over_a_long_gap_is_clamped(self) -> None:
        """文字少到切不动时, 让字幕出现在段首几秒内, 而不是拉满整个空档."""
        segments = [Segment(start=10.0, end=900.0, text="はい")]

        pieces = split_long_segments(segments)

        assert len(pieces) == 1
        assert pieces[0].start == 10.0
        assert pieces[0].end == 10.0 + MAX_SEGMENT_SECONDS

    def test_zero_max_seconds_disables_splitting(self) -> None:
        segments = [Segment(start=0.0, end=600.0, text="あ" * 10)]

        assert split_long_segments(segments, max_seconds=0) == segments
