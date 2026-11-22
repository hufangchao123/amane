"""体检: 只读探针, 在请求内同步跑完, 没有对应的后台任务.

探针只读自身状态 (配置、库、磁盘、队列) 并把慢调用丢进线程池, 因此不需要排队: 引入任务类型
会多出一份「等任务跑完」的状态, 而请求返回时结果已经是最终结果.
"""

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
