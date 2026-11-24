"""来源解析与抓取的用例: 全部喂假应答, 不连 subtitlecat / avsubtitles."""

from __future__ import annotations

import pytest

from amane.config.manager import SubtitleProvider
from amane.net import FailureKind, RequestError, RequestFailure
from amane.subtitles import SubtitleSourceError, fetch_subtitle
from amane.subtitles.models import language_tags, request_error_message
from amane.subtitles.subtitlecat import parse_search as parse_subtitlecat_search
from amane.subtitles.subtitlecat import pick_download_path
from tests.subtitles.support import SUBTITLE_BODY, FakeHttp


class TestSubtitlecatParse:
    def test_search_keeps_only_number_matches(self):
        html = (
            '<a href="subs/1/ad-1-chinese.html">AD-1 中文字幕</a>'
            '<a href="subs/2/other-9-chinese.html">OTHER-9 中文字幕</a>'
        )
        candidates = parse_subtitlecat_search(html, "AD-1", language_tags(["zh-CN"]))
        assert [c.detail_url for c in candidates] == ["https://www.subtitlecat.com/subs/1/ad-1-chinese.html"]

    @pytest.mark.parametrize(
        ("languages", "expected"),
        [
            (["zh-CN", "zh-TW"], "/subs/1/ad-1-zh-CN.srt"),
            (["zh-TW", "zh-CN"], "/subs/1/ad-1-zh-TW.srt"),
        ],
    )
    def test_pick_download_path_follows_configured_order(self, languages: list[str], expected: str):
        """语言偏好由配置顺序决定, 不写死简体优先."""
        html = '<a href="/subs/1/ad-1-zh-TW.srt">繁</a><a href="/subs/1/ad-1-zh-CN.srt">简</a>'
        assert pick_download_path(html, language_tags(languages)) == expected

    def test_pick_download_path_falls_back_to_unmarked_file(self):
        html = '<a href="/subs/1/ad-1.srt">字幕</a>'
        assert pick_download_path(html, language_tags(["zh-CN"])) == "/subs/1/ad-1.srt"

    def test_pick_download_path_without_candidates(self):
        assert pick_download_path("<html></html>", language_tags(["zh-CN"])) is None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_fetch_walks_search_then_detail(self, public_dns: None, subtitlecat_pages: dict[str, str]):
        web = FakeHttp(texts=subtitlecat_pages)
        download = await fetch_subtitle(web, _config(providers=["subtitlecat"]), "AD-1")

        assert download.provider == "subtitlecat"
        assert download.suffix == ".srt"
        assert download.content == SUBTITLE_BODY
        assert [url for url, _ in web.download_calls] == ["https://www.subtitlecat.com/subs/100/ad-1-zh-CN.srt"]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_fetch_without_candidate_is_not_transient(self, public_dns: None):
        web = FakeHttp(texts={"https://www.subtitlecat.com/index.php?search=AD-1": "<html></html>"})
        with pytest.raises(SubtitleSourceError) as excinfo:
            await fetch_subtitle(web, _config(providers=["subtitlecat"]), "AD-1")
        assert excinfo.value.transient is False


class TestFetchSubtitleOrdering:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_providers_are_tried_in_configured_order(self, public_dns: None, subtitlecat_pages: dict[str, str]):
        """``providers`` 的顺序就是兜底顺序: 前一个来源查不到才轮到下一个."""
        web = FakeHttp(texts=subtitlecat_pages)
        download = await fetch_subtitle(web, _config(providers=["subtitlecat", "avsubtitles"]), "AD-1")
        assert download.provider == "subtitlecat"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_first_provider_success_skips_the_second(self, public_dns: None, subtitlecat_pages: dict[str, str]):
        """第一家拿到就返回, 不为了"更保险"再问第二家."""
        web = FakeHttp(texts=subtitlecat_pages)
        download = await fetch_subtitle(web, _config(providers=["subtitlecat", "avsubtitles"]), "AD-1")
        assert download.provider == "subtitlecat"
        assert all("avsubtitles.com" not in url for url in web.calls)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_retries_stop_at_max_attempts(self, public_dns: None):
        """临时失败按 ``max_attempts`` 计次, 用完即止, 不无限重试."""
        web = FakeHttp(
            texts={"https://www.subtitlecat.com/index.php?search=": "<html></html>"},
            transient_failures={"https://www.subtitlecat.com/index.php?search=": 100},
        )
        with pytest.raises(SubtitleSourceError) as excinfo:
            await fetch_subtitle(web, _config(providers=["subtitlecat"], max_attempts=2), "AD-1")
        assert excinfo.value.transient is True
        assert len(web.calls) == 2

    @pytest.mark.asyncio(loop_scope="function")
    async def test_non_transient_error_is_not_retried(self, public_dns: None):
        web = _RejectingHttp()
        with pytest.raises(SubtitleSourceError) as excinfo:
            await fetch_subtitle(web, _config(providers=["subtitlecat"], max_attempts=3), "AD-1")
        assert excinfo.value.transient is False
        assert len(web.calls) == 1


def _config(*, providers: list[SubtitleProvider], max_attempts: int = 1):
    from amane.config.manager import SubtitleFetchConfig

    return SubtitleFetchConfig(
        enabled=True,
        providers=providers,
        languages=["zh-CN", "zh-TW"],
        max_attempts=max_attempts,
    )


class _RejectingHttp(FakeHttp):
    """来源回 404: HTTP 状态类错误不重试."""

    async def get_text(self, url: str, *, headers: dict[str, str] | None = None, use_proxy: bool = True) -> str:
        self.calls.append(url)
        raise RequestError(url, RequestFailure(kind=FailureKind.HTTP_STATUS, message="404 not found", status=404))


SECRET_TOKEN = "SECRET-TOKEN-3f9a"
"""假凭据: 用例只验证它不出现在任何文案里, 仓库里不留真实 token."""


class TestUrlRedaction:
    """失败文案常把整条请求地址抄进来; 地址里可能带凭据, 一律先脱敏再进结果."""

    def test_message_redacts_url_copied_into_failure(self):
        url = f"https://sub.example.com/search?q=AD-1&api_key={SECRET_TOKEN}"
        exc = RequestError(url, RequestFailure(kind=FailureKind.HTTP_STATUS, status=503, message=f"503 for url {url}"))

        message = request_error_message("搜索失败", exc)

        assert SECRET_TOKEN not in message
        assert SECRET_TOKEN not in message
        assert "api_key=***" in message

    def test_message_keeps_text_without_url(self):
        """脱敏只动地址: 没有地址的文案要保持原样, 便于按原文排查."""
        exc = RequestError(
            "https://sub.example.com/search", RequestFailure(kind=FailureKind.TIMEOUT, message="timeout")
        )

        assert request_error_message("搜索失败", exc) == "搜索失败: timeout"
