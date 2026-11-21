"""广告判定与文件夹内挑广告; 判错的代价是删掉正片, 所以每条规则都要有用例钉住."""

from __future__ import annotations

import pytest

from amane.transfer import has_number, is_ad_file, is_media_name, pick_folder_ads

KEYWORDS = ["台湾uu", "2048", "youma"]

AD_CASES = [
    ("台湾uu-学生妹.mp4", True),
    ("2048社区合集.mkv", True),
    ("YOUMA-招嫖广告.avi", True),
    ("IPX-123.mp4", False),
    ("2048-IPX-123.mp4", False),
    ("sample.mp4", False),
]


@pytest.mark.parametrize(("name", "expected"), AD_CASES)
def test_is_ad_file(name: str, expected: bool) -> None:
    assert is_ad_file(name, KEYWORDS) is expected


def test_is_ad_file_without_keywords_keeps_everything() -> None:
    """没配关键词就没有广告之说: 空配置不能变成"所有无番号文件都是广告"."""
    assert is_ad_file("台湾uu-学生妹.mp4", []) is False


MEDIA_CASES = [
    ("IPX-123.mp4", True),
    ("IPX-123.MKV", True),
    ("IPX-123.rmvb", True),
    ("IPX-123.nfo", False),
    ("IPX-123", False),
]


@pytest.mark.parametrize(("name", "expected"), MEDIA_CASES)
def test_is_media_name(name: str, expected: bool) -> None:
    assert is_media_name(name) is expected


def test_has_number() -> None:
    assert has_number("IPX-123 無修正.mp4") is True
    assert has_number("台湾uu-学生妹.mp4") is False


FOLDER_CASES = [
    # 番号夹里的广告挂件被挑出, 正片留下.
    ("IPX-123 無修正", [("a1", "IPX-123.mp4"), ("a2", "台湾uu.mp4")], ["a2"]),
    # 广告夹里只有无番号视频才是广告, 正片留下.
    ("2048社区", [("a1", "sample.mp4"), ("a2", "IPX-123.mp4")], ["a1"]),
    # 番号夹里的特典没有番号, 但它不是广告.
    ("IPX-123", [("a1", "IPX-123.mp4"), ("a2", "特典.mp4")], []),
    # 夹里只有正片时不动.
    ("IPX-123", [("a1", "IPX-123.mp4")], []),
    # 整夹都是广告: 安全阀返回空, 宁可漏搬也不搬空.
    ("台湾uu合集", [("a1", "台湾uu1.mp4"), ("a2", "台湾uu2.mp4")], []),
    ("2048社区", [("a1", "sample.mp4")], []),
]


@pytest.mark.parametrize(("folder", "media", "expected"), FOLDER_CASES)
def test_pick_folder_ads(folder: str, media: list[tuple[str, str]], expected: list[str]) -> None:
    assert pick_folder_ads(folder, media, KEYWORDS) == expected
