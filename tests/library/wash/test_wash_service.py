"""只读视图: 状态卡数字、候选分页/排序、回收站分页.

``summary`` 的组数按索引估算 (便宜、够用), 权威数字来自扫描任务 —— 这里把两者的口径差异也
一并钉住, 免得以后有人把估算当成精确值。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from amane.config.manager import WashConfig
from amane.db.models import SortOrder, WashChoiceAction, WashRecordKind, WashRecordState
from amane.db.repos.ops_wash import WashRecordSortField
from amane.handlers._common import LibraryTaskLocks
from amane.library.wash.apply import wash_groups
from amane.library.wash.service import (
    WashCandidateSortField,
    WashService,
    WashSummary,
    parse_kind,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from amane.db.repository import Repository

    from .conftest import WashLibrary

LOCAL = WashRecordKind.LOCAL
STRM = WashRecordKind.STRM


async def seed(wash_lib: WashLibrary) -> None:
    """两个本地组 + 一个 STRM 组, 组内各两份."""
    await wash_lib.add("a/ABC-100/ABC-100-1080p.mkv", size=3000, number="ABC-100")
    await wash_lib.add("a/ABC-100/ABC-100-720p.mp4", size=1000, number="ABC-100")
    await wash_lib.add("b/ABC-200/ABC-200-1080p.mkv", size=5000, number="ABC-200")
    await wash_lib.add("b/ABC-200/ABC-200-720p.mp4", size=2000, number="ABC-200")
    await wash_lib.add("c/ABC-300.strm", size=0, number="ABC-300")
    await wash_lib.add("d/ABC-300.strm", size=0, number="ABC-300")


ENABLED_CASES = [
    pytest.param(False, True, True, False, id="switch-off"),
    pytest.param(True, False, False, False, id="both-sides-off"),
    pytest.param(True, True, False, True, id="local-only"),
    pytest.param(True, False, True, True, id="strm-only"),
]


@pytest.mark.parametrize(("enabled", "local", "strm", "expected"), ENABLED_CASES)
def test_enabled_needs_switch_and_one_side(
    wash_lib: WashLibrary,
    enabled: bool,
    local: bool,
    strm: bool,
    expected: bool,
) -> None:
    """总开关与两侧开关任一关闭都会让洗版整体不可用."""
    service = WashService(WashConfig(enabled=enabled, local=local, strm=strm), wash_lib.repo)
    assert service.enabled is expected


KIND_CASES = [
    pytest.param(None, (True, True), [LOCAL, STRM], id="all-both-on"),
    pytest.param(None, (True, False), [LOCAL], id="all-local-only"),
    pytest.param(None, (False, True), [STRM], id="all-strm-only"),
    pytest.param(LOCAL, (True, True), [LOCAL], id="explicit-local"),
    pytest.param(STRM, (False, False), [], id="explicit-strm-respects-switch"),
    pytest.param(LOCAL, (False, True), [], id="explicit-local-respects-switch"),
]


@pytest.mark.parametrize(("kind", "sides", "expected"), KIND_CASES)
def test_kinds_expands_filter(
    wash_lib: WashLibrary,
    kind: WashRecordKind | None,
    sides: tuple[bool, bool],
    expected: list[WashRecordKind],
) -> None:
    """``kind=None`` 由配置决定; 明确指定也要过开关, 关掉的一侧不给参与."""
    service = WashService(WashConfig(enabled=True, local=sides[0], strm=sides[1]), wash_lib.repo)
    assert service.kinds(kind) == expected


def test_parse_kind_mapping() -> None:
    """路由与任务共用这一处映射."""
    assert parse_kind("local") is LOCAL
    assert parse_kind("strm") is STRM
    assert parse_kind("all") is None


@pytest.mark.asyncio(loop_scope="function")
async def test_summary_counts_groups_and_trash(wash_lib: WashLibrary, locks: LibraryTaskLocks) -> None:
    """状态卡三个数字: 组数 (估算) + 回收站条数 + 可还原体积."""
    await seed(wash_lib)
    service = WashService(WashConfig(enabled=True, min_size_mb=0, keep_days=7), wash_lib.repo)
    await wash_groups(wash_lib.repo, await service.groups(), keep_days=7, lock_for=locks.get)

    summary = await service.summary()

    # 洗完之后索引里只剩保留的那一份, 每组不再有第二个候选, 因此估算组数归零.
    assert summary == WashSummary(groups=0, trashed=3, restorable_bytes=3000)


@pytest.mark.asyncio(loop_scope="function")
async def test_summary_hides_groups_when_disabled(wash_lib: WashLibrary, locks: LibraryTaskLocks) -> None:
    """关掉开关时组数报 0, 但回收站里的东西仍然要显示 (它还要清理)."""
    await seed(wash_lib)
    enabled = WashService(WashConfig(enabled=True, min_size_mb=0, keep_days=7), wash_lib.repo)
    await wash_groups(wash_lib.repo, await enabled.groups(), keep_days=7, lock_for=locks.get)

    summary = await WashService(WashConfig(enabled=False, min_size_mb=0), wash_lib.repo).summary()

    assert (summary.groups, summary.trashed) == (0, 3)


@pytest.mark.asyncio(loop_scope="function")
async def test_summary_counts_strm_groups_without_the_size_floor(wash_lib: WashLibrary) -> None:
    """只开 STRM 洗版时体积下限不适用: 指针文件只有几百字节, 拿 MB 卡它会让按钮永远禁用."""
    await seed(wash_lib)
    service = WashService(WashConfig(enabled=True, local=False, strm=True, min_size_mb=300), wash_lib.repo)

    summary = await service.summary()

    assert summary.groups == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_summary_skips_ignored_groups(wash_lib: WashLibrary) -> None:
    """标成「都留着」的组不计入组数, 否则顶部数字与候选表一直对不上."""
    await seed(wash_lib)
    await wash_lib.repo.set_wash_choice("lib1:ABC-100", WashChoiceAction.IGNORE)
    service = WashService(WashConfig(enabled=True, min_size_mb=0), wash_lib.repo)

    summary = await service.summary()

    assert summary.groups == 2


@pytest.mark.asyncio(loop_scope="function")
async def test_summary_tolerates_missing_table(repo: Repository) -> None:
    """迁移还没跑到这一版时状态卡显示 0, 不能让 /wash/status 整个 500."""
    service = WashService(WashConfig(enabled=True), repo)
    assert await service.summary() == WashSummary()


@pytest.mark.asyncio(loop_scope="function")
async def test_candidates_are_the_groups_to_wash(wash_lib: WashLibrary) -> None:
    """候选列表与任务用的是同一套分组, 只是换了形状."""
    await seed(wash_lib)
    service = WashService(WashConfig(enabled=True, min_size_mb=0), wash_lib.repo)

    items, total = await service.candidates(limit=10)

    assert total == 3
    assert {(item.kind, item.group_key) for item in items} == {
        (LOCAL, "lib1:ABC-100"),
        (LOCAL, "lib1:ABC-200"),
        (STRM, "lib1:ABC-300"),
    }
    local = next(item for item in items if item.group_key == "lib1:ABC-100")
    assert local.keep_path == str(wash_lib.root / "a/ABC-100/ABC-100-1080p.mkv")
    assert local.drop_paths == [str(wash_lib.root / "a/ABC-100/ABC-100-720p.mp4")]
    assert local.bytes_freed == 1000


CANDIDATE_PAGE_CASES = [
    pytest.param(WashCandidateSortField.BYTES_FREED, SortOrder.DESC, ["lib1:ABC-200", "lib1:ABC-100"], id="bytes-desc"),
    pytest.param(WashCandidateSortField.BYTES_FREED, SortOrder.ASC, ["lib1:ABC-100", "lib1:ABC-200"], id="bytes-asc"),
    pytest.param(WashCandidateSortField.GROUP_KEY, SortOrder.ASC, ["lib1:ABC-100", "lib1:ABC-200"], id="key-asc"),
]


@pytest.mark.parametrize(("sort_by", "order", "expected"), CANDIDATE_PAGE_CASES)
@pytest.mark.asyncio(loop_scope="function")
async def test_candidate_paging_and_sorting(
    wash_lib: WashLibrary,
    sort_by: WashCandidateSortField,
    order: SortOrder,
    expected: Sequence[str],
) -> None:
    """翻页靠 offset/limit, 排序键后一律以分组键兜底, 保证不重不漏."""
    await seed(wash_lib)
    service = WashService(WashConfig(enabled=True, min_size_mb=0), wash_lib.repo)

    items, total = await service.candidates(kind=LOCAL, offset=0, limit=2, sort_by=sort_by, order=order)

    assert total == 2
    assert [item.group_key for item in items] == list(expected)

    second, _ = await service.candidates(kind=LOCAL, offset=1, limit=1, sort_by=sort_by, order=order)
    assert [item.group_key for item in second] == [expected[1]]


@pytest.mark.asyncio(loop_scope="function")
async def test_candidates_kind_filter(wash_lib: WashLibrary) -> None:
    """``kind`` 只影响这一页的范围, 与配置无关."""
    await seed(wash_lib)
    service = WashService(WashConfig(enabled=True, min_size_mb=0), wash_lib.repo)

    strm_items, strm_total = await service.candidates(kind=STRM)
    local_items, local_total = await service.candidates(kind=LOCAL)

    assert (strm_total, [item.kind for item in strm_items]) == (1, [STRM])
    assert (local_total, [item.kind for item in local_items]) == (2, [LOCAL, LOCAL])


@pytest.mark.asyncio(loop_scope="function")
async def test_min_size_filters_small_groups(wash_lib: WashLibrary) -> None:
    """低于门限的本地组不进候选 (4 KB 的样片不算「同番号多份」), STRM 不受门限约束."""
    await seed(wash_lib)
    service = WashService(WashConfig(enabled=True, min_size_mb=1), wash_lib.repo)

    items, total = await service.candidates()

    assert (total, [item.kind for item in items]) == (1, [STRM])


async def add_records(wash_lib: WashLibrary, locks: LibraryTaskLocks) -> None:
    await seed(wash_lib)
    service = WashService(WashConfig(enabled=True, min_size_mb=0, keep_days=7), wash_lib.repo)
    await wash_groups(wash_lib.repo, await service.groups(), keep_days=7, lock_for=locks.get)


RECORD_PAGE_CASES = [
    pytest.param(None, 3, id="all"),
    pytest.param(WashRecordState.TRASHED, 3, id="trashed"),
    pytest.param(WashRecordState.PURGED, 0, id="purged"),
]


@pytest.mark.parametrize(("state", "expected_total"), RECORD_PAGE_CASES)
@pytest.mark.asyncio(loop_scope="function")
async def test_records_state_filter(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
    state: WashRecordState | None,
    expected_total: int,
) -> None:
    """回收站列表默认不过滤状态; 指定时按状态过滤."""
    await add_records(wash_lib, locks)
    service = WashService(WashConfig(enabled=True), wash_lib.repo)

    records, total = await service.records(state=state)

    assert (total, len(records)) == (expected_total, expected_total)


@pytest.mark.asyncio(loop_scope="function")
async def test_records_sort_and_page(wash_lib: WashLibrary, locks: LibraryTaskLocks) -> None:
    """按体积排序 + 翻页, 总数不变."""
    await add_records(wash_lib, locks)
    service = WashService(WashConfig(enabled=True), wash_lib.repo)

    first, total = await service.records(
        sort_by=WashRecordSortField.SIZE_BYTES, order=SortOrder.DESC, offset=0, limit=1
    )
    second, _ = await service.records(sort_by=WashRecordSortField.SIZE_BYTES, order=SortOrder.DESC, offset=1, limit=1)

    assert total == 3
    assert first[0].size_bytes == 2000
    assert second[0].size_bytes == 1000
    created = first[0].created_at
    assert created is not None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    assert timedelta(0) <= datetime.now(UTC) - created < timedelta(minutes=5)


@pytest.mark.asyncio(loop_scope="function")
async def test_ignored_group_stays_listed_without_drops(wash_lib: WashLibrary) -> None:
    """选了「都留着」: 组仍在列表里可以撤销, 但没有落选者、不释放体积、不进统计."""
    await seed(wash_lib)
    await wash_lib.repo.set_wash_choice("lib1:ABC-100", WashChoiceAction.IGNORE)
    service = WashService(WashConfig(enabled=True, min_size_mb=0), wash_lib.repo)

    items, total = await service.candidates(limit=10)
    ignored = next(item for item in items if item.group_key == "lib1:ABC-100")

    assert total == 3
    assert ignored.ignored is True
    assert ignored.drop_paths == []
    assert ignored.bytes_freed == 0


@pytest.mark.asyncio(loop_scope="function")
async def test_pinned_keep_overrides_the_algorithm(wash_lib: WashLibrary) -> None:
    """钉住的那一份成为保留者, 另一份变成落选者 —— 即使算法会选反."""
    await seed(wash_lib)
    smaller = str(wash_lib.root / "a/ABC-100/ABC-100-720p.mp4")
    await wash_lib.repo.set_wash_choice("lib1:ABC-100", WashChoiceAction.KEEP, keep_path=smaller)
    service = WashService(WashConfig(enabled=True, min_size_mb=0), wash_lib.repo)

    items, _ = await service.candidates(limit=10)
    pinned = next(item for item in items if item.group_key == "lib1:ABC-100")

    assert pinned.keep_path == smaller
    assert pinned.pinned is True
    assert pinned.drop_paths == [str(wash_lib.root / "a/ABC-100/ABC-100-1080p.mkv")]
    assert pinned.bytes_freed == 3000


@pytest.mark.asyncio(loop_scope="function")
async def test_execution_view_hides_ignored_groups(wash_lib: WashLibrary) -> None:
    """执行路径拿到的分组不含「都留着」的组, 免得把使用者要留的文件搬走."""
    await seed(wash_lib)
    await wash_lib.repo.set_wash_choice("lib1:ABC-100", WashChoiceAction.IGNORE)
    service = WashService(WashConfig(enabled=True, min_size_mb=0), wash_lib.repo)

    keys = [group.group_key for group in await service.groups(include_ignored=False)]

    assert "lib1:ABC-100" not in keys
    assert "lib1:ABC-200" in keys


@pytest.mark.asyncio(loop_scope="function")
async def test_stale_choice_is_dropped_and_missing_keep_path_falls_back(wash_lib: WashLibrary) -> None:
    """过期决定: 分组没了会被清掉; 钉住的那份没了则退回算法排序."""
    await seed(wash_lib)
    await wash_lib.repo.set_wash_choice("lib9:GONE-000", WashChoiceAction.IGNORE)
    await wash_lib.repo.set_wash_choice("lib1:ABC-200", WashChoiceAction.KEEP, keep_path="/nowhere.mkv")
    service = WashService(WashConfig(enabled=True, min_size_mb=0), wash_lib.repo)

    groups = await service.groups()
    dropped = await wash_lib.repo.drop_stale_wash_choices({group.group_key for group in groups})
    later = next(group for group in groups if group.group_key == "lib1:ABC-200")

    assert dropped == 1
    assert later.keep.path.endswith("ABC-200-1080p.mkv")
