"""洗版的只读视图与两个入队动作.

扫候选 (``WASH_SCAN``) 与执行 (``WASH_APPLY``) 由 ``/tasks`` 提交; 这里另给还原与清理两个动作,
它们同样入队 ``WASH_APPLY``, 只把要处理的记录 id 用保留前缀写进 payload —— 任务类型是既有的
两类, 不为两个动作新增 ``TaskType`` (那是共享文件). 前缀的约定见 ``amane-merge/notes/wash.md``.

本模块不遍历文件系统 (``docs/dev/ops.md`` 的禁止事项): 候选与回收站都从索引与 ``wash_records``
读; 磁盘实况只由任务读.
"""

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query

from ...db.models import SortOrder, TaskType, WashChoiceAction, WashRecordKind, WashRecordState
from ...db.repos.ops_wash import WashRecordSortField
from ...handlers.models import WashApplyPayload
from ...library.wash import WashCandidateSortField, WashService
from ...library.wash.service import WashScope, parse_kind
from ...utils.model import to_resp
from ..deps import ConfigDep, RepoDep
from ..models import TaskResponse
from ..models.wash import (
    WashCandidateListResponse,
    WashChoiceClearResponse,
    WashChoiceItem,
    WashChoiceListResponse,
    WashChoiceRequest,
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
    return await _enqueue(repo, WashApplyPayload(restore_ids=list(req.ids)))


@router.post("/records/purge", status_code=202)
async def purge_wash_records(req: WashRecordActionRequest, repo: RepoDep) -> TaskResponse:
    """真删已经到期的回收站记录; 没到期的一律跳过, 由任务判定并计入 ``skipped``."""
    await _require_records(repo, req.ids, WashRecordState.TRASHED)
    return await _enqueue(repo, WashApplyPayload(purge_ids=list(req.ids)))


async def _require_records(repo: Repository, ids: list[int], state: WashRecordState) -> None:
    """校验 id 全部存在且处于期望状态; 有一项不合格就整体拒绝 (422), 不做部分提交."""
    records = await repo.get_wash_records(ids)
    known = {record.id for record in records if record.state == state}
    missing = sorted(set(ids) - known)
    if missing:
        raise HTTPException(status_code=422, detail=f"回收站记录不存在或状态不符: {missing}")


async def _enqueue(repo: Repository, payload: WashApplyPayload) -> TaskResponse:
    task = await repo.create_task(task_type=TaskType.WASH_APPLY, payload=payload)
    return to_resp(TaskResponse, task)


@router.get("/choices", response_model=WashChoiceListResponse)
async def wash_choices(repo: RepoDep) -> WashChoiceListResponse:
    """当前所有显式决定: 钉住哪一份、哪些组选择了都留着."""
    choices = await repo.list_wash_choices()
    items = [
        WashChoiceItem(
            group_key=choice.group_key, action=choice.action, keep_path=choice.keep_path, updated_at=choice.updated_at
        )
        for choice in sorted(choices.values(), key=lambda choice: choice.updated_at, reverse=True)
    ]
    return WashChoiceListResponse(items=items)


@router.put("/choices", response_model=WashChoiceItem)
async def wash_choice_set(req: WashChoiceRequest, repo: RepoDep) -> WashChoiceItem:
    """写入一组的决定 (覆盖旧值).

    ``keep`` 要给出组里存在的那一份的库内路径: 路径对不上时扫描会退回算法排序, 这里不拦 ——
    路径会随整理与转移变化, 用严格校验换来的只是又一次失败.
    """
    if req.action is WashChoiceAction.KEEP and not req.keep_path.strip():
        raise HTTPException(status_code=422, detail="钉住一份时要给出要保留的文件路径")
    choice = await repo.set_wash_choice(req.group_key, req.action, req.keep_path.strip())
    return WashChoiceItem(
        group_key=choice.group_key, action=choice.action, keep_path=choice.keep_path, updated_at=choice.updated_at
    )


@router.delete("/choices/{group_key}", response_model=WashChoiceClearResponse)
async def wash_choice_clear(group_key: str, repo: RepoDep) -> WashChoiceClearResponse:
    """撤销一组的决定, 回到算法选择."""
    return WashChoiceClearResponse(cleared=await repo.clear_wash_choice(group_key))
