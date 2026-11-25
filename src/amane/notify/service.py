"""推送实现: 按渠道拼请求, 把失败折成结果而不是抛给调用方.

通知失败不该让任务链中断, 因此这里返回 ``NotifyOutcome``; 调用方负责把它记进发送记录.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx2
import structlog

from ..config.manager import NotifyConfig
from ..utils.urls import redact_text
from .channels import build_request

if TYPE_CHECKING:
    from httpx2 import AsyncBaseTransport

logger = structlog.get_logger()


@dataclass(frozen=True)
class NotifyOutcome:
    """一次推送的结果; ``status`` 为空表示没拿到响应."""

    ok: bool
    channel: str
    status: int | None = None
    error: str | None = None


class Notifier:
    """一个渠道的发送器; 配置为空或未启用时不发请求."""

    def __init__(self, config: NotifyConfig, *, transport: AsyncBaseTransport | None = None) -> None:
        self._config = config
        self._transport = transport

    @property
    def ready(self) -> bool:
        return self._config.enabled and bool(self._config.url)

    async def send(self, title: str, body: str) -> NotifyOutcome:
        if not self._config.enabled:
            return NotifyOutcome(ok=False, channel=self._config.kind, error="通知未启用")
        if not self._config.url:
            return NotifyOutcome(ok=False, channel=self._config.kind, error="通知地址为空")
        request = build_request(self._config.kind, self._config.url, title, body)
        try:
            async with httpx2.AsyncClient(
                timeout=self._config.timeout_seconds, transport=self._transport, trust_env=False
            ) as client:
                response = await client.request(request.method, request.url, json=request.json, data=request.form)
        except Exception as error:
            # 端口越界、域名解析失败、TLS 握手失败都可能抛, 有时还裹在 anyio 的 ExceptionGroup 里.
            # 只捕 Exception: 取消与中断仍要能传出去.
            reason = redact_text(str(error))
            logger.warning("notify failed", channel=self._config.kind, reason=reason)
            return NotifyOutcome(ok=False, channel=self._config.kind, error=reason)
        ok = response.status_code < 400
        if not ok:
            logger.warning("notify rejected", channel=self._config.kind, status=response.status_code)
        return NotifyOutcome(
            ok=ok,
            channel=self._config.kind,
            status=response.status_code,
            error=None if ok else f"渠道返回 {response.status_code}",
        )
