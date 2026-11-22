"""NOTIFY handler: 消息原样转发, 周报现算摘要, 两种都留一条记录."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

from amane.config.manager import HotSettings, NotifyConfig
from amane.db.models import TaskType
from amane.handlers import NotifyHandler, NotifyPayload
from amane.notify.service import Notifier, NotifyOutcome

if TYPE_CHECKING:
    from amane.db.repository import Repository


def _hot(*, enabled: bool = False, url: str = "") -> HotSettings:
    return HotSettings(notify=NotifyConfig(enabled=enabled, url=url))


def _factory(outcome: NotifyOutcome) -> tuple[Callable[[NotifyConfig], Notifier], list[tuple[str, str]]]:
    """伪造发送器: 记下标题与正文, 回固定结果."""
    sent: list[tuple[str, str]] = []

    class _Fake(Notifier):
        async def send(self, title: str, body: str) -> NotifyOutcome:
            sent.append((title, body))
            return outcome

    return (lambda config: _Fake(config)), sent


@pytest.mark.asyncio(loop_scope="function")
async def test_message_forwards_title_and_body(repo: Repository) -> None:
    factory, sent = _factory(NotifyOutcome(ok=True, channel="webhook", status=200))
    handler = NotifyHandler(repo, _hot(enabled=True, url="https://hook.test/a"), notifier_factory=factory)
    result = await handler.handle(NotifyPayload(title="标题", body="正文"))
    assert result.success
    assert sent == [("标题", "正文")]
    assert result.result is not None
    assert result.result.channel == "webhook"
    assert result.result.status == 200
    records = await repo.list_notify_records()
    assert [record.title for record in records] == ["标题"]
    assert records[0].ok is True


@pytest.mark.asyncio(loop_scope="function")
async def test_disabled_reports_failure_and_keeps_record(repo: Repository) -> None:
    factory, _ = _factory(NotifyOutcome(ok=False, channel="webhook", error="通知未启用"))
    handler = NotifyHandler(repo, _hot(enabled=False), notifier_factory=factory)
    result = await handler.handle(NotifyPayload(title="标题", body="正文"))
    assert not result.success
    assert result.error == "通知未启用"
    records = await repo.list_notify_records()
    assert records[0].ok is False
    assert records[0].error == "通知未启用"


@pytest.mark.asyncio(loop_scope="function")
async def test_digest_builds_summary_from_repository(repo: Repository) -> None:
    """正文留空时按库内计数拼摘要, 并带上最近失败任务."""
    library = await repo.create_library(name="日韩", path="/media/日韩")
    assert library.id is not None
    await repo.create_task(TaskType.REFRESH, {})
    factory, sent = _factory(NotifyOutcome(ok=True, channel="webhook", status=204))
    handler = NotifyHandler(repo, _hot(enabled=True, url="https://hook.test/a"), notifier_factory=factory)
    result = await handler.handle(NotifyPayload(kind="digest"))
    assert result.success
    title, body = sent[0]
    assert title == "Amane 周报"
    assert "排队 1" in body
    assert "115 中转" in body
    assert result.result is not None
    assert result.result.digest_days == 7
