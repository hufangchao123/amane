"""写操作留痕: 列表与清空."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from ..deps import RepoDep
from ..models.audit import AuditListResponse, AuditRecordResponse

router = APIRouter(prefix="/audit", tags=["audit"])

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@router.get("", response_model=AuditListResponse)
async def list_audit(
    repo: RepoDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
    method: str | None = None,
) -> AuditListResponse:
    """按时间倒序分页; ``method`` 用来只看某一类写请求."""
    records = await repo.list_audit_records(limit=limit, offset=offset, method=method)
    total = await repo.count_audit_records(method)
    return AuditListResponse(
        items=[
            AuditRecordResponse(
                id=int(record.id or 0),
                created_at=record.created_at,
                method=record.method,
                path=record.path,
                query=record.query,
                status=record.status,
                duration_ms=record.duration_ms,
                client=record.client,
            )
            for record in records
        ],
        total=total,
    )


@router.delete("", status_code=204)
async def clear_audit(repo: RepoDep) -> None:
    """清空全部留痕; 不做分批, 由调用方自行确认."""
    await repo.clear_audit_records()
