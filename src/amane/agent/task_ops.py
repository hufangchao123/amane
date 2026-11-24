"""不代为执行; 只入队 / 取消 / 重试."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Capability

from ..api.support.task_resolve import resolve_submission
from ..db.models import TaskStatus, TaskType
from ..scheduler.worker import CANCEL_ERROR
from .payload_schema import TASK_SUBMISSION, TaskSubmissionType
from .tools import TOOL_OK, AgentDeps, require_approval, wash_cascade_armed

#: 不可逆的任务: 洗版带 ``purge_ids`` 时是真删, 空载荷则按当前策略重算全部候选.
_DESTRUCTIVE_TASK_TYPES = frozenset({TaskType.WASH_APPLY})

#: 可以重跑的任务: 只读 (盘点/校验/通知刷库) 或可重入 (重抓元数据而不碰文件).
#: 重跑等于绕过提交面的审批再触发一次, 因此搬移类 (organize / scrape_transfer)、
#: 清理类 (cleanup) 与销毁类 (media_purge / delete / wash_apply) 一律不在白名单里:
#: 它们改的是磁盘上的文件, 重跑该由人在 UI 里点. 提交面之外的类型 (semantic_index /
#: actor_push / subtitle_generate 等) 也进不来, 白名单与 ``TASK_SUBMISSION`` 成员取交集.
_RETRYABLE_TASK_TYPES = frozenset(
    {
        TaskType.REFRESH,
        TaskType.RESCRAPE,
        TaskType.SCRAPE,
        TaskType.ACTOR_SCRAPE,
        TaskType.SCAN_INVALID,
        TaskType.SUBTITLE_AUDIT,
        TaskType.AVATAR_CHECK,
        TaskType.MEDIA_REFRESH,
        TaskType.WASH_SCAN,
    }
)


def _approval_detail(action: str, task_type: TaskType) -> str:
    """审批文案: 洗版扫描的代价不在扫描本身, 而在打开自动执行后那一串后继删除."""
    if task_type is TaskType.WASH_SCAN:
        return f"{action}: wash_scan（洗版自动执行已打开, 扫描后会级联删除源文件）"
    return f"{action}: {task_type.value}"


def _needs_approval(task_type: TaskType, deps: AgentDeps) -> bool:
    """任务是否需要过人这一关: 不可逆的类型, 以及会级联成不可逆的洗版扫描."""
    if task_type in _DESTRUCTIVE_TASK_TYPES:
        return True
    return task_type is TaskType.WASH_SCAN and wash_cascade_armed(deps)


def build_task_ops_capability() -> Capability[AgentDeps]:
    cap: Capability[AgentDeps] = Capability(
        id="task-ops",
        instructions=(
            "Prefer the domain enqueue tools (enqueue_scrape / enqueue_actor_scrape / "
            "enqueue_library_refresh) when one covers the request; submit_task is the unified "
            "surface for the rest and does not execute work inline."
        ),
    )

    @cap.tool
    async def get_task_submission_schema(
        ctx: RunContext[AgentDeps], submission_type: TaskSubmissionType
    ) -> dict[str, Any]:
        """Return the accepted fields for one task submission type."""
        return TASK_SUBMISSION.schema(submission_type)

    @cap.tool
    async def submit_task(ctx: RunContext[AgentDeps], submission: dict[str, Any]) -> dict[str, Any]:
        """Enqueue a task; the body shape comes from get_task_submission_schema."""
        try:
            req = TASK_SUBMISSION.validate(submission)
            task_type, payload = await resolve_submission(req, ctx.deps.repo)
        except ValidationError as exc:
            return TASK_SUBMISSION.error(submission, exc)
        except HTTPException as exc:
            return {"error": str(exc.detail)}
        except ValueError as exc:
            return {"error": str(exc)}
        if _needs_approval(task_type, ctx.deps):
            # 与 UI 一致: 洗版要人点头, 助理不能自己决定删哪些文件.
            require_approval(
                ctx,
                sql=_approval_detail("提交任务", task_type),
                tool="submit_task",
                reason="destructive",
                extra={"type": task_type.value},
            )
        task = await ctx.deps.repo.create_task(task_type=task_type, payload=payload)
        assert task.id is not None
        return {"task_id": task.id}

    @cap.tool
    async def cancel_task(ctx: RunContext[AgentDeps], task_id: int) -> str | dict[str, Any]:
        """Cancel a queued or running task; report when the state moved first."""
        task = await ctx.deps.repo.get_task(task_id)
        if task is None:
            return {"error": f"task {task_id} 不存在"}
        if task.status == TaskStatus.QUEUED:
            if await ctx.deps.repo.fail_queued_tasks(error=CANCEL_ERROR, task_ids=[task_id]):
                return TOOL_OK
            # 读取后被认领: 重读后按运行中取消, 不能把排队标记失败当作已停止.
            latest = await ctx.deps.repo.get_task(task_id)
            if latest is None:
                return {"error": f"task {task_id} 不存在"}
            if latest.status != TaskStatus.RUNNING:
                return {"error": f"任务状态为 '{latest.status}', 未取消"}
            task = latest
        if task.status == TaskStatus.RUNNING:
            cancel_fn = ctx.deps.bridge.cancel_running_task
            if cancel_fn is not None and await cancel_fn(task_id):
                return TOOL_OK
            if await ctx.deps.repo.fail_running_task(task_id, error=CANCEL_ERROR):
                return TOOL_OK
            return {"error": "任务已结束, 未取消"}
        return {"error": f"无法取消状态为 '{task.status}' 的任务"}

    @cap.tool
    async def retry_task(ctx: RunContext[AgentDeps], task_id: int) -> dict[str, Any]:
        """Retry a failed task by enqueueing a new one with the same type/payload.

        Only read-only or re-runnable types qualify: retrying a file-moving or destructive
        task bypasses the approval that guards the submit path.
        """
        task = await ctx.deps.repo.get_task(task_id)
        if task is None:
            return {"error": f"task {task_id} 不存在"}
        if task.status != TaskStatus.FAILED:
            return {"error": "仅失败任务可重试"}
        if task.type not in _RETRYABLE_TASK_TYPES:
            allowed = ", ".join(sorted(member.value for member in _RETRYABLE_TASK_TYPES))
            return {
                "error": f"任务类型 '{task.type.value}' 不可重跑: 重跑只对只读或可重入的类型开放, "
                f"会改动文件的任务请在页面上重跑. 可重跑的类型: {allowed}"
            }
        if _needs_approval(task.type, ctx.deps):
            require_approval(
                ctx,
                sql=_approval_detail(f"重跑任务 id={task_id}", task.type),
                tool="retry_task",
                reason="destructive",
                extra={"task_id": task_id, "type": task.type.value},
            )
        new_task = await ctx.deps.repo.create_task(task_type=task.type, payload=task.payload, priority=task.priority)
        assert new_task.id is not None
        return {"task_id": new_task.id}

    return cap
