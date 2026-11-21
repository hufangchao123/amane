"""中转路由: 只读查询、过滤分页与参数校验; 搬移动作没有写端点, 只走 ``POST /tasks``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx2 import AsyncClient

    from amane.db.repository import Repository

pytestmark = pytest.mark.asyncio(loop_scope="function")


async def _seed(repo: Repository) -> None:
    await repo.track_transfer_items([("f1", "a.mp4"), ("f2", "b.mp4"), ("f3", "c.mp4")])
    await repo.mark_transfer_moved("f1", "a.mp4")
    await repo.mark_transfer_skipped("f2", "b.mp4", "广告文件")


async def test_status_reports_config_and_counts(ops_client: AsyncClient, api_repo: Repository) -> None:
    await _seed(api_repo)

    response = await ops_client.get("transfer/status")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert "未启用" in body["reason"], "未启用时要说清缺什么"
    assert {k: body[k] for k in ("pending", "moved", "skipped", "failed")} == {
        "pending": 1,
        "moved": 1,
        "skipped": 1,
        "failed": 0,
    }


async def test_items_lists_newest_first(ops_client: AsyncClient, api_repo: Repository) -> None:
    await _seed(api_repo)

    response = await ops_client.get("transfer/items")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert [item["fid"] for item in body["items"]] == ["f2", "f1", "f3"], "刚更新过的排在前面"
    assert body["items"][0]["state"] == "skipped"
    assert body["items"][1]["moved_at"] is not None


async def test_items_filters_by_state(ops_client: AsyncClient, api_repo: Repository) -> None:
    await _seed(api_repo)

    response = await ops_client.get("transfer/items", params={"state": "moved"})

    body = response.json()
    assert body["total"] == 1
    assert [item["fid"] for item in body["items"]] == ["f1"]


async def test_items_pages_with_total(ops_client: AsyncClient, api_repo: Repository) -> None:
    await _seed(api_repo)

    response = await ops_client.get("transfer/items", params={"offset": 1, "limit": 1})

    body = response.json()
    assert body["total"] == 3, "total 不受 limit 截断"
    assert len(body["items"]) == 1


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"offset": -1},
        {"state": "moving"},
    ],
)
async def test_items_rejects_illegal_query(
    ops_client: AsyncClient, api_repo: Repository, params: dict[str, int | str]
) -> None:
    response = await ops_client.get("transfer/items", params=params)

    assert response.status_code == 422


async def test_transfer_has_no_write_route(ops_client: AsyncClient) -> None:
    """搬移只能经 ``POST /tasks`` 入队: 路由里跑长循环既占事件循环也没法取消."""
    response = await ops_client.post("transfer/run")

    assert response.status_code in (404, 405)
