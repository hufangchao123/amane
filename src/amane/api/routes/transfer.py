"""115 中转的状态读取; 搬移本体是 ``TRANSFER_115`` 任务, 这里不开写端点."""

from fastapi import APIRouter

from ...transfer import TransferService
from ..deps import ConfigDep
from ..models.transfer import TransferStatusResponse

router = APIRouter(prefix="/transfer", tags=["transfer"])


@router.get("/status", response_model=TransferStatusResponse)
async def transfer_status(config: ConfigDep) -> TransferStatusResponse:
    service = TransferService(config.hot.transfer)
    summary = await service.summary()
    return TransferStatusResponse(enabled=service.enabled, **summary.model_dump())
