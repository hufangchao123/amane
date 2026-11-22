"""115 扫码登录: 取一张二维码, 轮询到确认, 把 Cookie 交给配置层保存.

只做三件事: 换 token、轮询状态、把结果拼成 Cookie 头. Cookie 只在返回值里出现一次, 由调用方
写进配置; 本模块不记日志、不落盘、不进异常消息.

为什么不复用 `webapi.115.com` 的客户端: 那条路要求先有 Cookie, 这里正是拿 Cookie 的前一步, 域名
与超时都不同 (登录接口慢且会长时间挂住, 因此用独立的短超时).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import httpx2

from .client import _USER_AGENT

if TYPE_CHECKING:
    from ..enums import TransferLoginClient

QR_BASE = "https://qrcodeapi.115.com"
PASSPORT_BASE = "https://passportapi.115.com"

TIMEOUT_SECONDS = 10.0
QR_TTL_SECONDS = 240
"""二维码有效期. 115 那边是 4 分钟左右, 取同一量级; 过期后页面要重新取一张."""

QrStatus = Literal["waiting", "scanned", "confirmed", "expired"]

IMAGE_MAX_BYTES = 256 * 1024
"""二维码图片的读取上限: 正常不足 1 KB, 留足余量但拒绝把大文件读进内存."""

_WAITING_CODE = 90038
_SCANNED_CODES = frozenset({90039, 90040})
_EXPIRED_CODES = frozenset({40101017, 40101018})


class QrLoginError(RuntimeError):
    """扫码登录失败: 接口不可用、返回不完整, 或确认后没拿到 Cookie."""


@dataclass(frozen=True, slots=True)
class QrTicket:
    """一张待扫的二维码. ``uid`` / ``time`` / ``sign`` 是轮询时要原样带回去的凭据."""

    uid: str
    time: str
    sign: str
    qrcode_url: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class QrPoll:
    """一次轮询的结果; 只有 ``confirmed`` 带 Cookie."""

    status: QrStatus
    cookies: str | None = None


class TransferQrLogin:
    """扫码登录的两次调用. 传输层可注入, 因此测试不需要真的连 115."""

    def __init__(
        self,
        *,
        qr_base: str = QR_BASE,
        passport_base: str = PASSPORT_BASE,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._qr_base = qr_base.rstrip("/")
        self._passport_base = passport_base.rstrip("/")
        self._transport = transport

    async def create(self, client: TransferLoginClient) -> QrTicket:
        url = f"{self._qr_base}/api/1.0/{client.value}/1.0/token/"
        payload = await self._request(url)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise QrLoginError("二维码接口没有返回数据")
        uid = str(data.get("uid") or "")
        stamp = str(data.get("time") or "")
        sign = str(data.get("sign") or "")
        qrcode = str(data.get("qrcode") or "")
        if not (uid and stamp and sign and qrcode):
            raise QrLoginError("二维码接口返回不完整")
        return QrTicket(uid=uid, time=stamp, sign=sign, qrcode_url=qrcode, expires_in=QR_TTL_SECONDS)

    async def poll(self, *, client: TransferLoginClient, uid: str, time: str, sign: str) -> QrPoll:
        url = f"{self._qr_base}/api/1.0/{client.value}/1.0/status/"
        payload = await self._request(url, params={"uid": uid, "time": time, "sign": sign})
        if payload.get("state"):
            cookies = _cookies_of(payload)
            if cookies is None and client.value == "web":
                # 网页端确认后不给 Cookie, 要拿 uid 去 passport 换一次.
                cookies = await self._exchange_web(uid)
            if cookies is None:
                raise QrLoginError("登录已确认, 但没有拿到 Cookie")
            return QrPoll(status="confirmed", cookies=cookies)

        message = str(payload.get("message") or "")
        code = payload.get("code")
        if "过期" in message or code in _EXPIRED_CODES:
            return QrPoll(status="expired")
        if "确认" in message:
            return QrPoll(status="scanned")
        if code == _WAITING_CODE or "扫描" in message:
            return QrPoll(status="waiting")
        return QrPoll(status="waiting")

    async def image(self, *, client: TransferLoginClient, uid: str) -> bytes:
        """取二维码图片本身. 前端直接显示, 因此后端转一次, 浏览器不必访问 115 的域名."""
        url = f"{self._qr_base}/api/1.0/{client.value}/1.0/qrcode/"
        headers = {"User-Agent": _USER_AGENT, "Referer": "https://115.com/"}
        try:
            async with httpx2.AsyncClient(timeout=TIMEOUT_SECONDS, transport=self._transport) as client_:
                response = await client_.get(url, params={"uid": uid}, headers=headers)
        except httpx2.HTTPError as error:
            raise QrLoginError(f"二维码图片不可用: {type(error).__name__}") from error
        if response.status_code >= 400:
            raise QrLoginError(f"二维码图片返回 {response.status_code}")
        if not response.headers.get("content-type", "").startswith("image/"):
            raise QrLoginError("二维码接口返回的不是图片")
        if len(response.content) > IMAGE_MAX_BYTES:
            raise QrLoginError("二维码图片过大")
        return response.content

    async def _exchange_web(self, uid: str) -> str | None:
        url = f"{self._passport_base}/app/1.0/web/1.0/login/qrcode/"
        payload = await self._request(url, method="POST", data={"account": uid, "app": "web"})
        return _cookies_of(payload)

    async def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> dict[str, object]:
        headers = {"User-Agent": _USER_AGENT, "Referer": "https://115.com/"}
        try:
            async with httpx2.AsyncClient(timeout=TIMEOUT_SECONDS, transport=self._transport) as client:
                response = await client.request(method, url, params=params, data=data, headers=headers)
        except httpx2.HTTPError as error:
            raise QrLoginError(f"扫码接口不可用: {type(error).__name__}") from error
        if response.status_code >= 400:
            raise QrLoginError(f"扫码接口返回 {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise QrLoginError("扫码接口返回的不是 JSON") from error
        if not isinstance(payload, dict):
            raise QrLoginError("扫码接口返回格式不对")
        return payload


def _cookies_of(payload: dict[str, object]) -> str | None:
    """把 ``data.cookie`` 归一成请求头值; 字典按 ``k=v; k2=v2`` 拼接, 字符串原样用."""
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    cookie = data.get("cookie")
    if isinstance(cookie, str):
        return cookie.strip() or None
    if isinstance(cookie, dict):
        pairs = [f"{key}={value}" for key, value in cookie.items() if value is not None and str(value)]
        return "; ".join(pairs) or None
    return None
