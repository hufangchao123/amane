"""洗版的状态读取; 扫候选与执行分别是 ``WASH_SCAN`` / ``WASH_APPLY`` 任务."""

from fastapi import APIRouter

from ...library.wash import WashService
from ..deps import ConfigDep
from ..models.wash import WashStatusResponse

router = APIRouter(prefix="/wash", tags=["wash"])


@router.get("/status", response_model=WashStatusResponse)
async def wash_status(config: ConfigDep) -> WashStatusResponse:
    service = WashService(config.hot.wash)
    summary = await service.summary()
    return WashStatusResponse(enabled=service.enabled, **summary.model_dump())
