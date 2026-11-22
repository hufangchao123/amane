"""通知渠道的状态与最近推送记录."""

from __future__ import annotations

from fastapi import APIRouter

from ..deps import ConfigDep, RepoDep
from ..models.notify import NotifyRecordResponse, NotifyStatusResponse

router = APIRouter(prefix="/notify", tags=["notify"])

RECORD_LIMIT = 20


@router.get("/status", response_model=NotifyStatusResponse)
async def notify_status(config: ConfigDep, repo: RepoDep) -> NotifyStatusResponse:
    """报渠道就绪情况与最近若干条推送; 记录里没有地址."""
    notify = config.hot.notify
    records = await repo.list_notify_records(limit=RECORD_LIMIT)
    return NotifyStatusResponse(
        enabled=notify.enabled,
        kind=notify.kind,
        has_url=bool(notify.url),
        records=[
            NotifyRecordResponse(
                created_at=record.created_at,
                kind=record.kind,
                channel=record.channel,
                title=record.title,
                ok=record.ok,
                error=record.error,
            )
            for record in records
        ],
    )
