"""``SUBTITLE_GENERATE`` 的编排: 前置校验、跳过口径与落盘结果, 真站与 ffmpeg 都用替身."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from amane.config.manager import HotSettings, LLMConfig, SubtitleFetchConfig, SubtitleGenerateConfig
from amane.handlers import subtitle_generate as module
from amane.handlers.models import SubtitleGeneratePayload
from amane.handlers.subtitle_generate import MACHINE_MARK, SubtitleGenerateHandler
from amane.subtitles.translate import TranslationOutcome
from amane.subtitles.whisper import Segment
from tests.subtitles.support import seed_media

if TYPE_CHECKING:
    from pathlib import Path

    from amane.db.repository import Repository


def _config(
    *,
    enabled: bool = True,
    base_url: str = "http://whisper.local/v1",
    translate: bool = True,
    max_file_minutes: int = 180,
) -> SubtitleFetchConfig:
    return SubtitleFetchConfig(
        enabled=True,
        generation=SubtitleGenerateConfig(
            enabled=enabled,
            base_url=base_url,
            translate=translate,
            max_file_minutes=max_file_minutes,
        ),
    )


def _hot(config: SubtitleFetchConfig, *, llm_key: str | None = "sk-test") -> HotSettings:
    return HotSettings(subtitles=config, llm=LLMConfig(enabled=llm_key is not None, api_key=llm_key, model="m"))


def _stub_pipeline(monkeypatch: pytest.MonkeyPatch, *, texts: list[str] | None = None) -> list[str]:
    """顶掉 ffmpeg、转写与翻译, 记下转写到的音轨名."""
    seen: list[str] = []

    monkeypatch.setattr(module, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")

    async def fake_extract(video: Path, dest: Path, *, timeout: float) -> None:
        seen.append(video.name)
        dest.write_bytes(b"RIFF")

    async def fake_transcribe(client: object, **kwargs: object) -> list[Segment]:
        return [Segment(0.0, 1.0, "こんにちは"), Segment(1.0, 2.0, "さようなら")]

    async def fake_translate(model: object, lines: object, **kwargs: object) -> TranslationOutcome:
        got = texts if texts is not None else ["你好", "再见"]
        return TranslationOutcome(lines=got, failed_batches=0, total_batches=1)

    monkeypatch.setattr(module, "extract_audio", fake_extract)
    monkeypatch.setattr(module, "transcribe", fake_transcribe)
    monkeypatch.setattr(module, "translate_lines", fake_translate)
    monkeypatch.setattr(module, "build_model", lambda *args, **kwargs: object())
    return seen


class TestPreconditions:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_disabled_returns_error(self, repo: Repository) -> None:
        handler = SubtitleGenerateHandler(repo, _hot(_config(enabled=False)))

        result = await handler.handle(SubtitleGeneratePayload())

        assert result.success is False
        assert result.error == "转写生成字幕未启用"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_missing_endpoint_returns_error(self, repo: Repository) -> None:
        handler = SubtitleGenerateHandler(repo, _hot(_config(base_url="")))

        result = await handler.handle(SubtitleGeneratePayload())

        assert result.success is False
        assert result.error == "没有配置转写端点"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_translate_without_llm_key_returns_error(
        self, repo: Repository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(module, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
        handler = SubtitleGenerateHandler(repo, _hot(_config(translate=True), llm_key=None))

        result = await handler.handle(SubtitleGeneratePayload())

        assert result.success is False
        assert "api_key" in (result.error or "")

    @pytest.mark.asyncio(loop_scope="function")
    async def test_missing_ffmpeg_returns_error(self, repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(module, "ffmpeg_path", lambda: None)
        handler = SubtitleGenerateHandler(repo, _hot(_config(translate=False)))

        result = await handler.handle(SubtitleGeneratePayload())

        assert result.success is False
        assert "ffmpeg" in (result.error or "")


class TestSkipRules:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_multi_disc_is_skipped(
        self, repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _stub_pipeline(monkeypatch)
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root / "AD-1-CD2", number="AD-1")
        assert media.id is not None
        handler = SubtitleGenerateHandler(repo, _hot(_config()))

        result = await handler.handle(SubtitleGeneratePayload(media_file_id=media.id))

        assert result.result is not None
        assert (result.result.generated, result.result.skipped) == (0, 1)
        assert seen == []

    @pytest.mark.asyncio(loop_scope="function")
    async def test_pointer_file_is_skipped(
        self, repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _stub_pipeline(monkeypatch)
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root, number="AD-1", suffix=".strm")
        assert media.id is not None
        handler = SubtitleGenerateHandler(repo, _hot(_config()))

        result = await handler.handle(SubtitleGeneratePayload(media_file_id=media.id))

        assert result.result is not None
        assert (result.result.generated, result.result.skipped) == (0, 1)
        assert seen == []

    @pytest.mark.asyncio(loop_scope="function")
    async def test_existing_subtitle_is_skipped(
        self, repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _stub_pipeline(monkeypatch)
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root, number="AD-1", subtitle="AD-1.zh.srt")
        assert media.id is not None
        handler = SubtitleGenerateHandler(repo, _hot(_config()))

        result = await handler.handle(SubtitleGeneratePayload(media_file_id=media.id))

        assert result.result is not None
        assert result.result.skipped == 1
        assert seen == []


class TestGeneration:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_writes_translated_subtitle_next_to_video(
        self, repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _stub_pipeline(monkeypatch)
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root, number="AD-1")
        assert media.id is not None
        handler = SubtitleGenerateHandler(repo, _hot(_config()))

        result = await handler.handle(SubtitleGeneratePayload(media_file_id=media.id))

        assert result.success is True
        assert result.result is not None
        assert (result.result.generated, result.result.translated, result.result.failed) == (1, 1, 0)
        assert seen == ["AD-1.mp4"]
        subtitle = root / "AD-1.zh.srt"
        body = subtitle.read_text(encoding="utf-8")
        assert MACHINE_MARK in body
        assert "你好" in body and "再见" in body
        assert "00:00:01,000 --> 00:00:02,000" in body
        fresh = await repo.get_media_file(media.id)
        assert fresh is not None
        assert fresh.has_external_subtitle is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_untranslated_run_uses_language_tag(
        self, repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_pipeline(monkeypatch)
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root, number="AD-2")
        assert media.id is not None
        handler = SubtitleGenerateHandler(repo, _hot(_config(translate=False)))

        result = await handler.handle(SubtitleGeneratePayload(media_file_id=media.id))

        assert result.result is not None
        assert result.result.translated == 0
        assert (root / "AD-2.ja.srt").exists()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_untranslated_run_writes_nothing(
        self, repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """端点整批不可用时不能落一份日文字幕冒充中文."""
        _stub_pipeline(monkeypatch)

        async def dead_translate(model: object, lines: object, **kwargs: object) -> object:
            from amane.subtitles.translate import TranslationOutcome

            return TranslationOutcome(lines=list(lines), failed_batches=1, total_batches=1, request_failures=1)

        monkeypatch.setattr(module, "translate_lines", dead_translate)
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root, number="AD-1")
        assert media.id is not None
        handler = SubtitleGenerateHandler(repo, _hot(_config()))

        result = await handler.handle(SubtitleGeneratePayload(media_file_id=media.id))

        assert result.result is not None
        assert (result.result.generated, result.result.failed) == (0, 1)
        assert not list(root.glob("*.srt"))

    @pytest.mark.asyncio(loop_scope="function")
    async def test_short_translation_still_writes(
        self, repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """模型少回一行只是它自己的毛病: 端点可用就按原文补齐并落盘, 不算失败."""
        _stub_pipeline(monkeypatch)

        async def short_translate(model: object, lines: list[str], **kwargs: object) -> object:
            from amane.subtitles.translate import TranslationOutcome

            padded = [*lines[:-1], "补位"] if lines else []
            return TranslationOutcome(lines=padded, failed_batches=1, total_batches=1, request_failures=0)

        monkeypatch.setattr(module, "translate_lines", short_translate)
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root, number="AD-1")
        assert media.id is not None
        handler = SubtitleGenerateHandler(repo, _hot(_config()))

        result = await handler.handle(SubtitleGeneratePayload(media_file_id=media.id))

        assert result.result is not None
        assert (result.result.generated, result.result.failed) == (1, 0)
        assert (root / "AD-1.zh.srt").exists()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_transcription_failure_counts_as_failed(
        self, repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(module, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")

        async def fake_extract(video: Path, dest: Path, *, timeout: float) -> None:
            dest.write_bytes(b"RIFF")

        async def failing_transcribe(client: object, **kwargs: object) -> list[Segment]:
            raise module.WhisperError("转写端点不可达", transient=True)

        monkeypatch.setattr(module, "extract_audio", fake_extract)
        monkeypatch.setattr(module, "transcribe", failing_transcribe)
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root, number="AD-3")
        assert media.id is not None
        handler = SubtitleGenerateHandler(repo, _hot(_config(translate=False)))

        result = await handler.handle(SubtitleGeneratePayload(media_file_id=media.id))

        assert result.success is True
        assert result.result is not None
        assert (result.result.generated, result.result.failed) == (0, 1)
        assert not (root / "AD-3.ja.srt").exists()
