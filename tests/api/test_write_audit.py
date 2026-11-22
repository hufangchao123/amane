"""写操作留痕: 写接口留下记录, 读接口不留; 清空接口返回 204."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from httpx2 import AsyncClient


@pytest.mark.asyncio(loop_scope="function")
async def test_write_request_is_audited(client: AsyncClient, safe_path: Path) -> None:
    assert (await client.post("libraries", json={"path": str(safe_path), "name": "留痕"})).status_code == 201
    listed = (await client.get("audit")).json()
    assert listed["total"] >= 1
    entry = listed["items"][0]
    assert entry["method"] == "POST"
    assert entry["path"] == "/api/libraries"
    assert entry["status"] == 201


@pytest.mark.asyncio(loop_scope="function")
async def test_read_request_is_not_audited(client: AsyncClient) -> None:
    await client.get("libraries")
    assert (await client.get("audit")).json()["total"] == 0


@pytest.mark.asyncio(loop_scope="function")
async def test_clear_empties_records(client: AsyncClient, safe_path: Path) -> None:
    assert (await client.post("libraries", json={"path": str(safe_path), "name": "留痕"})).status_code == 201
    assert (await client.delete("audit")).status_code == 204
    # 清空动作本身也是写请求, 因此清完会留下这一条
    listed = (await client.get("audit")).json()
    assert listed["total"] == 1
    assert listed["items"][0]["method"] == "DELETE"
    assert listed["items"][0]["path"] == "/api/audit"
