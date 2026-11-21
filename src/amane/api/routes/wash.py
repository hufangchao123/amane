"""洗版的只读视图与两个入队动作.

扫候选 (``WASH_SCAN``) 与执行 (``WASH_APPLY``) 由 ``/tasks`` 提交; 这里另给还原与清理两个动作,
它们同样入队 ``WASH_APPLY``, 只把要处理的记录 id 用保留前缀写进 payload —— 任务类型是既有的
两类, 不为两个动作新增 ``TaskType`` (那是共享文件). 前缀的约定见 ``amane-merge/notes/wash.md``.

本模块不遍历文件系统 (``docs/dev/ops.md`` 的禁止事项): 候选与回收站都从索引与 ``wash_records``
读; 磁盘实况只由任务读.
"""

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query

from ...db.models import SortOrder, TaskType, WashRecordKind, WashRecordState
from ...db.repos.ops_wash import WashRecordSortField
from ...handlers.models import WashApplyPayload
from ...handlers.wash import PURGE_PREFIX, RESTORE_PREFIX
from ...library.wash import WashCandidateSortField, WashService
from ...library.wash.service import WashScope, parse_kind
from ...utils.model import to_resp
from ..deps import ConfigDep, RepoDep
from ..models import TaskResponse
from ..models.wash import (
    WashCandidateListResponse,
    WashRecordActionRequest,
    WashRecordItem,
    WashRecordListResponse,
    WashStatusResponse,
)

if TYPE_CHECKING:
    from ...db.repository import Repository

router = APIRouter(prefix="/wash", tags=["wash"])


@router.get("/status", response_model=WashStatusResponse)
async def wash_status(config: ConfigDep, repo: RepoDep) -> WashStatusResponse:
    """状态卡: 只读, 既不取库锁也不扫盘, 因此可以随手刷新."""
    service = WashService(config.hot.wash, repo)
    summary = await service.summary()
    return WashStatusResponse(enabled=service.enabled, **summary.model_dump())


@router.get("/candidates", response_model=WashCandidateListResponse)
async def wash_candidates(
    config: ConfigDep,
    repo: RepoDep,
    kind: Annotated[WashScope, Query(description="Which side to inspect")] = "all",
    library_id: Annotated[int | None, Query(description="Filter by owning library")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort_by: Annotated[WashCandidateSortField, Query(description="Sort field")] = WashCandidateSortField.BYTES_FREED,
    order: Annotated[SortOrder, Query(description="Sort order")] = SortOrder.DESC,
) -> WashCandidateListResponse:
    """候选分组: 与执行任务同一套算法, 列表里看到的就是任务会搬走的东西.

    停用时返回空页而不是报错: 面板照旧能打开, 只是没有候选.
    """
    service = WashService(config.hot.wash, repo)
    if not service.enabled:
        return WashCandidateListResponse(items=[], total=0)
    items, total = await service.candidates(
        kind=parse_kind(kind), library_id=library_id, offset=offset, limit=limit, sort_by=sort_by, order=order
    )
    return WashCandidateListResponse(items=items, total=total)


@router.get("/records", response_model=WashRecordListResponse)
async def wash_records(
    repo: RepoDep,
    state: Annotated[WashRecordState | None, Query(description="Filter by record state")] = None,
    kind: Annotated[WashRecordKind | None, Query(description="Filter by record kind")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort_by: Annotated[WashRecordSortField, Query(description="Sort field")] = WashRecordSortField.CREATED_AT,
    order: Annotated[SortOrder, Query(description="Sort order")] = SortOrder.DESC,
) -> WashRecordListResponse:
    """回收站的一页; 只读数据库, 不看磁盘 —— 文件是否还在, 由清理任务负责核对."""
    records, total = await repo.list_wash_records(
        state=state, kind=kind, offset=offset, limit=limit, sort_by=sort_by, order=order
    )
    return WashRecordListResponse(items=[WashRecordItem.of(record) for record in records], total=total)


@router.post("/records/restore", status_code=202)
async def restore_wash_records(req: WashRecordActionRequest, repo: RepoDep) -> TaskResponse:
    """把选中的回收站记录搬回原位; 入队 ``WASH_APPLY``, 由任务持库锁后执行."""
    await _require_records(repo, req.ids, WashRecordState.TRASHED)
    return await _enqueue(repo, [f"{RESTORE_PREFIX}{record_id}" for record_id in req.ids])


@router.post("/records/purge", status_code=202)
async def purge_wash_records(req: WashRecordActionRequest, repo: RepoDep) -> TaskResponse:
    """真删已经到期的回收站记录; 没到期的一律跳过, 由任务判定并计入 ``skipped``."""
    await _require_records(repo, req.ids, WashRecordState.TRASHED)
    return await _enqueue(repo, [f"{PURGE_PREFIX}{record_id}" for record_id in req.ids])


async def _require_records(repo: Repository, ids: list[int], state: WashRecordState) -> None:
    """校验 id 全部存在且处于期望状态; 有一项不合格就整体拒绝 (422), 不做部分提交."""
    records = await repo.get_wash_records(ids)
    known = {record.id for record in records if record.state == state}
    missing = sorted(set(ids) - known)
    if missing:
        raise HTTPException(status_code=422, detail=f"回收站记录不存在或状态不符: {missing}")


async def _enqueue(repo: Repository, keys: list[str]) -> TaskResponse:
    task = await repo.create_task(task_type=TaskType.WASH_APPLY, payload=WashApplyPayload(group_keys=keys))
    return to_resp(TaskResponse, task)
