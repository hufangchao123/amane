from datetime import datetime

from pydantic import BaseModel

from ...db.models import TransferItemState


class TransferStatusResponse(BaseModel):
    enabled: bool
    pending: int
    moved: int
    skipped: int
    failed: int


class TransferItemResponse(BaseModel):
    """一条逐文件状态; ``error`` 在跳过时承载原因, 在失败时承载错误."""

    id: int
    fid: str
    file_name: str
    state: TransferItemState
    attempts: int
    error: str | None
    moved_at: datetime | None
    updated_at: datetime


class TransferItemListResponse(BaseModel):
    items: list[TransferItemResponse]
    total: int
    """不受本页 ``limit`` 截断."""
