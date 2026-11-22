"""``notify_records`` 的读写面: 登记、列举顺序、修剪."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from amane.db.repository import Repository


@pytest.mark.asyncio(loop_scope="function")
async def test_add_and_list_newest_first(repo: Repository) -> None:
    await repo.add_notify_record(kind="message", channel="webhook", title="第一条", body="a", ok=True)
    await repo.add_notify_record(kind="digest", channel="bark", title="第二条", body="b", ok=False, error="超时")
    records = await repo.list_notify_records()
    assert [record.title for record in records] == ["第二条", "第一条"]
    assert records[0].error == "超时"
    assert records[0].kind == "digest"


@pytest.mark.asyncio(loop_scope="function")
async def test_prune_keeps_latest(repo: Repository) -> None:
    for index in range(5):
        await repo.add_notify_record(kind="message", channel="webhook", title=f"第{index}条", body="", ok=True)
    removed = await repo.prune_notify_records(2)
    assert removed == 3
    records = await repo.list_notify_records()
    assert [record.title for record in records] == ["第4条", "第3条"]


@pytest.mark.asyncio(loop_scope="function")
async def test_prune_below_limit_deletes_nothing(repo: Repository) -> None:
    await repo.add_notify_record(kind="message", channel="webhook", title="唯一一条", body="", ok=True)
    assert await repo.prune_notify_records(50) == 0
    assert len(await repo.list_notify_records()) == 1
