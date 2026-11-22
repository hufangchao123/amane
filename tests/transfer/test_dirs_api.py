"""目录列表端点: 只在有 Cookie 时出去读, 只回文件夹, 失败转 502 且不带 Cookie."""

from __future__ import annotations

import pytest

from amane.api.routes import transfer as transfer_routes
from amane.transfer.client import CloudEntry, TransferError

pytestmark = pytest.mark.asyncio(loop_scope="function")


class _FakeClient:
    """替身: 按构造时给的条目返回, 记下被读的目录."""

    def __init__(self, entries: list[CloudEntry], error: TransferError | None = None) -> None:
        self.entries = entries
        self.error = error
        self.asked: list[str] = []
        self.closed = False

    def list_dir(self, cid: str) -> list[CloudEntry]:
        self.asked.append(cid)
        if self.error is not None:
            raise self.error
        return self.entries

    def move(self, fid: str, pid: str) -> None:  # pragma: no cover - 端点不搬
        raise AssertionError

    def trash(self, fid: str) -> None:  # pragma: no cover - 端点不删
        raise AssertionError

    def close(self) -> None:
        self.closed = True


def _entry(name: str, *, cid: str | None = None) -> CloudEntry:
    return CloudEntry(name=name, fid=None if cid else f"f-{name}", cid=cid, size=0, is_dir=cid is not None)


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    fake = _FakeClient([_entry("影视", cid="100"), _entry("日韩", cid="200"), _entry("a.mp4")])
    monkeypatch.setattr(transfer_routes, "_cloud_client", lambda cookies: fake)
    return fake


async def test_requires_cookies(ops_client, ops_app) -> None:
    response = await ops_client.get("transfer/dirs")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["items"] == []
    assert body["reason"] == "115 中转未配置 Cookie"


async def test_lists_folders_sorted(ops_client, ops_app, fake_client: _FakeClient) -> None:
    ops_app.state.runtime.config.hot.transfer.cookies = "UID=1_A1"

    response = await ops_client.get("transfer/dirs", params={"cid": "0"})

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["cid"] == "0"
    # 只回文件夹, 按名字排序
    assert [item["name"] for item in body["items"]] == ["影视", "日韩"]
    assert [item["id"] for item in body["items"]] == ["100", "200"]
    assert fake_client.asked == ["0"]
    assert fake_client.closed is True


async def test_error_becomes_502_without_cookie(ops_client, ops_app, monkeypatch: pytest.MonkeyPatch) -> None:
    ops_app.state.runtime.config.hot.transfer.cookies = "UID=1_A1"
    broken = _FakeClient([], error=TransferError("115 返回 state=false"))
    monkeypatch.setattr(transfer_routes, "_cloud_client", lambda cookies: broken)

    response = await ops_client.get("transfer/dirs", params={"cid": "100"})

    assert response.status_code == 502
    assert "读取目录失败" in response.json()["detail"]
    assert "1_A1" not in response.text
    assert broken.closed is True
