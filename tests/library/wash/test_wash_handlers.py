"""两个 handler 的编排: 开关与库校验、三种请求的分流、结果计数.

用例直接调 ``handle``, 不经任务队列: 验的是 payload 怎么被翻译成动作与 ``WashApplyResult``,
排队与重试是任务系统的事.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from amane.config.manager import HotSettings, WashConfig
from amane.db.models import WashRecordKind, WashRecordState
from amane.handlers.models import WashApplyPayload, WashScanPayload
from amane.handlers.wash import WashApplyHandler, WashScanHandler

if TYPE_CHECKING:
    from amane.db.models import WashRecord
    from amane.handlers._common import LibraryTaskLocks

    from .conftest import WashLibrary

LOCAL = WashRecordKind.LOCAL
STRM = WashRecordKind.STRM


def hot(*, enabled: bool = True, keep_days: int = 7) -> HotSettings:
    return HotSettings(wash=WashConfig(enabled=enabled, min_size_mb=0, keep_days=keep_days))


async def seed_library(wash_lib: WashLibrary) -> None:
    """一个本地组 (两份) 与一个 STRM 组 (两份)."""
    await wash_lib.add("2024/ABC-123/ABC-123-1080p.mkv", size=2000, duration=100.0)
    await wash_lib.add("2024/ABC-123/ABC-123-720p.mp4", size=900, duration=100.0)
    await wash_lib.add("strm-a/ABC-124.strm", size=0)
    await wash_lib.add("strm-b/ABC-124.strm", size=0)


def scan_handler(wash_lib: WashLibrary, *, enabled: bool = True) -> WashScanHandler:
    return WashScanHandler(wash_lib.repo, hot(enabled=enabled))


def apply_handler(wash_lib: WashLibrary, locks: LibraryTaskLocks, *, enabled: bool = True) -> WashApplyHandler:
    return WashApplyHandler(wash_lib.repo, hot(enabled=enabled), locks)


@pytest.mark.asyncio(loop_scope="function")
async def test_scan_reports_three_numbers(wash_lib: WashLibrary) -> None:
    """``groups`` / ``droppable`` / ``bytes_freed`` 都已算好, 前端直接显示."""
    await seed_library(wash_lib)

    result = await scan_handler(wash_lib).handle(WashScanPayload())

    assert result.success is True
    assert result.result is not None
    assert (result.result.groups, result.result.droppable, result.result.bytes_freed) == (2, 2, 900)


@pytest.mark.asyncio(loop_scope="function")
async def test_scan_respects_kind_filter(wash_lib: WashLibrary) -> None:
    """两侧可分别关掉; ``kind`` 就是那个开关."""
    await seed_library(wash_lib)

    result = await scan_handler(wash_lib).handle(WashScanPayload(kind="strm"))

    assert result.result is not None
    assert (result.result.groups, result.result.droppable, result.result.bytes_freed) == (1, 1, 0)


@pytest.mark.asyncio(loop_scope="function")
async def test_scan_is_read_only(wash_lib: WashLibrary) -> None:
    """扫描不搬任何东西, 也不写记录."""
    await seed_library(wash_lib)

    await scan_handler(wash_lib).handle(WashScanPayload())

    assert (wash_lib.root / "2024/ABC-123/ABC-123-720p.mp4").exists()
    records, _ = await wash_lib.repo.list_wash_records(limit=10)
    assert records == []


ERROR_CASES = [
    pytest.param(False, None, "未启用", id="disabled"),
    pytest.param(True, 4242, "不存在", id="unknown-library"),
]


@pytest.mark.parametrize(("enabled", "library_id", "expected_error"), ERROR_CASES)
@pytest.mark.asyncio(loop_scope="function")
async def test_scan_rejects_bad_request(
    wash_lib: WashLibrary,
    enabled: bool,
    library_id: int | None,
    expected_error: str,
) -> None:
    """没开开关、库不存在都是明确的失败, 不是空结果 —— 空结果会被误读成「没有可洗的」."""
    result = await scan_handler(wash_lib, enabled=enabled).handle(WashScanPayload(library_id=library_id))

    assert result.success is False
    assert result.error is not None and expected_error in result.error


@pytest.mark.asyncio(loop_scope="function")
async def test_apply_washes_all_groups_without_keys(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """不指定 ``group_keys`` 等于「全部候选」, 结果里如实报搬了几份."""
    await seed_library(wash_lib)

    result = await apply_handler(wash_lib, locks).handle(WashApplyPayload())

    assert result.success is True
    assert result.result is not None
    assert (result.result.trashed, result.result.bytes_freed, result.result.skipped) == (2, 900, 0)
    assert not (wash_lib.root / "2024/ABC-123/ABC-123-720p.mp4").exists()
    assert not (wash_lib.root / "strm-b/ABC-124.strm").exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_apply_with_keys_touches_only_those_groups(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """指定分组键时只动该组; 不成立 (已洗过) 的键计 skipping 而不是失败."""
    await seed_library(wash_lib)

    result = await apply_handler(wash_lib, locks).handle(
        WashApplyPayload(group_keys=["lib1:ABC-123", "lib1:NOT-THERE"])
    )

    assert result.success is True
    assert result.result is not None
    assert (result.result.trashed, result.result.skipped) == (1, 1)
    assert not (wash_lib.root / "2024/ABC-123/ABC-123-720p.mp4").exists()
    assert (wash_lib.root / "strm-a/ABC-124.strm").exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_apply_reports_disabled_and_bad_library(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """执行侧同样要先过开关与库校验."""
    disabled = await apply_handler(wash_lib, locks, enabled=False).handle(WashApplyPayload())
    missing = await apply_handler(wash_lib, locks).handle(WashApplyPayload(library_id=4242))

    assert disabled.success is False and disabled.error is not None and "未启用" in disabled.error
    assert missing.success is False and missing.error is not None and "不存在" in missing.error


async def washed(wash_lib: WashLibrary, locks: LibraryTaskLocks) -> list[WashRecord]:
    await seed_library(wash_lib)
    await apply_handler(wash_lib, locks).handle(WashApplyPayload())
    records, _ = await wash_lib.repo.list_wash_records(limit=10)
    return records


@pytest.mark.asyncio(loop_scope="function")
async def test_apply_restore_ids_puts_files_back(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """带 ``restore_ids`` 的请求走还原, 并把记录推进到 restored."""
    records = await washed(wash_lib, locks)
    target = next(record for record in records if record.kind is LOCAL)
    assert target.id is not None

    result = await apply_handler(wash_lib, locks).handle(WashApplyPayload(restore_ids=[target.id]))

    assert result.success is True
    assert result.result is not None and result.result.restored == 1
    assert result.result.trashed == 0
    assert (wash_lib.root / "2024/ABC-123/ABC-123-720p.mp4").exists()
    (stored,) = await wash_lib.repo.get_wash_records([target.id])
    assert stored.state is WashRecordState.RESTORED


@pytest.mark.asyncio(loop_scope="function")
async def test_apply_unknown_record_id_is_failure(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
) -> None:
    """记录不存在时整条任务判失败, 错误信息说清少了几条."""
    result = await apply_handler(wash_lib, locks).handle(WashApplyPayload(restore_ids=[4242]))

    assert result.success is False
    assert result.error is not None and "不存在" in result.error
    assert result.result is not None and result.result.failed == 1


EXPIRY_CASES = [
    pytest.param(timedelta(days=-1), True, id="expired"),
    pytest.param(timedelta(days=30), False, id="not-expired"),
]


@pytest.mark.parametrize(("expires_in", "expected_purged"), EXPIRY_CASES)
@pytest.mark.asyncio(loop_scope="function")
async def test_apply_purge_prefix_respects_expiry(
    wash_lib: WashLibrary,
    locks: LibraryTaskLocks,
    expires_in: timedelta,
    expected_purged: bool,
) -> None:
    """``purge_ids`` 只对到期的记录真删; 未到期的原样留在回收站."""
    await seed_library(wash_lib)
    source = wash_lib.root / "extra" / "ABC-777-1080p.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"x" * 5)
    target = wash_lib.root.parent / ".amane_wash" / "lib" / "local" / "extra" / "ABC-777-1080p.mkv"
    target.parent.mkdir(parents=True)
    source.rename(target)
    record_id = await wash_lib.repo.add_wash_record(
        kind=LOCAL,
        group_key="lib1:ABC-777",
        kept_path=str(wash_lib.root / "extra" / "ABC-777-720p.mp4"),
        removed_path=str(target),
        size_bytes=5,
        expires_at=datetime.now(UTC) + expires_in,
    )

    result = await apply_handler(wash_lib, locks).handle(WashApplyPayload(purge_ids=[record_id]))

    assert result.success is True
    assert target.exists() is not expected_purged
    (stored,) = await wash_lib.repo.get_wash_records([record_id])
    assert stored.state is (WashRecordState.PURGED if expected_purged else WashRecordState.TRASHED)
