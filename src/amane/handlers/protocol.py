from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from ..db.models import TaskType

type ProgressCallback = Callable[[int, int, str], Coroutine[Any, Any, None]]

_progress_callback: ContextVar[ProgressCallback | None] = ContextVar("amane_progress_callback", default=None)
"""当前任务的进度回调.

按 asyncio 上下文存放而不是挂在 handler 实例上: 同类任务的 handler 只有一个实例, 实例属性会被后
开始的任务覆盖, 进度于是上报到别的 ``task_id``. worker 在任务自己的 Task 里绑定, 上下文随任务结束
一起丢弃, 无需显式清除.
"""


class FollowupTask(BaseModel):
    """后继任务只经 TaskResult.followups 进入完成事务; handler 不能直接写队列."""

    key: str
    """父节点内后继语义键 (扇出须含实体 id, 如 scrape:{media_file_id}); 同父同名只留一条."""
    task_type: TaskType
    payload: dict
    """已完成绑定的类型化 payload; handler 自己组装, 不能透传父任务字段."""
    priority: int = 0


@dataclass
class TaskResult[R: BaseModel | dict]:
    success: bool
    result: R | None = None
    error: str | None = None
    followups: list[FollowupTask] = field(default_factory=list)

    def as_dict(self) -> dict | None:
        """结果载荷的唯一落库形状: 以 JSON 模式 dump, 由 `tasks.result` 列的 `json.dumps` 直接写出.

        传 dict 时不做校验, 载荷须自带 JSON 原生值 (与 payload 侧的 `_payload_dict` 对称).
        """
        if isinstance(self.result, dict):
            return self.result
        if isinstance(self.result, BaseModel):
            return self.result.model_dump(mode="json")
        return None


class TaskHandler[P: BaseModel | dict = dict, R: BaseModel | dict = dict](ABC):
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    def __init__(self, payload_t: type[P], result_t: type[R]) -> None:
        self.payload_type = payload_t
        self.result_type = result_t

    @abstractmethod
    async def handle(self, payload: P) -> TaskResult[R]: ...

    def failure_result(self, payload: P) -> R | None:
        """异常 / 取消时补交的失败载荷: 任务已攒下的事实不该随异常丢掉.

        默认没有; 有站点事实的 handler 覆写它, 由 worker 在崩溃与取消分支落库.
        """
        return None

    async def failure_followups(self, payload: P) -> list[FollowupTask]:
        """任务判失败后仍要排的后继; 默认没有.

        由 worker 在崩溃与失败两个分支调用, 后继以独立任务入队 (任务链只在成功分支写),
        因此它们不出现在失败任务的子任务里. 钩子抛异常只记日志, 不影响失败处理.
        """
        return []

    def parse_payload(self, raw: dict) -> P:
        """已是目标类型时原样返回, 不重新校验."""
        if isinstance(raw, self.payload_type):
            return raw
        if issubclass(self.payload_type, BaseModel):
            return self.payload_type.model_validate(raw)  # type: ignore
        raise TypeError(f"Unsupported payload type: {self.payload_type}")

    # --- Progress reporting ---

    def set_progress_callback(self, callback: ProgressCallback | None) -> None:
        """把回调绑定到调用方所在的 asyncio 上下文.

        worker 在认领到的任务里调用本方法, 因此每个任务的回调互相隔离. handler 内部经
        ``asyncio.gather`` / ``asyncio.to_thread`` 派生的子任务在创建时复制上下文, 同样能上报到本任务.
        """
        _progress_callback.set(callback)

    async def report_progress(self, current: int, total: int, message: str = "") -> None:
        """无回调时静默忽略."""
        callback = _progress_callback.get()
        if callback is not None:
            await callback(current, total, message)
