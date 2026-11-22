"""执行编排: 洗掉、还原、到期清理.

组怎么算是 ``test_wash_model`` 的事, 这里验的是动盘与记账: 记录写得对不对、重复执行会不会重复搬、
一个组或一个库出错会不会拖垮整批、真删有没有守住「只有到期才删」这条线.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from amane.db.models import WashRecordKind, WashRecordState
from amane.library.wash.apply import ApplyReport, purge_records, restore_records, wash_groups
from amane.library.wash.collect import collect_entries
from amane.library.wash.model import build_groups
from amane.library.wash.trash import move_into_trash, trash_root

if TYPE_CHECKING:
    from collections.abc import Sequence

    from amane.db.models import WashRecord
    from amane.handlers._common import LibraryTaskLocks

    from .conftest import WashLibrary

LOCAL = WashRecordKind.LOCAL
STRM = WashRecordKind.STRM


async def groups_for(
    wash_lib: WashLibrary,
    kinds: Sequence[WashRecordKind] = (LOCAL, STRM),
    *,
    min_size_bytes: int = 0,
) -> list:
    entries = await collect_entries(wash_lib.repo, kinds=kinds)
    return build_groups(entries, min_size_bytes=min_size_bytes)


async def seed_two_groups(wash_lib: WashLibrary) -> None:
    """两组各两份: 本地同番号两份 + STRM 同番号两份 → 各洗掉一份."""
    await wash_lib.add("2024/ABC-123/ABC-123-1080p.mkv", size=2000, duration=100.0)
    await wash_lib.add("2024/ABC-123/ABC-123-720p.mp4", size=900, duration=100.0)
    # 目录名刻意让 strm-a 排在 strm-b 之前: STRM 的兜底是路径字典序, 留下的就是 a 里那份.
    await wash_lib.add("strm-a/ABC-124.strm", size=0)
    await wash_lib.add("strm-b/ABC-124.strm", size=0)


async def all_records(wash_lib: WashLibrary) -> list[WashRecord]:
    records, _ = await wash_lib.repo.list_wash_records(limit=100)
    return records


@pytest.mark.asyncio(loop_scope="function")
async def test_wash_moves_drops_and_writes_records(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """每组留下最优那份, 落选者进回收站, 每条落选写一行记录 (含到期时间与体积)."""
    await seed_two_groups(wash_lib)
    groups = await groups_for(wash_lib)
    assert len(groups) == 2

    report = await wash_groups(wash_lib.repo, groups, keep_days=7, lock_for=locks.get)

    assert (report.done, report.failed, report.skipped) == (2, 0, 0)
    assert report.bytes == 900
    assert not (wash_lib.root / "2024/ABC-123/ABC-123-720p.mp4").exists()
    assert not (wash_lib.root / "strm-b/ABC-124.strm").exists()
    assert (wash_lib.root / "2024/ABC-123/ABC-123-1080p.mkv").exists()

    records = await all_records(wash_lib)
    by_kind = {record.kind: record for record in records}
    local = by_kind[LOCAL]
    assert local.group_key == "lib1:ABC-123"
    assert local.kept_path == str(wash_lib.root / "2024/ABC-123/ABC-123-1080p.mkv")
    assert local.removed_path == str(
        wash_lib.root.parent / ".amane_wash" / "lib" / "local" / "2024/ABC-123/ABC-123-720p.mp4"
    )
    assert (local.size_bytes, local.state) == (900, WashRecordState.TRASHED)
    assert local.expires_at is not None
    delta = local.expires_at.replace(tzinfo=None) - datetime.now(UTC).replace(tzinfo=None)
    assert timedelta(days=6, hours=23) < delta <= timedelta(days=7)
    assert by_kind[STRM].group_key == "lib1:ABC-124"


@pytest.mark.asyncio(loop_scope="function")
async def test_second_run_changes_nothing(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """重复执行幂等: 已搬走的文件不再进候选, 记录数不变."""
    await seed_two_groups(wash_lib)
    await wash_groups(wash_lib.repo, await groups_for(wash_lib), keep_days=7, lock_for=locks.get)

    regrouped = await groups_for(wash_lib)
    report = await wash_groups(wash_lib.repo, regrouped, keep_days=7, lock_for=locks.get)

    assert regrouped == []
    assert (report.done, report.failed) == (0, 0)
    assert len(await all_records(wash_lib)) == 2


@pytest.mark.asyncio(loop_scope="function")
async def test_one_blocked_group_does_not_stop_the_others(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """某一组的落点被占用时只跳过那一份, 后面的组照常洗 (面板 P1-10 的「两层 break」教训)."""
    await seed_two_groups(wash_lib)
    occupied = trash_root(wash_lib.root) / "local" / "2024/ABC-123/ABC-123-720p.mp4"
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"y" * 5)

    report = await wash_groups(wash_lib.repo, await groups_for(wash_lib), keep_days=7, lock_for=locks.get)

    assert (report.done, report.skipped, report.failed) == (1, 1, 0)
    assert occupied.read_bytes() == b"y" * 5
    assert not (wash_lib.root / "strm-b/ABC-124.strm").exists()
    assert [record.kind for record in await all_records(wash_lib)] == [STRM]


@pytest.mark.asyncio(loop_scope="function")
async def test_missing_library_fails_only_its_own_groups(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """库在索引里被删掉时只记失败, 任务不中断, 也不写半条记录."""
    await seed_two_groups(wash_lib)
    groups = await groups_for(wash_lib)
    assert wash_lib.library.id is not None
    await wash_lib.repo.delete_library(wash_lib.library.id)

    report = await wash_groups(wash_lib.repo, groups, keep_days=7, lock_for=locks.get)

    assert (report.done, report.failed) == (0, 2)
    assert report.errors and "不存在" in report.errors[0]
    assert await all_records(wash_lib) == []


@pytest.mark.asyncio(loop_scope="function")
async def test_unusable_library_root_fails_without_records(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """库目录整个不见了 (挂载掉了) 时不能凭空记账: 记了就是永久失联的一份."""
    await seed_two_groups(wash_lib)
    groups = await groups_for(wash_lib)
    shutil.rmtree(wash_lib.root)

    report = await wash_groups(wash_lib.repo, groups, keep_days=7, lock_for=locks.get)

    assert (report.done, report.failed) == (0, 2)
    assert report.errors and "库目录不可用" in report.errors[0]


@pytest.mark.asyncio(loop_scope="function")
async def test_progress_reports_total_up_front(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """进度先报总数、最后报完成, 前端才知道还有多少没做完."""
    await seed_two_groups(wash_lib)
    seen: list[tuple[int, int, str]] = []

    async def progress(current: int, total: int, message: str) -> None:
        seen.append((current, total, message))

    await wash_groups(wash_lib.repo, await groups_for(wash_lib), keep_days=7, lock_for=locks.get, progress=progress)

    assert seen[0] == (0, 2, "wash")
    assert seen[-1] == (2, 2, "done")
    assert [item[0] for item in seen] == sorted(item[0] for item in seen)


async def washed_records(wash_lib: WashLibrary, locks: LibraryTaskLocks) -> list[WashRecord]:
    await seed_two_groups(wash_lib)
    await wash_groups(wash_lib.repo, await groups_for(wash_lib), keep_days=7, lock_for=locks.get)
    return await all_records(wash_lib)


@pytest.mark.asyncio(loop_scope="function")
async def test_restore_puts_file_back_and_marks_record(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """还原回原位, 记录推进到 restored."""
    records = await washed_records(wash_lib, locks)

    report = await restore_records(wash_lib.repo, records, lock_for=locks.get)

    assert (report.done, report.failed, report.skipped) == (2, 0, 0)
    assert report.bytes == 900 + 0
    assert (wash_lib.root / "2024/ABC-123/ABC-123-720p.mp4").exists()
    assert (wash_lib.root / "strm-b/ABC-124.strm").exists()
    assert {record.state for record in await all_records(wash_lib)} == {WashRecordState.RESTORED}


@pytest.mark.asyncio(loop_scope="function")
async def test_restore_skips_when_original_path_is_taken(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """原位已有人放了文件时跳过, 记录仍是 trashed, 人工再决定."""
    records = await washed_records(wash_lib, locks)
    taken = wash_lib.root / "2024/ABC-123/ABC-123-720p.mp4"
    taken.parent.mkdir(parents=True, exist_ok=True)
    taken.write_bytes(b"z" * 3)
    local = [record for record in records if record.kind is LOCAL]

    report = await restore_records(wash_lib.repo, local, lock_for=locks.get)

    assert (report.done, report.skipped) == (0, 1)
    assert taken.read_bytes() == b"z" * 3
    assert local[0].id is not None
    (stored,) = await wash_lib.repo.get_wash_records([local[0].id])
    assert stored.state is WashRecordState.TRASHED


@pytest.mark.asyncio(loop_scope="function")
async def test_restore_fails_when_file_left_the_trash(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """回收站里的文件被人手工删掉后, 还原要如实失败而不是假装成功."""
    records = await washed_records(wash_lib, locks)
    Path(records[0].removed_path).unlink()

    report = await restore_records(wash_lib.repo, [records[0]], lock_for=locks.get)

    assert (report.done, report.failed) == (0, 1)
    assert report.errors and "找不到" in report.errors[0]


@pytest.mark.asyncio(loop_scope="function")
async def test_restore_rejects_unparsable_group_key(wash_lib: WashLibrary, locks: LibraryTaskLocks) -> None:
    """分组键解不出库时不能猜库根, 直接记失败."""
    # 目录名刻意让 strm-a 排在 strm-b 之前: STRM 的兜底是路径字典序, 留下的就是 a 里那份.
    await wash_lib.add("strm-a/ABC-124.strm", size=0)
    await wash_lib.add("strm-b/ABC-124.strm", size=0)
    (group,) = await groups_for(wash_lib, kinds=[STRM])
    await wash_groups(wash_lib.repo, [group], keep_days=7, lock_for=locks.get)
    (record,) = await all_records(wash_lib)
    record_id = record.id
    assert record_id is not None
    tampered = record.model_copy(update={"group_key": "broken"})

    report = await restore_records(wash_lib.repo, [tampered], lock_for=locks.get)

    assert (report.done, report.failed) == (0, 1)
    assert report.errors and "无法解析" in report.errors[0]


async def make_record(
    wash_lib: WashLibrary,
    *,
    relative: str = "a/ABC-123-720p.mp4",
    expires_in: timedelta | None,
) -> WashRecord:
    """造一条「文件已在回收站里」的记录, 用来单独验到期判定."""
    source = wash_lib.root / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"x" * 42)
    moved = await move_into_trash(source, library_root=wash_lib.root, kind=LOCAL)
    assert moved.target is not None
    expires_at = datetime.now(UTC) + expires_in if expires_in is not None else None
    record_id = await wash_lib.repo.add_wash_record(
        kind=LOCAL,
        group_key="lib1:ABC-123",
        kept_path=str(wash_lib.root / "b/ABC-123-1080p.mkv"),
        removed_path=str(moved.target),
        size_bytes=42,
        expires_at=expires_at,
    )
    (record,) = await wash_lib.repo.get_wash_records([record_id])
    return record


PURGE_CASES = [
    pytest.param(timedelta(days=-1), True, id="expired-yesterday"),
    pytest.param(timedelta(hours=-1), True, id="expired-hour-ago"),
    pytest.param(timedelta(days=1), False, id="expires-tomorrow"),
]


@pytest.mark.parametrize(("expires_in", "should_purge"), PURGE_CASES)
@pytest.mark.asyncio(loop_scope="function")
async def test_purge_only_deletes_expired(
    wash_lib: WashLibrary,
    expires_in: timedelta,
    should_purge: bool,
) -> None:
    """``purged`` 是唯一真删路径, 未到期的记录必须原样留在回收站."""
    record = await make_record(wash_lib, expires_in=expires_in)

    report = await purge_records(wash_lib.repo, [record])

    path_kept = Path(record.removed_path).exists()
    assert path_kept is not should_purge
    assert report.done == int(should_purge)
    assert report.skipped == int(not should_purge)
    assert record.id is not None
    (stored,) = await wash_lib.repo.get_wash_records([record.id])
    assert stored.state is (WashRecordState.PURGED if should_purge else WashRecordState.TRASHED)


@pytest.mark.asyncio(loop_scope="function")
async def test_purge_without_expiry_keeps_record(wash_lib: WashLibrary) -> None:
    """没有到期时间的历史行不猜: 留着总比错删安全."""
    record = await make_record(wash_lib, expires_in=None)

    report = await purge_records(wash_lib.repo, [record])

    assert (report.done, report.skipped) == (0, 1)
    assert Path(record.removed_path).exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_purge_marks_record_with_lost_file(wash_lib: WashLibrary) -> None:
    """文件已经不在回收站里的到期记录也要置 purged: 它再也还原不回来."""
    record = await make_record(wash_lib, expires_in=timedelta(days=-1))
    Path(record.removed_path).unlink()

    report = await purge_records(wash_lib.repo, [record])

    assert (report.done, report.skipped) == (0, 1)
    assert record.id is not None
    (stored,) = await wash_lib.repo.get_wash_records([record.id])
    assert stored.state is WashRecordState.PURGED


@pytest.mark.asyncio(loop_scope="function")
async def test_purge_does_nothing_when_nothing_expired(wash_lib: WashLibrary) -> None:
    """全是未到期记录时连回收站目录都不用碰."""
    record = await make_record(wash_lib, expires_in=timedelta(days=30))

    report = await purge_records(wash_lib.repo, [record])

    assert report == ApplyReport(skipped=1)
    assert Path(record.removed_path).exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_purge_skips_record_already_restored(wash_lib: WashLibrary, locks: LibraryTaskLocks) -> None:
    """已经还原的记录不再是清理对象, 否则会去删原位文件."""
    await seed_two_groups(wash_lib)
    await wash_groups(wash_lib.repo, await groups_for(wash_lib), keep_days=7, lock_for=locks.get)
    records = await all_records(wash_lib)
    await restore_records(wash_lib.repo, records, lock_for=locks.get)

    report = await purge_records(wash_lib.repo, records, now=datetime.now(UTC) + timedelta(days=365))

    assert (report.done, report.skipped) == (0, 2)
    assert (wash_lib.root / "2024/ABC-123/ABC-123-720p.mp4").exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_purge_prunes_emptied_trash_dirs(wash_lib: WashLibrary) -> None:
    """删完到期文件后空目录收掉, 但回收站根保留 (下次洗版还要用)."""
    record = await make_record(wash_lib, relative="deep/nest/ABC-123-720p.mp4", expires_in=timedelta(days=-1))

    await purge_records(wash_lib.repo, [record])

    root = trash_root(wash_lib.root)
    assert not (root / "local" / "deep").exists()
    assert (wash_lib.root.parent / ".amane_wash").is_dir()


@pytest.mark.asyncio(loop_scope="function")
async def test_purge_keeps_record_when_unlink_fails(wash_lib: WashLibrary) -> None:
    """删失败的文件还在回收站里: 记录必须留在 trashed, 既不能假成功也要留着重试."""
    if os.geteuid() == 0:
        pytest.skip("root 无视目录权限, 造不出 unlink 失败")
    record = await make_record(wash_lib, relative="locked/ABC-123-720p.mp4", expires_in=timedelta(days=-1))
    removed = Path(record.removed_path)
    removed.parent.chmod(0o500)
    try:
        report = await purge_records(wash_lib.repo, [record])
    finally:
        removed.parent.chmod(0o700)

    assert (report.done, report.failed) == (0, 1)
    assert report.errors
    assert removed.exists()
    assert record.id is not None
    (stored,) = await wash_lib.repo.get_wash_records([record.id])
    assert stored.state is WashRecordState.TRASHED


@pytest.mark.asyncio(loop_scope="function")
async def test_purge_advances_only_cleared_records(wash_lib: WashLibrary) -> None:
    """一批里删成功与删失败混在一起时, 只有成功的那条能被推进到 purged."""
    if os.geteuid() == 0:
        pytest.skip("root 无视目录权限, 造不出 unlink 失败")
    good = await make_record(wash_lib, relative="ok/ABC-123-720p.mp4", expires_in=timedelta(days=-1))
    bad = await make_record(wash_lib, relative="locked/ABC-124-720p.mp4", expires_in=timedelta(days=-1))
    locked = Path(bad.removed_path).parent
    locked.chmod(0o500)
    try:
        report = await purge_records(wash_lib.repo, [good, bad])
    finally:
        locked.chmod(0o700)

    assert (report.done, report.failed, report.bytes) == (1, 1, 42)
    assert good.id is not None
    assert bad.id is not None
    states = {record.id: record.state for record in await wash_lib.repo.get_wash_records([good.id, bad.id])}
    assert states == {good.id: WashRecordState.PURGED, bad.id: WashRecordState.TRASHED}
