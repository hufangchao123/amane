"""``audit_records`` 的读写面: 登记、分页、按方法过滤、修剪与清空."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from amane.db.repository import Repository


async def _add(repo: Repository, method: str, path: str) -> None:
    await repo.add_audit_record(method=method, path=path, query="", status=200, duration_ms=3, client="127.0.0.1")


@pytest.mark.asyncio(loop_scope="function")
async def test_list_newest_first_and_count(repo: Repository) -> None:
    await _add(repo, "POST", "/api/libraries")
    await _add(repo, "DELETE", "/api/tasks/1")
    records = await repo.list_audit_records(limit=10, offset=0)
    assert [record.path for record in records] == ["/api/tasks/1", "/api/libraries"]
    assert await repo.count_audit_records() == 2
    assert await repo.count_audit_records("POST") == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_paging_with_method_filter(repo: Repository) -> None:
    for index in range(3):
        await _add(repo, "POST", f"/api/x/{index}")
    await _add(repo, "DELETE", "/api/y")
    page = await repo.list_audit_records(limit=1, offset=1, method="POST")
    assert [record.path for record in page] == ["/api/x/1"]


@pytest.mark.asyncio(loop_scope="function")
async def test_prune_and_clear(repo: Repository) -> None:
    for index in range(4):
        await _add(repo, "POST", f"/api/x/{index}")
    assert await repo.prune_audit_records(2) == 2
    assert [record.path for record in await repo.list_audit_records(limit=10, offset=0)] == ["/api/x/3", "/api/x/2"]
    assert await repo.clear_audit_records() == 2
    assert await repo.count_audit_records() == 0
