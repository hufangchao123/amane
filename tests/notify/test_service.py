"""推送服务: 未启用不发请求, 渠道出错折成结果."""

from __future__ import annotations

import httpx2
import pytest

from amane.config.manager import NotifyConfig
from amane.notify.service import Notifier


def _config(*, enabled: bool = False, url: str = "") -> NotifyConfig:
    return NotifyConfig(enabled=enabled, url=url)


def _transport(status: int) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert "title" in request.content.decode() or request.method == "GET"
        return httpx2.Response(status, json={"ok": True})

    return httpx2.MockTransport(handler)


@pytest.mark.asyncio(loop_scope="function")
async def test_disabled_sends_nothing() -> None:
    notifier = Notifier(_config(enabled=False, url="https://hook.test/a"))
    outcome = await notifier.send("标题", "正文")
    assert outcome.ok is False
    assert outcome.error == "通知未启用"
    assert notifier.ready is False


@pytest.mark.asyncio(loop_scope="function")
async def test_missing_url_reports_reason() -> None:
    notifier = Notifier(_config(enabled=True, url=""))
    outcome = await notifier.send("标题", "正文")
    assert outcome.ok is False
    assert outcome.error == "通知地址为空"
    assert notifier.ready is False


@pytest.mark.asyncio(loop_scope="function")
async def test_accepted_response_is_success() -> None:
    notifier = Notifier(_config(enabled=True, url="https://hook.test/a"), transport=_transport(200))
    outcome = await notifier.send("标题", "正文")
    assert outcome.ok is True
    assert outcome.status == 200
    assert notifier.ready is True


@pytest.mark.asyncio(loop_scope="function")
async def test_rejected_response_carries_status() -> None:
    notifier = Notifier(_config(enabled=True, url="https://hook.test/a"), transport=_transport(500))
    outcome = await notifier.send("标题", "正文")
    assert outcome.ok is False
    assert outcome.status == 500
    assert outcome.error == "渠道返回 500"


@pytest.mark.asyncio(loop_scope="function")
async def test_unreachable_address_fails_without_raising() -> None:
    """连不上的地址 (连拒绝) 也要折成失败结果, 不能炸穿任务.

    端口越界那种在配置阶段就被拒, 见 `tests/config/test_config_manager.py`.
    """
    notifier = Notifier(NotifyConfig(enabled=True, url="http://127.0.0.1:9/hook"))
    outcome = await notifier.send("标题", "正文")
    assert outcome.ok is False
    assert outcome.error
