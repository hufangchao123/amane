"""字幕用例的夹具: 假应答页面与只开一个来源的配置."""

from __future__ import annotations

import pytest

from amane.config.manager import SubtitleFetchConfig


@pytest.fixture
def subtitlecat_pages() -> dict[str, str]:
    """subtitlecat 的搜索页与详情页; 番号 ``AD-1`` 有一条简体中文候选."""
    return {
        "https://www.subtitlecat.com/index.php?search=AD-1": (
            "<html><body>"
            '<a href="subs/100/ad-1-chinese.html">AD-1 中文字幕</a>'
            '<a href="subs/200/other-999-english.html">OTHER-999 english</a>'
            "</body></html>"
        ),
        "https://www.subtitlecat.com/subs/100/ad-1-chinese.html": (
            "<html><body>"
            '<a href="/subs/100/ad-1-zh-TW.srt">繁体</a>'
            '<a href="/subs/100/ad-1-zh-CN.srt">简体</a>'
            "</body></html>"
        ),
    }


@pytest.fixture
def subtitle_config() -> SubtitleFetchConfig:
    """只开 subtitlecat: 用例之间不靠 token 也不读真实来源."""
    from amane.config.manager import SubtitleFetchConfig

    return SubtitleFetchConfig(
        enabled=True,
        providers=["subtitlecat"],
        languages=["zh-CN", "zh-TW"],
        max_attempts=2,
    )
