"""115 中转的只读查询; 搬移本体是 ``TRANSFER_115`` 任务, 这里不开写端点.

"立即中转"走 ``POST /tasks``: 搬移要几十秒到几分钟, 路由里跑长循环会占住事件循环, 也没法取消.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from ...db.models import TransferItemState
from ...transfer import TransferService, TransferSummary
from ...utils.model import to_resp
from ..deps import ConfigDep, RepoDep
from ..models.transfer import TransferItemListResponse, TransferItemResponse, TransferStatusResponse

router = APIRouter(prefix="/transfer", tags=["transfer"])


@router.get("/status")
async def transfer_status(config: ConfigDep, repo: RepoDep) -> TransferStatusResponse:
    service = TransferService(config.hot.transfer)
    summary = TransferSummary.from_counts(await repo.transfer_state_counts())
    return TransferStatusResponse(enabled=service.enabled, **summary.model_dump())


@router.get("/items")
async def list_transfer_items(
    repo: RepoDep,
    state: Annotated[TransferItemState | None, Query(description="仅该状态的条目")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200, description="每页条数")] = 50,
) -> TransferItemListResponse:
    rows, total = await repo.list_transfer_items(state=state, offset=offset, limit=limit)
    return TransferItemListResponse(items=[to_resp(TransferItemResponse, row) for row in rows], total=total)
