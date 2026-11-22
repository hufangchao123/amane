"""扫码登录: 只验三次 HTTP 的形状与状态映射, 不连真 115.

传输层注入, 因此用例不依赖网络; 真实接口的等待 / 已扫 / 确认分支由线上扫码覆盖.
"""

from __future__ import annotations

import httpx2
import pytest

from amane.enums import TransferLoginClient
from amane.transfer.login import QR_TTL_SECONDS, QrLoginError, TransferQrLogin

QR = "https://qr.test"
PASSPORT = "https://passport.test"


def _login(handler: httpx2.MockTransport) -> TransferQrLogin:
    return TransferQrLogin(qr_base=QR, passport_base=PASSPORT, transport=handler)


TOKEN_CASES = [pytest.param(client, id=client.value) for client in TransferLoginClient]


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("client", TOKEN_CASES)
async def test_create_uses_client_specific_path(client: TransferLoginClient) -> None:
    """不同客户端各走一条域名路径, 参数原样回传给前端."""
    seen: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        return httpx2.Response(
            200,
            json={
                "state": 1,
                "data": {"uid": "u1", "time": 1795367285, "sign": "s1", "qrcode": "https://115.com/scan/dg-u1"},
            },
        )

    ticket = await _login(httpx2.MockTransport(handle)).create(client)
    assert seen == [f"/api/1.0/{client.value}/1.0/token/"]
    assert (ticket.uid, ticket.time, ticket.sign) == ("u1", "1795367285", "s1")
    assert ticket.qrcode_url == "https://115.com/scan/dg-u1"
    assert ticket.expires_in == QR_TTL_SECONDS


def _status(payload: dict[str, object]) -> httpx2.MockTransport:
    return httpx2.MockTransport(lambda request: httpx2.Response(200, json=payload))


POLL_CASES = [
    pytest.param({"state": False, "code": 90038, "message": "请扫描二维码"}, "waiting", id="waiting-by-code"),
    pytest.param({"state": False, "message": "请扫描二维码"}, "waiting", id="waiting-by-message"),
    pytest.param({"state": False, "message": "已扫描, 请在手机上确认"}, "scanned", id="scanned"),
    pytest.param({"state": False, "code": 40101017, "message": "老乡验证失败！"}, "expired", id="expired-by-code"),
    pytest.param({"state": False, "message": "二维码已过期"}, "expired", id="expired-by-message"),
    pytest.param({"state": False, "code": 12345, "message": "未知"}, "waiting", id="unknown-is-waiting"),
]


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize(("payload", "expected"), POLL_CASES)
async def test_poll_maps_state(payload: dict[str, object], expected: str) -> None:
    """未确认时只映射状态: 扫过但没确认不能当成登录成功."""
    poll = await _login(_status(payload)).poll(client=TransferLoginClient.WEB, uid="u1", time="t1", sign="s1")
    assert poll.status == expected
    assert poll.cookies is None


@pytest.mark.asyncio(loop_scope="function")
async def test_poll_confirmed_with_cookie_dict() -> None:
    """确认后 data.cookie 是字典时拼成请求头值."""
    transport = _status({"state": True, "data": {"cookie": {"UID": "1_A1", "CID": "c1", "SEID": "s1"}}})
    poll = await _login(transport).poll(client=TransferLoginClient.ANDROID, uid="u1", time="t1", sign="s1")
    assert poll.status == "confirmed"
    assert poll.cookies == "UID=1_A1; CID=c1; SEID=s1"


@pytest.mark.asyncio(loop_scope="function")
async def test_poll_confirmed_web_exchanges_uid_for_cookie() -> None:
    """网页端确认后不给 Cookie, 要拿 uid 去 passport 换一次."""
    calls: list[tuple[str, str]] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/status/"):
            return httpx2.Response(200, json={"state": True, "data": {}})
        assert request.content == b"account=u1&app=web"
        return httpx2.Response(200, json={"state": 1, "data": {"cookie": {"UID": "1_A1", "CID": "c1"}}})

    poll = await _login(httpx2.MockTransport(handle)).poll(
        client=TransferLoginClient.WEB, uid="u1", time="t1", sign="s1"
    )
    assert poll.status == "confirmed"
    assert poll.cookies == "UID=1_A1; CID=c1"
    assert calls == [("GET", "/api/1.0/web/1.0/status/"), ("POST", "/app/1.0/web/1.0/login/qrcode/")]


@pytest.mark.asyncio(loop_scope="function")
async def test_confirmed_without_cookie_raises() -> None:
    """确认了却拿不到 Cookie 是错误, 不能当成登录成功."""
    transport = _status({"state": True, "data": {}})
    with pytest.raises(QrLoginError, match="没有拿到 Cookie"):
        await _login(transport).poll(client=TransferLoginClient.ANDROID, uid="u1", time="t1", sign="s1")


@pytest.mark.asyncio(loop_scope="function")
async def test_image_returns_bytes_for_client_path() -> None:
    """二维码图片按客户端路径取; 只有 image/* 才透传给浏览器."""
    seen: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        return httpx2.Response(200, content=b"\x89PNG\r\n\x1a\nqr", headers={"content-type": "image/png"})

    image = await _login(httpx2.MockTransport(handle)).image(client=TransferLoginClient.IOS, uid="u1")
    assert image.startswith(b"\x89PNG")
    assert seen == ["/api/1.0/ios/1.0/qrcode/"]


@pytest.mark.asyncio(loop_scope="function")
async def test_image_rejects_non_image() -> None:
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json={"state": 1}))
    with pytest.raises(QrLoginError, match="不是图片"):
        await _login(transport).image(client=TransferLoginClient.WEB, uid="u1")


@pytest.mark.asyncio(loop_scope="function")
async def test_incomplete_token_payload_raises() -> None:
    transport = _status({"state": 1, "data": {"uid": "u1"}})
    with pytest.raises(QrLoginError, match="不完整"):
        await _login(transport).create(TransferLoginClient.WEB)


@pytest.mark.asyncio(loop_scope="function")
async def test_http_error_becomes_qr_error() -> None:
    transport = httpx2.MockTransport(lambda request: httpx2.Response(500))
    with pytest.raises(QrLoginError, match="返回 500"):
        await _login(transport).create(TransferLoginClient.WEB)
