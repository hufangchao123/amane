"""字幕补全的目标判定: 分卷片不算缺口."""

from __future__ import annotations

import pytest

from amane.subtitles.targets import is_multi_disc


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/lib/ABC-123.mp4", False),
        ("/lib/ABC-123-有码.mp4", False),
        # 番号本身的尾号不是分卷标记
        ("/lib/AD-1.mp4", False),
        ("/lib/ABC-123-CD2.mkv", True),
        ("/lib/ABC-123-cd10.mkv", True),
        ("/lib/ABC-123-part1.mkv", True),
        ("/lib/ABC-123_DISC_2.mp4", True),
        ("/lib/IPOK-008/CD1/IPOK-008.mp4", True),
        ("/lib/IPOK-008/第1巻/IPOK-008.mp4", False),
    ],
)
def test_is_multi_disc(path: str, expected: bool) -> None:
    assert is_multi_disc(path) is expected
