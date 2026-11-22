"""通知状态: 渠道是否就绪与最近几次推送的结果."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from ...config.manager import NotifyKind


class NotifyRecordResponse(BaseModel):
    """一条推送留痕; 不含渠道地址."""

    created_at: datetime
    kind: str
    channel: str
    title: str
    ok: bool
    error: str | None = None


class NotifyStatusResponse(BaseModel):
    enabled: bool
    kind: NotifyKind
    has_url: bool
    """地址是否已填; 只报有无, 不回显内容."""

    records: list[NotifyRecordResponse]
