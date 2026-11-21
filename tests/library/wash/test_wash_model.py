"""择一算法: 分组的划分依据与五档排序.

这里是纯计算, 用例直接喂 ``WashEntry``, 不碰磁盘与数据库.
"""

from __future__ import annotations

import pytest

from amane.db.models import WashRecordKind
from amane.library.wash.model import (
    WashEntry,
    build_groups,
    container_rank,
    definition_rank,
    group_key_of,
    library_id_of,
)
from amane.parsing import Mosaic

LOCAL = WashRecordKind.LOCAL
STRM = WashRecordKind.STRM


def entry(
    name: str,
    *,
    kind: WashRecordKind = LOCAL,
    library_id: int = 1,
    number: str = "ABC-123",
    size: int = 1000,
    duration: float | None = None,
    definition: str | None = None,
    mosaic: Mosaic | None = None,
    cd: int | None = None,
) -> WashEntry:
    return WashEntry(
        path=f"/lib/{name}",
        kind=kind,
        library_id=library_id,
        number=number,
        size=size,
        duration=duration,
        definition=definition,
        mosaic=mosaic,
        cd=cd,
    )


GROUP_KEY_CASES = [
    pytest.param({"number": "abc-123"}, "lib1:ABC-123", id="upper-number"),
    pytest.param({"cd": 2}, "lib1:ABC-123:cd2", id="cd-part"),
    pytest.param({"mosaic": Mosaic.UNCENSORED}, "lib1:ABC-123:uncensored", id="mosaic-part"),
    pytest.param({"cd": 1, "mosaic": Mosaic.LEAKED}, "lib1:ABC-123:cd1:leaked", id="both-parts"),
    pytest.param({"library_id": 7}, "lib7:ABC-123", id="library-scoped"),
]


@pytest.mark.parametrize(("kwargs", "expected"), GROUP_KEY_CASES)
def test_group_key_composition(kwargs: dict[str, object], expected: str) -> None:
    """分组键必须带库、分段与有码标记: 少了任何一个都会把不同版本当成重复."""
    assert group_key_of(entry("a.mkv", **kwargs)) == expected  # type: ignore[arg-type]


LIBRARY_ID_CASES = [
    pytest.param("lib12:ABC-123", 12, id="plain"),
    pytest.param("lib12:ABC-123:cd1", 12, id="with-cd"),
    pytest.param("ABC-123", None, id="no-prefix"),
    pytest.param("lib:ABC-123", None, id="non-numeric"),
    pytest.param("", None, id="empty"),
]


@pytest.mark.parametrize(("key", "expected"), LIBRARY_ID_CASES)
def test_library_id_of(key: str, expected: int | None) -> None:
    """还原要按记录找回库根; 解不出来的键必须老实返回 None, 不能猜."""
    assert library_id_of(key) == expected


KEEP_CASES = [
    pytest.param(
        [entry("a-1080p.mkv", definition="1080p"), entry("b-4k.mkv", definition="4K")],
        "/lib/b-4k.mkv",
        id="resolution-first",
    ),
    pytest.param(
        [entry("a-4k-small.mkv", definition="4K", size=900), entry("b-1080p-big.mkv", definition="1080p", size=9000)],
        "/lib/a-4k-small.mkv",
        id="resolution-beats-size",
    ),
    pytest.param(
        [
            entry("a-1080p.mkv", definition="1080p", size=2000, duration=100.0),
            entry("b-1080p.mkv", definition="1080p", size=2000, duration=50.0),
        ],
        "/lib/b-1080p.mkv",
        id="bitrate-beats-size",
    ),
    pytest.param(
        [entry("a-1080p.mkv", definition="1080p", size=2000), entry("b-1080p.mkv", definition="1080p", size=5000)],
        "/lib/b-1080p.mkv",
        id="size-without-duration",
    ),
    pytest.param(
        [
            entry("a-1080p.mkv", definition="1080p", size=2000, duration=10.0),
            entry("b-1080p.mp4", definition="1080p", size=2000, duration=10.0),
        ],
        "/lib/a-1080p.mkv",
        id="container-mkv-over-mp4",
    ),
    pytest.param(
        [entry("a-1080p.mp4", definition="1080p"), entry("a-1080p.avi", definition="1080p")],
        "/lib/a-1080p.mp4",
        id="container-mp4-over-avi",
    ),
    pytest.param(
        [entry("b-1080p.xyz", definition="1080p"), entry("a-1080p.mkv", definition="1080p")],
        "/lib/a-1080p.mkv",
        id="known-container-over-unknown",
    ),
    pytest.param(
        [entry("b-1080p.mkv", definition="1080p"), entry("a-1080p.mkv", definition="1080p")],
        "/lib/a-1080p.mkv",
        id="path-breaks-tie",
    ),
    pytest.param(
        [entry("a.mkv", definition=None), entry("b.mkv", definition="720p")],
        "/lib/b.mkv",
        id="unknown-definition-last",
    ),
]


@pytest.mark.parametrize(("entries", "expected"), KEEP_CASES)
def test_local_keeps_best(entries: list[WashEntry], expected: str) -> None:
    """五档优先级: 分辨率 → 码率 → 体积 → 容器 → 路径."""
    groups = build_groups(reversed(entries), min_size_bytes=0)
    assert len(groups) == 1
    assert groups[0].keep.path == expected
    assert len(groups[0].drops) == 1


def test_strm_ignores_size_and_definition() -> None:
    """STRM 只是指针: 体积与文件名里的档位都不该决定去留 (二次审计 P0-7)."""
    groups = build_groups(
        [
            entry("b/ABC-123.strm", kind=STRM, size=0, definition="8K"),
            entry("a/ABC-123.mp4.strm", kind=STRM, size=0, definition="480p"),
        ],
        min_size_bytes=0,
    )
    assert len(groups) == 1
    # 容器优先级相同 (.strm), 于是落到路径字典序 —— 与体积、档位无关.
    assert groups[0].keep.path == "/lib/a/ABC-123.mp4.strm"


def test_local_and_strm_never_share_a_group() -> None:
    """同一番号的本地文件与 STRM 分属两组, 否则 STRM 会被当成「更小的一份」清洗掉."""
    groups = build_groups(
        [
            entry("a/ABC-123.mkv", definition="1080p"),
            entry("b/ABC-123.mkv", definition="720p"),
            entry("c/ABC-123.strm", kind=STRM),
            entry("d/ABC-123.strm", kind=STRM),
        ],
        min_size_bytes=0,
    )
    assert [(group.kind, len(group.drops)) for group in groups] == [(LOCAL, 1), (STRM, 1)]


def test_min_size_only_applies_to_local() -> None:
    """低于门限的本地文件不参与 (多半是样片), 但 STRM 不受体积门限约束."""
    groups = build_groups(
        [
            entry("a/ABC-123.mkv", size=10),
            entry("b/ABC-123.mp4", size=20),
            entry("c/ABC-124.strm", kind=STRM, number="ABC-124", size=0),
            entry("d/ABC-124.strm", kind=STRM, number="ABC-124", size=0),
        ],
        min_size_bytes=100,
    )
    assert [(group.kind, group.group_key) for group in groups] == [(STRM, "lib1:ABC-124")]


def test_single_member_group_is_not_a_candidate() -> None:
    """只有一份的组没有可回收对象."""
    assert build_groups([entry("a/ABC-123.mkv")], min_size_bytes=0) == []


def test_unparsed_number_is_ignored() -> None:
    """番号解析不出来就没有分组依据, 宁可不动."""
    assert build_groups([entry("a/x.mkv", number="  "), entry("b/y.mkv", number="")], min_size_bytes=0) == []


CD_AND_MOSAIC_CASES = [
    pytest.param({"cd": 1}, {"cd": 2}, {"lib1:ABC-123:cd1", "lib1:ABC-123:cd2"}, id="cd-split"),
    pytest.param(
        {"mosaic": Mosaic.UNCENSORED},
        {"mosaic": None},
        {"lib1:ABC-123:uncensored", "lib1:ABC-123"},
        id="unmarked-not-merged",
    ),
    pytest.param(
        {"mosaic": Mosaic.UNCENSORED},
        {"mosaic": Mosaic.LEAKED},
        {"lib1:ABC-123:uncensored", "lib1:ABC-123:leaked"},
        id="different-mosaic",
    ),
    pytest.param({"library_id": 1}, {"library_id": 2}, {"lib1:ABC-123", "lib2:ABC-123"}, id="different-library"),
]


@pytest.mark.parametrize(("left", "right", "expected_keys"), CD_AND_MOSAIC_CASES)
def test_version_dimensions_are_not_merged(
    left: dict[str, object],
    right: dict[str, object],
    expected_keys: set[str],
) -> None:
    """分段、有码标记与库都是版本维度; 合并它们等于丢内容或洗错版本."""
    groups = build_groups(
        [
            entry("a1.mkv", **left),  # type: ignore[arg-type]
            entry("a2.mp4", **left),  # type: ignore[arg-type]
            entry("b1.mkv", **right),  # type: ignore[arg-type]
            entry("b2.mp4", **right),  # type: ignore[arg-type]
        ],
        min_size_bytes=0,
    )
    assert {group.group_key for group in groups} == expected_keys


def test_groups_are_sorted_by_kind_then_key() -> None:
    """组顺序固定, 任务每次跑出来一致, 便于对数."""
    groups = build_groups(
        [
            entry("z/ZZZ-9.mkv", number="ZZZ-9"),
            entry("z/ZZZ-9.mp4", number="ZZZ-9"),
            entry("a/AAA-1.strm", kind=STRM, number="AAA-1"),
            entry("a/AAA-1.mp4.strm", kind=STRM, number="AAA-1"),
            entry("a/AAA-1.mkv", number="AAA-1"),
            entry("a/AAA-1.mp4", number="AAA-1"),
        ],
        min_size_bytes=0,
    )
    assert [(group.kind.value, group.group_key) for group in groups] == [
        ("local", "lib1:AAA-1"),
        ("local", "lib1:ZZZ-9"),
        ("strm", "lib1:AAA-1"),
    ]


RANK_CASES = [
    pytest.param("8K", 0, id="8k"),
    pytest.param("4k", 1, id="4k-casefolded"),
    pytest.param("1080p", 3, id="1080p"),
    pytest.param("480p", 5, id="480p"),
    pytest.param(None, 6, id="unknown"),
    pytest.param("weird", 6, id="unrecognised"),
]


@pytest.mark.parametrize(("definition", "expected"), RANK_CASES)
def test_definition_rank(definition: str | None, expected: int) -> None:
    assert definition_rank(definition) == expected


CONTAINER_CASES = [
    pytest.param("a.mkv", 0, id="mkv"),
    pytest.param("a.MP4", 1, id="mp4-casefolded"),
    pytest.param("a.avi", 6, id="avi"),
    pytest.param("a.xyz", len(("x",)) + 11, id="unknown-last"),
    pytest.param("a", 12, id="no-suffix"),
]


@pytest.mark.parametrize(("path", "expected"), CONTAINER_CASES)
def test_container_rank(path: str, expected: int) -> None:
    assert container_rank(path) == expected
