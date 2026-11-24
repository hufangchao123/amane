"""avsubtitles 来源的解析与抓取用例: 全部喂假页面, 不连站点."""

from __future__ import annotations

import pytest

from amane.net import FailureKind, RequestError, RequestFailure
from amane.subtitles import SubtitleSourceError, fetch_subtitle
from amane.subtitles.avsubtitles import (
    MovieCandidate,
    SubtitleLink,
    download_urls,
    parse_revision,
    parse_search,
    parse_subtitles,
    pick_subtitle,
)
from tests.subtitles.support import SUBTITLE_BODY, FakeHttp

MOVIE_URL = "https://www.avsubtitles.com/movie1/ad-1--x-2020"
DETAIL_URL = "https://www.avsubtitles.com/movie1/ad-1--x-2020/subtitles/zh/44165"

SEARCH_HTML = """
<div class="card">
  <a href="/movie1/ad-1--x-2020">[AD-1] 标题 - (2020)</a>
  <a href="/movie2/ad-1-prequel">[AD-1] 另一部 - (2019)</a>
  <a href="/movie3/zzz-9">[ZZZ-9] 不相干</a>
</div>
"""

MOVIE_HTML = """
<table>
  <tr><td>Japanese</td><td><a href="/movie1/ad-1--x-2020/subtitles/ja/139234">详情</a></td></tr>
  <tr><td>Korean</td><td><a href="/movie1/ad-1--x-2020/subtitles/ko/59223">详情</a></td></tr>
  <tr><td>Chinese</td><td><a href="/movie1/ad-1--x-2020/subtitles/zh/44165">详情</a></td></tr>
</table>
"""

DETAIL_HTML = """
<form method="get" target="_blank" action="/download_page.php">
  <input type="hidden" name="subid" value="44165" />
  <input type="hidden" name="revid" value="20250702110559" />
  <input type="submit" value="Download subtitles" />
</form>
"""


def _config(providers: list[str] | None = None, languages: list[str] | None = None, max_attempts: int = 1):
    from amane.config.manager import SubtitleFetchConfig

    return SubtitleFetchConfig(
        enabled=True,
        providers=providers or ["avsubtitles"],
        languages=languages or ["zh-CN", "zh-TW"],
        max_attempts=max_attempts,
    )


class TestParse:
    def test_search_keeps_matches_in_page_order(self):
        """番号对不上的影片必须丢掉: 站点搜索是全文匹配, 会带回预告片与同系列."""
        assert parse_search(SEARCH_HTML, "AD-1") == [
            MovieCandidate(number="AD-1", title="[AD-1] 标题 - (2020)", url=MOVIE_URL),
            MovieCandidate(
                number="AD-1", title="[AD-1] 另一部 - (2019)", url="https://www.avsubtitles.com/movie2/ad-1-prequel"
            ),
        ]

    def test_search_ignores_other_numbers(self):
        assert [c.url for c in parse_search(SEARCH_HTML, "ZZZ-9")] == ["https://www.avsubtitles.com/movie3/zzz-9"]

    def test_subtitles_keep_language_and_id(self):
        assert parse_subtitles(MOVIE_HTML) == [
            SubtitleLink(
                language="ja",
                sub_id="139234",
                url="https://www.avsubtitles.com/movie1/ad-1--x-2020/subtitles/ja/139234",
            ),
            SubtitleLink(
                language="ko", sub_id="59223", url="https://www.avsubtitles.com/movie1/ad-1--x-2020/subtitles/ko/59223"
            ),
            SubtitleLink(language="zh", sub_id="44165", url=DETAIL_URL),
        ]

    @pytest.mark.parametrize("languages", [["zh-CN", "zh-TW"], ["zh-TW"], ["zh-Hans"]])
    def test_pick_subtitle_maps_chinese_onto_zh(self, languages: list[str]):
        """站点只有 ``zh`` 一个中文段, 三种中文语言码都要挑到它."""
        link = pick_subtitle(parse_subtitles(MOVIE_HTML), languages)
        assert link is not None
        assert link.sub_id == "44165"

    def test_pick_subtitle_returns_none_without_chinese(self):
        """只有日文与韩文时不下手: 本包补的是中文字幕."""
        html = '<a href="/movie1/ad-1/subtitles/ja/1">日</a><a href="/movie1/ad-1/subtitles/ko/2">韩</a>'
        assert pick_subtitle(parse_subtitles(html), ["zh-CN"]) is None

    def test_parse_revision(self):
        assert parse_revision(DETAIL_HTML) == "20250702110559"

    def test_parse_revision_without_form(self):
        assert parse_revision("<html>登录后可见</html>") is None

    def test_download_urls_carry_both_ids(self):
        confirm, file = download_urls("44165", "20250702110559")
        assert confirm == "https://www.avsubtitles.com/download_page.php?subid=44165&revid=20250702110559"
        assert file == "https://www.avsubtitles.com/download_sub.php?subid=44165&revid=20250702110559"


class TestFetch:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_fetch_walks_search_movie_detail_confirm_then_download(self, public_dns: None):
        web = FakeHttp(
            texts={
                "https://www.avsubtitles.com/search_results.php?search=AD-1": SEARCH_HTML,
                # 详情页地址以影片页地址开头, 假通道按前缀匹配, 长前缀必须排在前面
                DETAIL_URL: DETAIL_HTML,
                "https://www.avsubtitles.com/download_page.php?subid=44165": "<html>确认页</html>",
                MOVIE_URL: MOVIE_HTML,
            }
        )
        download = await fetch_subtitle(web, _config(), "AD-1")

        assert download.content == SUBTITLE_BODY
        assert download.suffix == ".srt"
        assert download.provider == "avsubtitles"
        # 确认页必须先走: 下载接口凭它下发的会话 cookie 放行, 少了只有 302 到 error_403
        assert web.calls == [
            "https://www.avsubtitles.com/search_results.php?search=AD-1",
            MOVIE_URL,
            DETAIL_URL,
            "https://www.avsubtitles.com/download_page.php?subid=44165&revid=20250702110559",
        ]
        assert [url for url, _ in web.download_calls] == [
            "https://www.avsubtitles.com/download_sub.php?subid=44165&revid=20250702110559"
        ]
        # 下载要带字节上限, 否则来源可以借字幕通道塞任意大文件
        assert web.download_calls[0][1] is not None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_fetch_reports_missing_result(self, public_dns: None):
        web = FakeHttp(texts={"https://www.avsubtitles.com/search_results.php?search=AD-1": "<html>没有结果</html>"})
        with pytest.raises(SubtitleSourceError) as excinfo:
            await fetch_subtitle(web, _config(), "AD-1")
        assert excinfo.value.transient is False
        assert "未搜到" in str(excinfo.value)
        assert web.download_calls == []

    @pytest.mark.asyncio(loop_scope="function")
    async def test_fetch_reports_missing_subtitle(self, public_dns: None):
        web = FakeHttp(
            texts={
                "https://www.avsubtitles.com/search_results.php?search=AD-1": SEARCH_HTML,
                MOVIE_URL: '<a href="/movie1/ad-1--x-2020/subtitles/ja/139234">日</a>',
            }
        )
        with pytest.raises(SubtitleSourceError) as excinfo:
            await fetch_subtitle(web, _config(), "AD-1")
        assert excinfo.value.transient is False
        assert "没有 AD-1 的中文字幕" in str(excinfo.value)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_fetch_reports_missing_revision(self, public_dns: None):
        web = FakeHttp(
            texts={
                "https://www.avsubtitles.com/search_results.php?search=AD-1": SEARCH_HTML,
                DETAIL_URL: "<html>结构变了</html>",
                MOVIE_URL: MOVIE_HTML,
            }
        )
        with pytest.raises(SubtitleSourceError) as excinfo:
            await fetch_subtitle(web, _config(), "AD-1")
        assert "没有 revid" in str(excinfo.value)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_fetch_marks_timeout_transient(self, public_dns: None):
        web = FakeHttp(
            texts={
                "https://www.avsubtitles.com/search_results.php?search=AD-1": SEARCH_HTML,
                # 详情页地址以影片页地址开头, 假通道按前缀匹配, 长前缀必须排在前面
                DETAIL_URL: DETAIL_HTML,
                "https://www.avsubtitles.com/download_page.php?subid=44165": "<html>确认页</html>",
                MOVIE_URL: MOVIE_HTML,
            },
            transient_failures={"https://www.avsubtitles.com/search_results.php": 1},
        )
        download = await fetch_subtitle(web, _config(max_attempts=2), "AD-1")
        assert download.provider == "avsubtitles"


class _SearchRejects(FakeHttp):
    async def get_text(self, url: str, *, headers: dict[str, str] | None = None, use_proxy: bool = True) -> str:
        self.calls.append(url)
        raise RequestError(url, RequestFailure(kind=FailureKind.HTTP_STATUS, message="404 not found", status=404))


class TestTransientFlags:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_http_404_is_not_transient(self, public_dns: None):
        """404 重试多少次都是同一个答案."""
        web = _SearchRejects()
        with pytest.raises(SubtitleSourceError) as excinfo:
            await fetch_subtitle(web, _config(max_attempts=3), "AD-1")
        assert excinfo.value.transient is False
        assert len(web.calls) == 1
