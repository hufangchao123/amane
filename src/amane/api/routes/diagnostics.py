"""体检: 只读探针, 请求内直接跑; 深度自检与模拟运行走 ``DIAGNOSE`` 任务."""

from fastapi import APIRouter

from ...diagnostics import SelfCheck
from ..deps import RuntimeDep
from ..models.diagnostics import CheckItemResponse, DiagnosticsResponse

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


@router.get("/status", response_model=DiagnosticsResponse)
async def diagnostics_status(runtime: RuntimeDep) -> DiagnosticsResponse:
    report = await SelfCheck(runtime.config.cold, runtime.config.hot, runtime.repo).run()
    return DiagnosticsResponse(
        items=[CheckItemResponse(key=item.key, ok=item.ok, detail=item.detail) for item in report.items],
        failed=len(report.failed),
    )
