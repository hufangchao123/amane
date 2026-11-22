"""来源解析与抓取的用例: 全部喂假应答, 不连 assrt / subtitlecat."""

from __future__ import annotations

import pytest

from amane.config.manager import SubtitleProvider
from amane.net import FailureKind, RequestError, RequestFailure
from amane.subtitles import SubtitleSourceError, fetch_subtitle
from amane.subtitles.assrt import AssrtCandidate, parse_detail, parse_search, rank_candidates
from amane.subtitles.models import language_tags, request_error_message
from amane.subtitles.subtitlecat import parse_search as parse_subtitlecat_search
from amane.subtitles.subtitlecat import pick_download_path
from tests.subtitles.support import SUBTITLE_BODY, FakeHttp


class TestAssrtParse:
    def test_search_accepts_both_shapes(self):
        """assrt 的 ``sub`` 有时是对象、有时再包一层 ``subs``; 两种都要能读."""
        flat = {"sub": {"subs": [{"id": 11, "videoname": "AD-1", "title": "中文字幕"}]}}
        nested = {"sub": {"subs": {"subs": [{"id": 12, "videoname": "AD-2"}]}}}
        assert parse_search(flat) == [AssrtCandidate(sub_id=11, title="AD-1 / 中文字幕")]
        assert parse_search(nested) == [AssrtCandidate(sub_id=12, title="AD-2")]

    def test_search_skips_malformed_entries(self):
        """缺 id 或用字符串 id 的条目直接丢, 不让脏数据把整轮抓取打断."""
        payload = {"sub": {"subs": [{"id": "13"}, {"title": "无 id"}, {"id": 14, "native_name": "中"}]}}
        assert [c.sub_id for c in parse_search(payload)] == [14]

    def test_search_tolerates_unexpected_payload(self):
        assert parse_search({"status": 200}) == []
        assert parse_search(None) == []

    def test_detail_picks_first_url(self):
        payload = {"sub": {"subs": [{"filelist": [{"f": "a.srt"}, {"url": "https://x/AD-1.srt", "f": "b.srt"}]}]}}
        picked = parse_detail(payload)
        assert picked is not None
        assert picked.url == "https://x/AD-1.srt"
        assert picked.name == "b.srt"

    def test_detail_without_filelist(self):
        assert parse_detail({"sub": {"subs": [{}]}}) is None

    def test_rank_prefers_number_match_then_language(self):
        tags = language_tags(["zh-CN", "zh-TW"])
        candidates = [
            AssrtCandidate(sub_id=1, title="OTHER-9 english"),
            AssrtCandidate(sub_id=2, title="AD-1 繁体字幕"),
            AssrtCandidate(sub_id=3, title="AD-1 简体字幕"),
        ]
        # 有候选命中番号时, 不含番号的整条丢掉: 来源的模糊结果多半是别的片子.
        assert [c.sub_id for c in rank_candidates(candidates, "AD-1", tags)] == [3, 2]

    @pytest.mark.parametrize(
        ("languages", "expected"),
        [
            (["zh-CN", "zh-TW"], 1),
            (["zh-TW", "zh-CN"], 2),
        ],
    )
    def test_rank_follows_configured_language_order(self, languages: list[str], expected: int):
        """多语言候选按配置顺序排, 不写死简体优先: 配了繁体优先就该先取繁体那份."""
        candidates = [
            AssrtCandidate(sub_id=1, title="AD-1 简体中字"),
            AssrtCandidate(sub_id=2, title="AD-1 繁体中字"),
        ]
        ranked = rank_candidates(candidates, "AD-1", language_tags(languages))
        assert ranked[0].sub_id == expected

    def test_rank_keeps_all_when_nothing_matches(self):
        """一条都不含番号时保留全部, 否则整个来源等于白跑."""
        candidates = [AssrtCandidate(sub_id=1, title="某字幕组 合集")]
        assert [c.sub_id for c in rank_candidates(candidates, "AD-1", language_tags(["zh-CN"]))] == [1]


class TestAssrtFetch:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_fetch_downloads_first_ranked_candidate(self, public_dns: None):
        web = FakeHttp(
            jsons={
                "https://api.assrt.net/v1/sub/search": {
                    "sub": {"subs": [{"id": 5, "videoname": "OTHER-9"}, {"id": 6, "videoname": "AD-1 简体"}]}
                },
                "https://api.assrt.net/v1/sub/detail?id=6": {
                    "sub": {"subs": [{"filelist": [{"url": "https://cdn.example.com/AD-1.srt", "f": "AD-1.srt"}]}]}
                },
            }
        )
        download = await fetch_subtitle(web, _config(providers=["assrt"], assrt_token="tok"), "AD-1")

        assert download.content == SUBTITLE_BODY
        assert download.suffix == ".srt"
        assert download.provider == "assrt"
        # 含番号的候选排在前面, 因此先查 id=6, 且没有去查 id=5
        assert web.calls == [
            "https://api.assrt.net/v1/sub/search?q=AD-1&token=tok",
            "https://api.assrt.net/v1/sub/detail?id=6&token=tok",
        ]
        assert [url for url, _ in web.download_calls] == ["https://cdn.example.com/AD-1.srt"]
        # 下载必须带字节上限, 否则来源可以借字幕通道塞任意大文件
        assert web.download_calls[0][1] is not None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_fetch_reports_missing_result(self, public_dns: None):
        web = FakeHttp(jsons={"https://api.assrt.net/v1/sub/search": {"sub": {"subs": []}}})
        with pytest.raises(SubtitleSourceError) as excinfo:
            await fetch_subtitle(web, _config(providers=["assrt"], assrt_token="tok"), "AD-1")
        assert excinfo.value.transient is False
        assert web.download_calls == []

    @pytest.mark.asyncio(loop_scope="function")
    async def test_fetch_marks_transient_on_timeout(self, public_dns: None):
        web = FakeHttp(
            jsons={
                "https://api.assrt.net/v1/sub/search": {"sub": {"subs": [{"id": 6, "videoname": "AD-1"}]}},
                "https://api.assrt.net/v1/sub/detail?id=6": {
                    "sub": {"subs": [{"filelist": [{"url": "https://cdn.example.com/AD-1.srt"}]}]}
                },
            },
            transient_failures={"https://api.assrt.net/v1/sub/search": 1},
        )
        download = await fetch_subtitle(web, _config(providers=["assrt"], assrt_token="tok", max_attempts=2), "AD-1")
        assert download.provider == "assrt"
        assert len(web.calls) == 3


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
        download = await fetch_subtitle(web, _config(providers=["subtitlecat", "assrt"], assrt_token="tok"), "AD-1")
        assert download.provider == "subtitlecat"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_assrt_without_token_is_skipped(self, public_dns: None, subtitlecat_pages: dict[str, str]):
        """缺 token 的 assrt 不参与, 也不该被当作失败: 直接落到 subtitlecat."""
        web = FakeHttp(texts=subtitlecat_pages)
        download = await fetch_subtitle(web, _config(providers=["assrt", "subtitlecat"]), "AD-1")
        assert download.provider == "subtitlecat"
        assert all("api.assrt.net" not in url for url in web.calls)

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


def _config(*, providers: list[SubtitleProvider], assrt_token: str | None = None, max_attempts: int = 1):
    from amane.config.manager import SubtitleFetchConfig

    return SubtitleFetchConfig(
        enabled=True,
        assrt_token=assrt_token,
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
"""假 token: 用例只验证它不出现在任何文案里, 仓库里不留真实凭据."""


class _LeakingHttp(FakeHttp):
    """来源失败时把整条带 token 的地址抄进 ``failure.message``, 这是上游真实会有的写法."""

    async def get_json(self, url: str, *, headers: dict[str, str] | None = None, use_proxy: bool = True) -> object:
        self.calls.append(url)
        raise RequestError(
            url,
            RequestFailure(kind=FailureKind.HTTP_STATUS, status=503, message=f"503 for url {url}"),
        )


class TestTokenRedaction:
    """assrt 的 token 只能走查询串, 因此文案里一旦抄了地址就等于泄露凭据."""

    def test_message_redacts_url_copied_into_failure(self):
        url = f"https://api.assrt.net/v1/sub/search?q=AD-1&token={SECRET_TOKEN}"
        exc = RequestError(url, RequestFailure(kind=FailureKind.HTTP_STATUS, status=503, message=f"503 for url {url}"))

        message = request_error_message("assrt 搜索失败", exc)

        assert SECRET_TOKEN not in message
        assert "token=***" in message

    def test_message_keeps_text_without_url(self):
        """脱敏只动地址: 没有地址的文案要保持原样, 便于按原文排查."""
        exc = RequestError(
            "https://api.assrt.net/v1/sub/search", RequestFailure(kind=FailureKind.TIMEOUT, message="timeout")
        )

        assert request_error_message("assrt 搜索失败", exc) == "assrt 搜索失败: timeout"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_fetch_failure_message_never_carries_token(self, public_dns: None):
        web = _LeakingHttp()

        with pytest.raises(SubtitleSourceError) as excinfo:
            await fetch_subtitle(web, _config(providers=["assrt"], assrt_token=SECRET_TOKEN), "AD-1")

        assert SECRET_TOKEN not in str(excinfo.value)
        assert "token=***" in str(excinfo.value)
        assert excinfo.value.transient is True, "503 是临时失败, 下一轮还会再试"
