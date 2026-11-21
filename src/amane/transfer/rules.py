"""广告与媒体的名字判定: 决定网盘源目录里哪些项可以搬进 STRM 目录.

源目录里混着资源站的推广文件, 搬进 STRM 目录后会被整理流程当成素材;
番号是"这是正片"的唯一证据, 因此带番号的文件一律保留 —— 宁可漏过滤, 不把正片当广告.
"""

from collections.abc import Sequence
from pathlib import PurePosixPath

from ..library.rules import MEDIA_EXTENSIONS
from ..parsing import extract_number

TRANSFER_FILE_EXTENSIONS = MEDIA_EXTENSIONS | frozenset({".rmvb", ".m4v"})
"""中转认的媒体扩展名.

在上游整理流程的清单之外补 ``.rmvb`` / ``.m4v``: 网盘里存量资源多是这两个旧容器,
按整理流程的清单会把它们当非媒体文件留在源目录里.
"""


def is_media_name(name: str) -> bool:
    """只看扩展名, 不问文件是否存在: 网盘侧文件不由本机打开."""
    return PurePosixPath(name).suffix.lower() in TRANSFER_FILE_EXTENSIONS


def has_number(name: str) -> bool:
    """名字里是否含番号; 解析复用解析层, 与整理流程的判定保持一致."""
    return extract_number(name) is not None


def is_ad_file(name: str, keywords: Sequence[str]) -> bool:
    """命中关键词且不含番号才算广告."""
    if not keywords or has_number(name):
        return False
    lowered = name.lower()
    return any(keyword.strip().lower() in lowered for keyword in keywords if keyword.strip())


def pick_folder_ads(folder: str, media: Sequence[tuple[str, str]], keywords: Sequence[str]) -> list[str]:
    """挑出文件夹里该被处理的广告, 返回它们的 fid.

    文件夹本身可能是广告合集: 名字不含番号却命中关键词时, 里面每个无番号视频都算广告.
    番号文件夹里无番号的伴随文件 (sample / 特典) 不算广告 —— 面板会连带删掉它们,
    这里只按策略处理, 其余随文件夹一起搬走, 无番号的伴随文件不会被整理入库.

    安全阀: 整夹视频全被判成广告时不返回广告. 删空或跳过整夹都不是"过滤广告",
    这种情形应退回给人看.
    """
    if not media:
        return []
    folder_is_ad = is_ad_file(folder, keywords)
    ads = [fid for fid, name in media if is_ad_file(name, keywords) or (folder_is_ad and not has_number(name))]
    if len(ads) >= len(media):
        return []
    return ads
