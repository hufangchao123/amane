"""择一与清理规则的边界: 门限恰等于、清晰度全缺失、容器大小写、到期恰等于、跨库同番号.

规则本身写在 ``library/wash/model.py`` 与 ``apply.py`` 的 docstring 里, 这里逐条钉住边界值,
避免以后把 ``<`` 改成 ``<=`` 或把「无时长」当成「时长为 0」这类改动悄悄改变取舍.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from amane.db.models import WashRecordKind, WashRecordState
from amane.library.wash.apply import _is_expired, restore_records, wash_groups
from amane.library.wash.collect import collect_entries
from amane.library.wash.model import WashEntry, build_groups, container_rank
from amane.library.wash.trash import move_into_trash

if TYPE_CHECKING:
    from amane.handlers._common import LibraryTaskLocks

    from .conftest import WashLibrary

LOCAL = WashRecordKind.LOCAL
STRM = WashRecordKind.STRM


def entry(path: str, *, kind: WashRecordKind = LOCAL, number: str = "ABC-123", **kwargs: object) -> WashEntry:
    return WashEntry(path=f"/lib/{path}", kind=kind, library_id=1, number=number, **kwargs)  # type: ignore[arg-type]


def test_min_size_boundary_includes_the_threshold() -> None:
    """门限是「小于才排除」: 恰好等于门限的两份仍然成组, 少一个字节的那份不参与."""
    exact = [
        entry("a.mkv", size=100, definition="1080p"),
        entry("b.mp4", size=100, definition="720p"),
    ]
    assert len(build_groups(exact, min_size_bytes=100)) == 1

    below = [
        entry("a.mkv", size=99, definition="1080p"),
        entry("b.mp4", size=99, definition="720p"),
    ]
    assert build_groups(below, min_size_bytes=100) == []


def test_all_definitions_missing_fall_back_to_size() -> None:
    """档位全缺失时不许抛错, 也不许把「未知」当成「一样好」而随手挑第一个: 落到体积."""
    groups = build_groups(
        [entry("a.mkv", size=100), entry("b.mp4", size=900)],
        min_size_bytes=0,
    )
    assert groups[0].keep.path == "/lib/b.mp4"


def test_unknown_definition_still_compares_bitrate() -> None:
    """档位缺失但有时长时, 码率仍然可比 —— 缺失的只是第一档, 不是整条规则."""
    groups = build_groups(
        [
            entry("a.mkv", size=2000, duration=100.0),
            entry("b.mkv", size=2000, duration=50.0),
        ],
        min_size_bytes=0,
    )
    assert groups[0].keep.path == "/lib/b.mkv"


def test_duration_missing_ranks_after_known_duration() -> None:
    """无时长的候选排在有时长之后: 拿不出时长就没法证明码率更高, 不拿体积冒充."""
    groups = build_groups(
        [
            entry("a-big.mkv", size=9000, definition="1080p"),
            entry("b-timed.mkv", size=2000, duration=100.0, definition="1080p"),
        ],
        min_size_bytes=0,
    )
    assert groups[0].keep.path == "/lib/b-timed.mkv"


def test_container_rank_ignores_case_and_puts_unknown_last() -> None:
    """扩展名比较与大小写无关; 不在表里的排最后, 否则 `.XYZ` 会靠字典序赢过 mkv."""
    assert container_rank("A.MKV") == container_rank("a.mkv")
    assert container_rank("a.xyz") > container_rank("a.avi")
    assert container_rank("a") == container_rank("a.xyz")


def test_expiry_is_inclusive_and_naive_time_is_utc() -> None:
    """到期判定是 ``<=``: 恰好到点的记录本轮就该清; 无过期时间的历史行不动."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert _is_expired(now, now)
    assert not _is_expired(now + timedelta(microseconds=1), now)
    assert not _is_expired(None, now)
    # SQLite 读回来的时间不带时区, 按 UTC 解释; 不能因为 naive 就被当成「未来」而永远不清.
    assert _is_expired(datetime(2025, 12, 31), now)  # noqa: DTZ001 - 故意给 naive 时间, 验的就是「按 UTC 解释」


@pytest.mark.asyncio(loop_scope="function")
async def test_same_number_in_two_libraries_keeps_one_each(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """同番号跨库不算同组: 两个库各留各的最优份, 谁也不会被另一个库的版本挤掉."""
    other_root = wash_lib.root.parent / "lib2"
    other_root.mkdir()
    assert wash_lib.library.id is not None
    other = await wash_lib.repo.create_library(name="wash2", path=str(other_root), write_nfo=False)

    await wash_lib.add("a/ABC-123-1080p.mkv", size=2000, duration=100.0)
    await wash_lib.add("a/ABC-123-720p.mp4", size=900, duration=100.0)
    (other_root / "b").mkdir()
    (other_root / "b/ABC-123-480p.mp4").write_bytes(b"x" * 700)
    (other_root / "b/ABC-123-360p.mp4").write_bytes(b"x" * 300)
    for relative, size in (("b/ABC-123-480p.mp4", 700), ("b/ABC-123-360p.mp4", 300)):
        assert other.id is not None
        await wash_lib.repo.create_media_file(other.id, path=str(other_root / relative), size=size, duration=100.0)

    entries = await collect_entries(wash_lib.repo, kinds=[LOCAL])
    groups = build_groups(entries, min_size_bytes=0)
    assert {group.group_key for group in groups} == {f"lib{wash_lib.library.id}:ABC-123", f"lib{other.id}:ABC-123"}

    report = await wash_groups(wash_lib.repo, groups, keep_days=7, lock_for=locks.get)

    assert (report.done, report.failed) == (2, 0)
    assert (wash_lib.root / "a/ABC-123-1080p.mkv").exists()
    assert (other_root / "b/ABC-123-480p.mp4").exists()
    records, total = await wash_lib.repo.list_wash_records(limit=10)
    assert total == 2
    assert {record.group_key.split(":")[0] for record in records} == {
        f"lib{wash_lib.library.id}",
        f"lib{other.id}",
    }


@pytest.mark.asyncio(loop_scope="function")
async def test_restore_skips_records_that_left_the_trash(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """只有 ``trashed`` 能还原: 已 purged / restored 的行不许再往库里搬文件."""
    source = wash_lib.root / "a/ABC-123-720p.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x" * 900)
    moved = await move_into_trash(source, library_root=wash_lib.root, kind=LOCAL)
    assert moved.target is not None
    record_id = await wash_lib.repo.add_wash_record(
        kind=LOCAL,
        group_key="lib1:ABC-123",
        kept_path=str(wash_lib.root / "a/ABC-123-1080p.mkv"),
        removed_path=str(moved.target),
        size_bytes=900,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    await wash_lib.repo.set_wash_record_state([record_id], WashRecordState.PURGED)

    (stored,) = await wash_lib.repo.get_wash_records([record_id])
    report = await restore_records(wash_lib.repo, [stored], lock_for=locks.get)

    assert (report.done, report.skipped, report.failed) == (0, 1, 0)
    assert not source.exists()
