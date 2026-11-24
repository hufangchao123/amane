"""字幕补全的目标判定.

分卷片 (``-CD2`` / ``PART1`` / 父目录 ``CD1``) 不算缺口: 字幕站给的是整片的字幕, 与拆开的
文件对不齐, 抓下来时间轴也错位, 因此统计里单独计数、补全时直接跳过.

只认显式的分卷标记, 不按「番号带个尾号」判 —— ``AD-1.mp4`` 的尾号属于番号本身.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from ..library.rules import is_pointer_media

__all__ = ["GapKind", "classify_gap", "is_multi_disc"]

_CD_TOKEN = re.compile(r"(?:^|[-_.\s])(?:cd|disc|disk|part|vol)[-_.\s]?0?(\d{1,2})(?=$|[-_.\s])", re.IGNORECASE)


def is_multi_disc(path: Path | str) -> bool:
    """文件名或直接父目录名里有显式的分卷标记."""
    target = Path(path)
    return any(_CD_TOKEN.search(text) for text in (target.stem, target.parent.name))


class GapKind(StrEnum):
    """「没有字幕」的三种口径, 互斥: 分卷整片对不齐、指针文件没有音轨、其余才是真缺口."""

    MULTI_DISC = "multi_disc"
    POINTER = "pointer"
    MISSING = "missing"


def classify_gap(path: Path | str) -> GapKind:
    """把一份缺字幕的文件归到唯一一类.

    优先级写死在这里, 统计、巡检与补全目标共用: 两处各写一条 ``if`` 链的话, 分卷又是 ``.strm``
    的文件会被两个桶各记一次 —— 状态页上的 ``missing`` 会算成负数, 补全却仍把它当目标.
    """
    target = Path(path)
    if is_multi_disc(target):
        return GapKind.MULTI_DISC
    if is_pointer_media(target):
        return GapKind.POINTER
    return GapKind.MISSING
