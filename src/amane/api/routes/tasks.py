import json
from collections.abc import Iterator, Sequence
from typing import Annotated

import structlog
from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, TypeAdapter

from ...db import MediaFile, Repository
from ...db.models import SortOrder, Task, TaskSortField, TaskStatus, TaskType
from ...handlers import ActorScrapePayload, ScrapePayload
from ...observability import TaskLogPage, build_record_zip, read_task_log
from ...utils.model import to_resp
from ..deps import ConfigDep, RepoDep, RuntimeDep
from ..models import (
    ActorScrapeSubmission,
    ScrapeSubmission,
    TaskBatchSubmitRequest,
    TaskBatchSubmitResponse,
    TaskChildItem,
    TaskChildListResponse,
    TaskChildStatusCounts,
    TaskListItem,
    TaskListResponse,
    TaskResponse,
    TaskSubmission,
)
from ..models.tasks import TaskBatchRequest, TaskBatchResponse, TaskWorkerResponse
from ..support.task_batch import execute_task_batch
from ..support.task_resolve import resolve_submission

logger = structlog.get_logger()

router = APIRouter(prefix="/tasks", tags=["tasks"])

# 标题取库名的任务类型: payload 带 `library_id` 的库内任务.
_LIBRARY_TASK_TYPES = (
    TaskType.REFRESH,
    TaskType.ORGANIZE,
    TaskType.SCAN_INVALID,
    TaskType.DELETE,
    TaskType.WASH_SCAN,
    TaskType.WASH_APPLY,
    TaskType.SUBTITLE_AUDIT,
    TaskType.NFO_PATCH,
)
"""库内任务: 载荷带 ``library_id``, 标题取库名."""

# 标题取番号的任务类型: payload 带 `media_file_id` 的单文件任务. 否则列表里只剩一个 id,
# 看不出补的是哪一部.
_MEDIA_TASK_TYPES = (TaskType.SUBTITLE_FETCH, TaskType.SUBTITLE_GENERATE)


async def _task_titles(repo: RepoDep, tasks: Sequence[Task]) -> dict[int, str | None]:
    """scrape→番号, actor_scrape→演员名, 库内任务→库名, 单文件任务→番号. 批量查库, 避免列表 N+1."""
    actor_ids = {
        int(p["actor_id"])
        for t in tasks
        if t.type == TaskType.ACTOR_SCRAPE and isinstance((p := t.payload or {}).get("actor_id"), int)
    }
    library_ids = {
        int(p["library_id"])
        for t in tasks
        if t.type in (*_LIBRARY_TASK_TYPES, *_MEDIA_TASK_TYPES)
        and isinstance((p := t.payload or {}).get("library_id"), int)
    }
    media_ids = {
        int(p["media_file_id"])
        for t in tasks
        if t.type in _MEDIA_TASK_TYPES and isinstance((p := t.payload or {}).get("media_file_id"), int)
    }
    actor_names = await repo.get_actor_names(list(actor_ids))
    library_names = await repo.get_library_names(list(library_ids))
    media_labels = await repo.get_media_labels(list(media_ids))
    out: dict[int, str | None] = {}
    for t in tasks:
        if t.id is None:
            continue
        payload = t.payload or {}
        title: str | None = None
        if t.type == TaskType.SCRAPE:
            number = payload.get("number")
            title = str(number) if number else None
        elif t.type == TaskType.ACTOR_SCRAPE:
            actor_id = payload.get("actor_id")
            title = actor_names.get(int(actor_id)) if isinstance(actor_id, int) else None
        elif t.type in _MEDIA_TASK_TYPES:
            media_id = payload.get("media_file_id")
            if isinstance(media_id, int):
                title = media_labels.get(media_id)
            # 从字幕页按库发起时载荷只有 library_id, 没有文件名可显示
            library_id = payload.get("library_id")
            if title is None and isinstance(library_id, int):
                title = library_names.get(library_id)
            # 按番号发起时载荷里只有番号 (没有文件 ID), 直接拿它当标题
            if title is None:
                number = payload.get("number")
                if isinstance(number, str) and number.strip():
                    title = number.strip()
        elif t.type in _LIBRARY_TASK_TYPES:
            library_id = payload.get("library_id")
            title = library_names.get(int(library_id)) if isinstance(library_id, int) else None
        out[t.id] = title
    return out


def _child_status(counts: dict[TaskStatus, int] | None) -> TaskChildStatusCounts:
    counts = counts or {}
    return TaskChildStatusCounts(
        queued=counts.get(TaskStatus.QUEUED, 0),
        running=counts.get(TaskStatus.RUNNING, 0),
        done=counts.get(TaskStatus.DONE, 0),
        failed=counts.get(TaskStatus.FAILED, 0),
    )


_SUMMARY_TYPES = (TaskType.SUBTITLE_FETCH, TaskType.SUBTITLE_GENERATE, TaskType.SUBTITLE_AUDIT)


def _outcome_summary(task: Task) -> tuple[str | None, str | None]:
    """按结果字段拼一句摘要; 字幕类「一条都没成功」要看得出是没找到还是跳过.

    列表接口不返回 result (太重), 但「下载失败 / 转写失败」在库里其实是 DONE + 计数,
    只看状态会以为没记录, 所以把计数提炼成一句人话 + 一个严重度.
    """
    raw = task.result
    if raw is None:
        return None, None
    if isinstance(raw, str):  # 历史行里存的是 JSON 文本
        try:
            raw = json.loads(raw)
        except ValueError:
            return None, None
    if not isinstance(raw, dict):
        return None, None
    result: dict[str, object] = raw

    def count(key: str) -> int:
        value = result.get(key)
        return value if isinstance(value, int) else 0

    match task.type:
        case TaskType.SUBTITLE_FETCH:
            fetched, missing, failed = count("fetched"), count("missing"), count("failed")
            parts = [f"取到 {fetched}", f"缺 {missing}"]
            if failed:
                parts.append(f"失败 {failed}")
            level = "bad" if failed else ("warn" if fetched == 0 and missing else "ok")
            return " / ".join(parts), level
        case TaskType.SUBTITLE_GENERATE:
            generated, skipped, failed = count("generated"), count("skipped"), count("failed")
            parts = [f"生成 {generated}"]
            if skipped:
                parts.append(f"跳过 {skipped}")
            if failed:
                parts.append(f"失败 {failed}")
            level = "bad" if failed else ("warn" if generated == 0 and skipped else "ok")
            return " / ".join(parts), level
        case TaskType.SUBTITLE_AUDIT:
            missing = count("missing")
            return f"缺 {missing}", ("warn" if missing else "ok")
        case _:
            return None, None


async def _decorate_tasks[T: TaskListItem](repo: RepoDep, tasks: Sequence[Task], model: type[T]) -> list[T]:
    """展示标题 + 直接后继计数. 一次批量, 避免列表 N+1.

    传 `TaskListItem` 得列表用的精简形状; 传 `TaskResponse` 时结果按判别联合校验.
    """
    titles = await _task_titles(repo, tasks)
    status_map = await repo.child_status_counts([t.id for t in tasks if t.id is not None])
    items: list[T] = []
    for t in tasks:
        child_status = _child_status(status_map.get(t.id) if t.id is not None else None)
        summary = _outcome_summary(t)
        items.append(
            to_resp(model, t).model_copy(
                update={
                    "title": titles.get(t.id) if t.id is not None else None,
                    "child_count": child_status.queued + child_status.running + child_status.done + child_status.failed,
                    "child_status": child_status,
                    "summary": summary[0],
                    "summary_level": summary[1],
                }
            )
        )
    return items


async def _to_resp(repo: RepoDep, task: Task) -> TaskResponse:
    """详情: 结果按判别联合校验, 并核对成员类型与任务类型; 读不出来或不一致时置空."""
    resp = (await _decorate_tasks(repo, [task], TaskResponse))[0]
    if task.result is None:
        return resp
    # 缺 type 的行属预期状态 (每次展开都会命中), 降到 debug; 真的写坏了要靠记录目录排查.
    if resp.result is None:
        logger.debug("task result unreadable", task_id=task.id, task_type=str(task.type))
        return resp
    if resp.result.type != task.type:
        logger.debug(
            "task result type mismatch",
            task_id=task.id,
            task_type=str(task.type),
            result_type=str(resp.result.type),
        )
        return resp.model_copy(update={"result": None})
    return resp


@router.get("")
async def list_tasks(
    repo: RepoDep,
    status: Annotated[list[TaskStatus] | None, Query(description="Filter by task status (multiple)")] = None,
    type: Annotated[list[TaskType] | None, Query(description="Filter by task type (multiple)")] = None,
    root_task_id: Annotated[
        int | None, Query(description="Filter to a task chain (all tasks sharing the root)")
    ] = None,
    roots_only: Annotated[bool, Query(description="只返回链根; 想看子任务(如逐文件字幕)传 false")] = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort_by: Annotated[TaskSortField, Query(description="Sort field")] = TaskSortField.CREATED_AT,
    order: Annotated[SortOrder, Query(description="Sort order")] = SortOrder.DESC,
) -> TaskListResponse:
    """默认只返回链根 (roots_only). 子任务由 /children 按需加载;
    显式 root_task_id 时返回该链全部任务."""
    items = (
        await repo.list_tasks_by_root(root_task_id)
        if root_task_id is not None
        else await repo.list_tasks(
            statuses=status,
            task_types=type,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            order=order,
            roots_only=roots_only,
        )
    )
    total = (
        len(items)
        if root_task_id is not None
        else await repo.count_tasks(statuses=status, task_types=type, roots_only=roots_only)
    )
    return TaskListResponse(items=await _decorate_tasks(repo, items, TaskListItem), total=total)


@router.get("/{task_id}/children")
async def get_task_children(
    task_id: int,
    repo: RepoDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TaskChildListResponse:
    """直接后继 (TaskLink 出边). total 为出边总数, 不受本页截断."""
    if await repo.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    pairs = await repo.list_children(task_id, limit=limit, offset=offset)
    decorated = await _decorate_tasks(repo, [task for task, _ in pairs], TaskListItem)
    parent_counts = await repo.child_status_counts([task_id])
    total = sum(parent_counts.get(task_id, {}).values())
    return TaskChildListResponse(
        items=[
            TaskChildItem.model_validate({**resp.model_dump(), "link_key": key})
            for resp, (_, key) in zip(decorated, pairs, strict=True)
        ],
        total=total,
    )


@router.post("", status_code=202)
async def submit_task(req: Annotated[TaskSubmission, Body(...)], repo: RepoDep) -> TaskResponse:
    task_type, payload = await resolve_submission(req, repo)
    task = await repo.create_task(task_type=task_type, payload=payload)
    # mode="json": 该日志经 WS 广播时会被 json 序列化, payload 中的 set/enum 等需转为原生类型.
    logger.info("task submitted", task_id=task.id, task_type=task_type, payload=payload.model_dump(mode="json"))
    return await _to_resp(repo, task)


@router.get("/schema")
async def get_task_schema() -> dict:
    return TypeAdapter(TaskSubmission).json_schema()


@router.get("/worker")
async def get_task_worker(runtime: RuntimeDep) -> TaskWorkerResponse:
    return TaskWorkerResponse(paused=runtime.worker.is_paused)


@router.post("/worker/pause")
async def pause_task_worker(runtime: RuntimeDep) -> TaskWorkerResponse:
    runtime.worker.set_paused(True)
    return TaskWorkerResponse(paused=True)


@router.post("/worker/resume")
async def resume_task_worker(runtime: RuntimeDep) -> TaskWorkerResponse:
    runtime.worker.set_paused(False)
    return TaskWorkerResponse(paused=False)


@router.post("/batch")
async def batch_tasks(
    req: Annotated[TaskBatchRequest, Body(...)],
    repo: RepoDep,
    runtime: RuntimeDep,
    config: ConfigDep,
) -> TaskBatchResponse:
    """``task_ids`` 与 status/type 互斥."""
    result = await execute_task_batch(
        action=req.action,
        repo=repo,
        cancel_task=runtime.cancel_task,
        log_dir=config.cold.log_dir,
        task_ids=req.task_ids,
        statuses=req.status,
        task_types=req.type,
    )
    logger.info(
        "tasks batch",
        action=req.action,
        affected=result.affected,
        skipped=result.skipped,
        missing=result.missing,
        submitted=result.submitted,
    )
    return result


async def _resolve_batch_submission(
    submission: TaskSubmission,
    repo: Repository,
    files_by_id: dict[int, MediaFile],
    known_actors: set[int],
) -> tuple[TaskType, BaseModel] | None:
    """解析单条提交; 目标实体不存在返回 None (计入 missing), 参数级错误照常抛出."""
    if isinstance(submission, ScrapeSubmission):
        media = files_by_id.get(submission.media_id) if submission.media_id is not None else None
        payload = submission.build_payload(media)
        return None if payload is None else (TaskType.SCRAPE, payload)
    if isinstance(submission, ActorScrapeSubmission):
        if submission.actor_id not in known_actors:
            return None
        return TaskType.ACTOR_SCRAPE, ActorScrapePayload(actor_id=submission.actor_id, use_cache=submission.use_cache)
    return await resolve_submission(submission, repo)


def _batch_dedupe_key(entry: tuple[TaskType, BaseModel]) -> tuple[TaskType, str, int | None] | None:
    """同请求内语义重复的判别键; 其余类型各自独立, 不去重 (键为 None)."""
    task_type, payload = entry
    if task_type is TaskType.SCRAPE:
        assert isinstance(payload, ScrapePayload)
        # 同番号不同文件保留: 每个文件各有一次落库与自动整理
        return TaskType.SCRAPE, payload.number.casefold(), payload.media_file_id
    if task_type is TaskType.ACTOR_SCRAPE:
        assert isinstance(payload, ActorScrapePayload)
        return TaskType.ACTOR_SCRAPE, "", payload.actor_id
    return None


@router.post("/batch/submit", status_code=202)
async def batch_submit_tasks(req: TaskBatchSubmitRequest, repo: RepoDep) -> TaskBatchSubmitResponse:
    """批量提交任务. 逐项解析同单条提交, 同类型合并为一次入队事务.

    ``missing`` 只覆盖目标实体不存在 (404); 路径越界 / 参数互斥等仍是整批中断.
    """
    submissions = req.submissions
    media_ids = {s.media_id for s in submissions if isinstance(s, ScrapeSubmission) and s.media_id is not None}
    files_by_id = (
        {f.id: f for f in await repo.list_media_files(limit=None, ids=sorted(media_ids)) if f.id is not None}
        if media_ids
        else {}
    )
    actor_ids = {s.actor_id for s in submissions if isinstance(s, ActorScrapeSubmission)}
    # 存在性检查借 get_actor_names: 不存在的 id 不出现在结果里.
    known_actors = set(await repo.get_actor_names(sorted(actor_ids))) if actor_ids else set()

    entries: list[tuple[TaskType, BaseModel] | None] = []
    seen: set[tuple[TaskType, str, int | None]] = set()
    missing = 0
    skipped = 0
    for submission in submissions:
        try:
            entry = await _resolve_batch_submission(submission, repo, files_by_id, known_actors)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            entry = None
        if entry is None:
            missing += 1
            entries.append(None)
            continue
        key = _batch_dedupe_key(entry)
        if key is not None:
            if key in seen:
                skipped += 1
                entries.append(None)
                continue
            seen.add(key)
        entries.append(entry)

    grouped: dict[TaskType, list[BaseModel]] = {}
    for entry in entries:
        if entry is not None:
            grouped.setdefault(entry[0], []).append(entry[1])

    created: dict[TaskType, Iterator[Task]] = {
        task_type: iter(await repo.create_tasks(task_type, payloads)) for task_type, payloads in grouped.items()
    }
    task_ids: list[int] = []
    for entry in entries:
        if entry is None:
            continue
        task = next(created[entry[0]])
        if task.id is not None:
            task_ids.append(task.id)

    logger.info("tasks batch submitted", submitted=len(task_ids), missing=missing, skipped=skipped)
    return TaskBatchSubmitResponse(submitted=len(task_ids), missing=missing, skipped=skipped, task_ids=task_ids)


@router.get("/{task_id}")
async def get_task(task_id: int, repo: RepoDep) -> TaskResponse:
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return await _to_resp(repo, task)


@router.get("/{task_id}/log")
async def get_task_log(
    task_id: int,
    repo: RepoDep,
    config: ConfigDep,
    limit: Annotated[int, Query(ge=1, le=5000, description="Max entries from the end of task.log")] = 5000,
) -> TaskLogPage:
    """任务日志尾部. 记录文件不存在时返回空页."""
    if await repo.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return read_task_log(config.cold.log_dir, task_id, limit=limit)


@router.get("/{task_id}/record")
async def get_task_record(
    task_id: int,
    repo: RepoDep,
    config: ConfigDep,
):
    """导出任务记录 (zip), 一律脱敏.

    任务目录旁的 ``.secrets.hot.json`` 只在宿主机上留作调试, 不经 HTTP 下发: 它是 GET 请求,
    不进写操作留痕表, 而内容包含站点 cookie 与大模型密钥.
    """
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status not in (TaskStatus.DONE, TaskStatus.FAILED):
        raise HTTPException(status_code=409, detail="只有已结束的任务才能导出任务记录")

    try:
        data = build_record_zip(config.cold.log_dir, task_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="该任务没有任务记录") from None

    return StreamingResponse(
        iter([data]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="task-{task_id}-record.zip"'},
    )
