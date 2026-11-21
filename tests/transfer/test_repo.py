"""``transfer_items`` 的读写面: 幂等登记与状态机.

状态机是任务之间唯一共享的事实, 所以每条转移都单独钉一遍.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from amane.db.models import TransferItemState
from amane.transfer import TransferSummary

if TYPE_CHECKING:
    from amane.db.repository import Repository

pytestmark = pytest.mark.asyncio(loop_scope="function")


async def _rows(repo: Repository, *, state: TransferItemState | None = None) -> list[tuple[str, TransferItemState]]:
    rows, _ = await repo.list_transfer_items(state=state, offset=0, limit=100)
    return [(row.fid, row.state) for row in rows]


async def test_track_is_idempotent_on_fid(transfer_repo: Repository) -> None:
    await transfer_repo.track_transfer_items([("f1", "首见.mp4"), ("f1", "首见.mp4")])
    await transfer_repo.track_transfer_items([("f1", "改名.mp4")])

    rows, total = await transfer_repo.list_transfer_items(state=None, offset=0, limit=10)
    assert total == 1
    assert rows[0].state is TransferItemState.PENDING
    assert rows[0].file_name == "首见.mp4", "登记后不回写: 名字以首见为准"
    assert rows[0].attempts == 0


async def test_mark_moved_records_time_and_attempt(transfer_repo: Repository) -> None:
    await transfer_repo.track_transfer_items([("f1", "a.mp4")])
    await transfer_repo.mark_transfer_moved("f1", "a.mp4")

    rows, _ = await transfer_repo.list_transfer_items(state=None, offset=0, limit=10)
    row = rows[0]
    assert row.state is TransferItemState.MOVED
    assert row.attempts == 1
    assert row.moved_at is not None
    assert row.error is None


async def test_mark_skipped_keeps_reason_and_attempts(transfer_repo: Repository) -> None:
    await transfer_repo.track_transfer_items([("f1", "广告.mp4")])
    await transfer_repo.mark_transfer_skipped("f1", "广告.mp4", "广告文件")

    rows, _ = await transfer_repo.list_transfer_items(state=None, offset=0, limit=10)
    assert rows[0].state is TransferItemState.SKIPPED
    assert rows[0].error == "广告文件"
    assert rows[0].attempts == 0, "跳过不是尝试"
    assert rows[0].moved_at is None


async def test_mark_failed_accumulates_attempts(transfer_repo: Repository) -> None:
    await transfer_repo.track_transfer_items([("f1", "a.mp4")])
    await transfer_repo.mark_transfer_failed("f1", "a.mp4", "第一次")
    await transfer_repo.mark_transfer_failed("f1", "a.mp4", "第二次")

    rows, _ = await transfer_repo.list_transfer_items(state=None, offset=0, limit=10)
    assert rows[0].state is TransferItemState.FAILED
    assert rows[0].attempts == 2
    assert rows[0].error == "第二次", "只留最后一次的错误, 免得多轮消息叠加"


async def test_state_counts_and_summary(transfer_repo: Repository) -> None:
    await transfer_repo.track_transfer_items([("f1", "a.mp4"), ("f2", "b.mp4"), ("f3", "c.mp4"), ("f4", "d.mp4")])
    await transfer_repo.mark_transfer_moved("f1", "a.mp4")
    await transfer_repo.mark_transfer_skipped("f2", "b.mp4", "广告文件")
    await transfer_repo.mark_transfer_failed("f3", "c.mp4", "搬不动")

    counts = await transfer_repo.transfer_state_counts()
    summary = TransferSummary.from_counts(counts)
    assert (summary.pending, summary.moved, summary.skipped, summary.failed) == (1, 1, 1, 1)


async def test_list_filters_by_state_and_pages(transfer_repo: Repository) -> None:
    await transfer_repo.track_transfer_items([("f1", "a.mp4"), ("f2", "b.mp4"), ("f3", "c.mp4")])
    await transfer_repo.mark_transfer_moved("f1", "a.mp4")

    moved, moved_total = await transfer_repo.list_transfer_items(state=TransferItemState.MOVED, offset=0, limit=10)
    assert [row.fid for row in moved] == ["f1"]
    assert moved_total == 1, "total 不受 limit 截断"

    page, total = await transfer_repo.list_transfer_items(state=None, offset=1, limit=1)
    assert total == 3
    assert len(page) == 1
    # 刚更新过的排在前面, 其余按 id 倒序: 分页顺序必须稳定, 否则翻页会漏行.
    assert [row.fid for row in page] == ["f3"]


async def test_snapshots_return_every_requested_fid(transfer_repo: Repository) -> None:
    """登记与查询都要跨分片: 源目录一次可能上百条, 超过单个 SQL 参数上限."""
    entries = [(f"f{index}", f"{index}.mp4") for index in range(520)]
    await transfer_repo.track_transfer_items(entries)

    snapshots = await transfer_repo.transfer_snapshots([fid for fid, _ in entries])
    assert len(snapshots) == 520
    assert snapshots["f519"].state is TransferItemState.PENDING


async def test_snapshots_of_nothing_skip_the_query(transfer_repo: Repository) -> None:
    assert await transfer_repo.transfer_snapshots([]) == {}


async def test_unknown_fid_is_ignored(transfer_repo: Repository) -> None:
    """状态写只针对已登记的项; 对未知 fid 不能插出一行半成品."""
    await transfer_repo.mark_transfer_moved("ghost", "ghost.mp4")
    _, total = await transfer_repo.list_transfer_items(state=None, offset=0, limit=10)
    assert total == 0
