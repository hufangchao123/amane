"""/subtitles 与字幕任务的接口约束."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx2 import AsyncClient


class TestSubtitleTask:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_invalid_payload_is_422(self, client: AsyncClient, stop_worker: None):
        bad_media = await client.post("tasks", json={"type": "subtitle_fetch", "media_file_id": "abc"})
        assert bad_media.status_code == 422
        bad_library = await client.post("tasks", json={"type": "subtitle_fetch", "library_id": "abc"})
        assert bad_library.status_code == 422

    @pytest.mark.asyncio(loop_scope="function")
    async def test_valid_payloads_are_accepted(self, client: AsyncClient, stop_worker: None):
        """两个字段都可空; 都空表示全库按缺口挑, 因此空 payload 也必须被接受."""
        empty = await client.post("tasks", json={"type": "subtitle_fetch"})
        assert empty.status_code == 202
        by_library = await client.post("tasks", json={"type": "subtitle_fetch", "library_id": 1})
        assert by_library.status_code == 202
        assert by_library.json()["payload"] == {"media_file_id": None, "library_id": 1, "limit": 50}

        with_limit = await client.post("tasks", json={"type": "subtitle_fetch", "library_id": 1, "limit": 200})
        assert with_limit.status_code == 202
        assert with_limit.json()["payload"] == {"media_file_id": None, "library_id": 1, "limit": 200}


class TestGenerationProbe:
    """转写端点的探测接口: 只报告能不能用, 不改配置也不入任务表."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_reports_disabled_when_generation_is_off(self, client: AsyncClient):
        response = await client.post("subtitles/generation/test")

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert "未启用" in body["detail"]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_reports_missing_endpoint(self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
        await client.patch("config", json={"subtitles": {"generation": {"enabled": True, "base_url": ""}}})

        response = await client.post("subtitles/generation/test")

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert "没有配置转写端点" in body["detail"]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_silent_probe_counts_as_reachable(self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
        """静音没有分段: 端点答了 200 就算通, 不能报成不可用."""
        from amane.api.routes import subtitles as routes
        from amane.subtitles.whisper import WhisperError

        async def fake_transcribe(*args, **kwargs):
            raise WhisperError("转写端点没有返回分段 (response_format 需要 verbose_json)")

        monkeypatch.setattr(routes, "transcribe", fake_transcribe)
        await client.patch(
            "config",
            json={"subtitles": {"generation": {"enabled": True, "base_url": "http://whisper.local/v1"}}},
        )

        response = await client.post("subtitles/generation/test")

        body = response.json()
        assert body["ok"] is True
        assert "静音探测" in body["detail"]
        assert body["latency_ms"] >= 0
