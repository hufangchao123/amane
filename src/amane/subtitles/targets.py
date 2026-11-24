"""字幕补全的目标判定.

分卷片 (``-CD2`` / ``PART1`` / 父目录 ``CD1``) 不算缺口: 字幕站给的是整片的字幕, 与拆开的
文件对不齐, 抓下来时间轴也错位, 因此统计里单独计数、补全时直接跳过.

只认显式的分卷标记, 不按「番号带个尾号」判 —— ``AD-1.mp4`` 的尾号属于番号本身.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["is_multi_disc"]

_CD_TOKEN = re.compile(r"(?:^|[-_.\s])(?:cd|disc|disk|part|vol)[-_.\s]?0?(\d{1,2})(?=$|[-_.\s])", re.IGNORECASE)


def is_multi_disc(path: Path | str) -> bool:
    """文件名或直接父目录名里有显式的分卷标记."""
    target = Path(path)
    return any(_CD_TOKEN.search(text) for text in (target.stem, target.parent.name))
