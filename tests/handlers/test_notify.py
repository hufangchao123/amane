"""NOTIFY handler: 消息原样转发, 周报现算摘要, 两种都留一条记录."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from amane.config.manager import HotSettings, NotifyConfig
from amane.db.models import TaskType
from amane.handlers import NotifyHandler, NotifyPayload
from amane.handlers.models import NOTIFY_BODY_LIMIT, NOTIFY_TITLE_LIMIT
from amane.notify.service import Notifier, NotifyOutcome

if TYPE_CHECKING:
    from amane.db.repository import Repository


def _hot(*, enabled: bool = False, url: str = "", sections: list[str] | None = None) -> HotSettings:
    notify = NotifyConfig(enabled=enabled, url=url)
    if sections is not None:
        notify = notify.model_copy(update={"sections": sections})
    return HotSettings(notify=notify)


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
    await repo.create_task(TaskType.SCRAPE, {})
    factory, sent = _factory(NotifyOutcome(ok=True, channel="webhook", status=204))
    handler = NotifyHandler(repo, _hot(enabled=True, url="https://hook.test/a"), notifier_factory=factory)
    result = await handler.handle(NotifyPayload(kind="digest"))
    assert result.success
    title, body = sent[0]
    assert title == "Amane 周报"
    # 原有功能的类型也要出现在摘要里, 不能只报移植来的几项
    assert "扫描入库 1" in body
    assert "刮削 1" in body
    assert "排队 2" in body
    assert "媒体库: 0 份文件" in body
    assert "洗版回收站" in body
    assert result.result is not None
    assert result.result.digest_days == 7


@pytest.mark.asyncio(loop_scope="function")
async def test_digest_only_includes_selected_sections(repo: Repository) -> None:
    """周报内容按配置裁剪: 只要媒体库那一段时, 队列与洗版都不该出现."""
    await repo.create_library(name="日韩", path="/media/日韩")
    factory, sent = _factory(NotifyOutcome(ok=True, channel="webhook", status=200))
    handler = NotifyHandler(
        repo,
        _hot(enabled=True, url="https://hook.test/a", sections=["library"]),
        notifier_factory=factory,
    )
    assert (await handler.handle(NotifyPayload(kind="digest"))).success
    body = sent[0][1]
    assert body == "媒体库: 0 份文件, 其中缺外挂字幕 0 份"


@pytest.mark.asyncio(loop_scope="function")
async def test_digest_without_sections_is_empty(repo: Repository) -> None:
    """全部关掉时只留标题, 正文为空."""
    factory, sent = _factory(NotifyOutcome(ok=True, channel="webhook", status=200))
    handler = NotifyHandler(
        repo,
        _hot(enabled=True, url="https://hook.test/a", sections=[]),
        notifier_factory=factory,
    )
    assert (await handler.handle(NotifyPayload(kind="digest"))).success
    assert sent[0][1] == ""


def test_payload_rejects_over_long_title() -> None:
    """载荷进任务表, 批量提交时无上限会被写成几 MB 的 JSON."""
    with pytest.raises(ValidationError):
        NotifyPayload(title="字" * (NOTIFY_TITLE_LIMIT + 1))


def test_payload_rejects_over_long_body() -> None:
    with pytest.raises(ValidationError):
        NotifyPayload(body="字" * (NOTIFY_BODY_LIMIT + 1))
