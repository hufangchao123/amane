"""出站校验与下载上限: 外链来自第三方页面, 不能让 amane 顺着它读内网."""

from __future__ import annotations

import pytest

from amane.net.errors import EgressRejected
from amane.subtitles import SubtitleSourceError
from amane.subtitles.models import MAX_SUBTITLE_BYTES
from amane.subtitles.transport import download_subtitle, ensure_public_url
from tests.subtitles.support import SUBTITLE_BODY, FakeHttp

REJECTED_URLS = [
    "file:///etc/passwd",
    "http://127.0.0.1/subtitle.srt",
    "http://169.254.169.254/latest/meta-data/",
]


class TestEgress:
    @pytest.mark.parametrize("url", REJECTED_URLS)
    @pytest.mark.asyncio(loop_scope="function")
    async def test_ensure_public_url_rejects(self, url: str):
        with pytest.raises(EgressRejected):
            await ensure_public_url(url)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_public_url_is_allowed(self, public_dns: None):
        """公网地址照常放行: 校验不能宽到把正常来源也挡掉."""
        await ensure_public_url("https://cdn.example.com/AD-1.srt")

    @pytest.mark.parametrize("url", REJECTED_URLS)
    @pytest.mark.asyncio(loop_scope="function")
    async def test_download_rejects_before_touching_network(self, url: str):
        """被拒的地址一个字节都不下载: 报错文案要能被用户看懂, 且给出拒绝原因."""
        web = FakeHttp()
        with pytest.raises(SubtitleSourceError) as excinfo:
            await download_subtitle(web, url, provider="assrt", description="AD-1")
        assert "下载地址被拒绝" in str(excinfo.value)
        assert excinfo.value.transient is False
        assert web.download_calls == []


class TestSizeLimit:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_download_always_carries_byte_cap(self, public_dns: None):
        web = FakeHttp()
        await download_subtitle(web, "https://cdn.example.com/AD-1.srt", provider="assrt", description="AD-1")
        assert web.download_calls[0][1] == MAX_SUBTITLE_BYTES

    @pytest.mark.asyncio(loop_scope="function")
    async def test_oversized_payload_is_rejected(self, public_dns: None):
        web = FakeHttp(payload=b"1\n" + b"x" * (MAX_SUBTITLE_BYTES + 1))
        with pytest.raises(SubtitleSourceError) as excinfo:
            await download_subtitle(web, "https://cdn.example.com/AD-1.srt", provider="assrt", description="AD-1")
        assert "大小上限" in str(excinfo.value)
        assert excinfo.value.transient is False

    @pytest.mark.asyncio(loop_scope="function")
    async def test_failed_transfer_is_transient(self, public_dns: None):
        """传输中断值得用 ``max_attempts`` 重试, 因此必须标成临时失败."""
        web = FakeHttp(download_ok=False)
        with pytest.raises(SubtitleSourceError) as excinfo:
            await download_subtitle(web, "https://cdn.example.com/AD-1.srt", provider="assrt", description="AD-1")
        assert excinfo.value.transient is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_downloaded_bytes_are_returned(self, public_dns: None):
        web = FakeHttp()
        download = await download_subtitle(
            web,
            "https://cdn.example.com/AD-1.srt",
            provider="subtitlecat",
            description="AD-1 ← 中文字幕",
        )
        assert download.content == SUBTITLE_BODY
        assert download.suffix == ".srt"
        assert download.provider == "subtitlecat"
