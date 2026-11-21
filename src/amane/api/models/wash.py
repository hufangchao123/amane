"""洗版的响应模型与动作请求.

回收站行按 ``wash_records`` 的列直接摊平: 前端要展示「留下的那份 / 移走的那份 / 到期时间」,
不需要再拼一层嵌套.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Self

from pydantic import BaseModel, Field

from ...db.models import WashRecordKind, WashRecordState
from ...library.wash import WashCandidate

if TYPE_CHECKING:
    from ...db.models import WashRecord


class WashStatusResponse(BaseModel):
    """状态卡: 待处理组数 (估算)、回收站条数、可还原体积."""

    enabled: bool
    groups: int
    trashed: int
    restorable_bytes: int


class WashCandidateListResponse(BaseModel):
    """候选分组的一页."""

    items: list[WashCandidate]
    total: int


class WashRecordItem(BaseModel):
    """回收站的一行."""

    id: int
    kind: WashRecordKind
    group_key: str
    kept_path: str
    removed_path: str
    size_bytes: int
    state: WashRecordState
    created_at: datetime
    expires_at: datetime | None

    @classmethod
    def of(cls, record: WashRecord) -> Self:
        assert record.id is not None
        return cls(
            id=record.id,
            kind=record.kind,
            group_key=record.group_key,
            kept_path=record.kept_path,
            removed_path=record.removed_path,
            size_bytes=record.size_bytes,
            state=record.state,
            created_at=record.created_at,
            expires_at=record.expires_at,
        )


class WashRecordListResponse(BaseModel):
    items: list[WashRecordItem]
    total: int


class WashRecordActionRequest(BaseModel):
    """还原 / 清理的请求体: 一次一批记录 id.

    上限与列表页大小一致: 单批过大时任务里的进度回调与错误汇总会失去意义.
    """

    ids: Annotated[list[int], Field(min_length=1, max_length=200)]
