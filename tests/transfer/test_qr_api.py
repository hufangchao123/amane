"""扫码登录的路由: 取码与轮询; 确认后 Cookie 写进配置, 但响应里不回显."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from amane.api.routes import transfer as transfer_routes
from amane.enums import TransferLoginClient
from amane.transfer.login import QrPoll, QrTicket

if TYPE_CHECKING:
    from fastapi import FastAPI
    from httpx2 import AsyncClient

pytestmark = pytest.mark.asyncio(loop_scope="function")

TICKET = QrTicket(uid="u1", time="ts1", sign="sg1", qrcode_url="https://115.com/scan/dg-u1", expires_in=240)


class _FakeLogin:
    """替身: 记录被要求换码的客户端, 轮询结果按构造时给的固定值返回."""

    def __init__(self, poll: QrPoll) -> None:
        self.poll_result = poll
        self.clients: list[TransferLoginClient] = []

    async def create(self, client: TransferLoginClient) -> QrTicket:
        self.clients.append(client)
        return TICKET

    async def poll(self, *, client: TransferLoginClient, uid: str, time: str, sign: str) -> QrPoll:
        self.clients.append(client)
        return self.poll_result


@pytest.fixture
def fake_login(monkeypatch: pytest.MonkeyPatch) -> _FakeLogin:
    """默认换成「等待扫码」, 用例按需改 `poll_result`."""
    fake = _FakeLogin(QrPoll(status="waiting"))
    monkeypatch.setattr(transfer_routes, "_qr_login", lambda: fake)
    return fake


async def test_start_returns_ticket(ops_client: AsyncClient, fake_login: _FakeLogin) -> None:
    response = await ops_client.post("transfer/qrcode", json={"client": "android"})

    assert response.status_code == 201
    body = response.json()
    assert (body["uid"], body["time"], body["sign"]) == ("u1", "ts1", "sg1")
    assert body["qrcode_url"] == "https://115.com/scan/dg-u1"
    assert body["expires_in"] == 240
    assert fake_login.clients == [TransferLoginClient.ANDROID]


async def test_start_defaults_to_web(ops_client: AsyncClient, fake_login: _FakeLogin) -> None:
    response = await ops_client.post("transfer/qrcode", json={})

    assert response.status_code == 201
    assert fake_login.clients == [TransferLoginClient.WEB]


async def test_status_waiting_does_not_touch_config(ops_client: AsyncClient, fake_login: _FakeLogin) -> None:
    response = await ops_client.get(
        "transfer/qrcode/status", params={"client": "web", "uid": "u1", "time": "t1", "sign": "s1"}
    )

    assert response.status_code == 200
    assert response.json() == {"status": "waiting", "saved": False}


async def test_status_confirmed_saves_cookies(
    ops_client: AsyncClient, ops_app: FastAPI, fake_login: _FakeLogin
) -> None:
    fake_login.poll_result = QrPoll(status="confirmed", cookies="UID=1_A1; CID=c1")

    response = await ops_client.get(
        "transfer/qrcode/status", params={"client": "web", "uid": "u1", "time": "ts1", "sign": "sg1"}
    )

    assert response.status_code == 200
    assert response.json() == {"status": "confirmed", "saved": True}
    # Cookie 只进配置, 不出现在响应里
    assert "1_A1" not in response.text
    assert ops_app.state.runtime.config.hot.transfer.cookies == "UID=1_A1; CID=c1"


async def test_status_rejects_missing_params(ops_client: AsyncClient, fake_login: _FakeLogin) -> None:
    response = await ops_client.get(
        "transfer/qrcode/status", params={"client": "web", "uid": "", "time": "t1", "sign": "s1"}
    )

    assert response.status_code == 422
