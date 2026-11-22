"""写操作留痕的读出面."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class AuditRecordResponse(BaseModel):
    id: int
    created_at: datetime
    method: str
    path: str
    query: str
    status: int
    duration_ms: int
    client: str


class AuditListResponse(BaseModel):
    items: list[AuditRecordResponse]
    total: int
