"""每个任务在独立 contextvars 中执行; Recorder 安装 task.log FileHandler 与结构化产物."""

import asyncio
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel

from ..config import HotSettings
from ..db.models import TaskStatus, TaskType
from ..events import EventType
from ..handlers.models import NOTIFY_BODY_LIMIT, NotifyPayload
from ..handlers.protocol import FollowupTask, TaskResult
from ..observability import Recorder

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from ..db.models import Task, TaskType
    from ..db.repository import Repository
    from ..events import EventBus
    from ..handlers.protocol import TaskHandler

logger = structlog.get_logger()

CANCEL_ERROR = "已由用户取消"
# 停服打断与人工取消是两回事: 前者是运维动作, 事后排查要能区分开, 所以另给一句文案.
SHUTDOWN_ERROR = "服务停止, 任务被中断"
# 一天待发通知算「同一件事」: 失败通知按任务类型合并, 队列里有一条就不再排.
PENDING_NOTIFY_STATUSES = (TaskStatus.QUEUED, TaskStatus.RUNNING)

# 关闭时给活跃任务的宽限: 0 会让第一轮等待立刻超时, 在飞任务全部被取消.
_DEFAULT_SHUTDOWN_TIMEOUT = 10.0
# 关闭时等待主循环 (含尚未结算的认领) 退出的兜底阈值; 正常路径只等它自己退出.
MAIN_LOOP_STOP_TIMEOUT = 5.0
# 主循环崩溃后的退避重启: 首次等待与单次上限, 每次崩溃翻倍直到上限.
LOOP_RESTART_DELAY = 1.0
LOOP_RESTART_MAX_DELAY = 60.0


class AsyncWorker:
    """任务执行循环, 以 ``is_main`` 标志控制是否认领.

    ``retire()`` 后主循环退出, 已认领任务继续运行; 调用方负责在活跃任务清零后释放该 worker 占用的资源.
    崩溃的主循环由 ``_supervise`` 退避重启, ``retire()`` 后不再拉起.
    """

    def __init__(
        self,
        repo: Repository,
        handlers: Mapping[TaskType, TaskHandler[Any, Any]],
        *,
        concurrency: int = 3,
        poll_interval: float = 2.0,
        shutdown_timeout: float = _DEFAULT_SHUTDOWN_TIMEOUT,
        event_bus: EventBus | None = None,
        log_dir: Path | None = None,
        get_hot: Callable[[], HotSettings] | None = None,
    ):
        self._repo = repo
        self._handlers = handlers
        self._concurrency = concurrency
        self._poll_interval = poll_interval
        self._shutdown_timeout = shutdown_timeout
        # 停服时置位: 被打断的任务落库文案与人工取消区分开.
        self._shutting_down = False
        self._semaphore = asyncio.Semaphore(concurrency)
        self._is_main = True
        # retire() 唤醒轮询中的主循环; 主循环自行退出, 不取消.
        self._stop_signal = asyncio.Event()
        self._main_task: asyncio.Task[None] | None = None
        self._active_tasks: dict[int, asyncio.Task[None]] = {}
        self._done_queue: asyncio.Queue[int] = asyncio.Queue()  # 完成时 put task_id, 供外部精确同步
        self._event_bus = event_bus
        self._log_dir = log_dir
        self._get_hot = get_hot
        self._active_recorders: dict[int, Recorder] = {}  # 未 finalize 的 Recorder, shutdown 时关闭
        self._cleanup_tasks: set[asyncio.Task[bool]] = set()  # 取消兜底写入, 保持引用
        self._paused = False

    @property
    def is_main(self) -> bool:
        return self._is_main

    @property
    def active_count(self) -> int:
        return len(self._active_tasks)

    @property
    def is_paused(self) -> bool:
        """暂停时循环仍在, 不再 claim 新任务; 已认领的继续执行."""
        return self._paused

    def set_paused(self, paused: bool) -> None:
        if self._paused == paused:
            return
        self._paused = paused
        logger.info("worker paused" if paused else "worker resumed")

    def retire(self) -> None:
        """停止认领; 主循环在当前迭代结束后退出. 不可逆, 重复调用无操作."""
        if not self._is_main:
            return
        self._is_main = False
        self._stop_signal.set()

    def start(self) -> None:
        self._stop_signal.clear()
        self._main_task = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        """守护主循环: 异常退出时退避重启, retire 后与正常退出一样返回.

        主循环是唯一的认领入口, 一次未捕获异常 (典型是写锁等待超时) 就会让调度器永久停止认领;
        崩溃只在退役与关闭路径经 ``wait_stopped`` 才能观察到, 运行期没有其它读取点. 重启前等待
        ``_stop_signal``, 使 retire 不必等满退避.
        """
        delay = LOOP_RESTART_DELAY
        while self._is_main:
            try:
                await self._run_loop()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("worker main loop crashed, restarting", delay=delay, exc_info=e)
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_signal.wait(), delay)
                delay = min(delay * 2, LOOP_RESTART_MAX_DELAY)

    def cancel_main_loop(self) -> None:
        """关闭路径等待超时后调用; 取消主循环, 尚未结算的认领事务可能无法完整结束."""
        if self._main_task is not None:
            self._main_task.cancel()

    async def wait_stopped(self) -> None:
        """等主循环退出 (含认领结算).

        主循环崩溃由 ``_supervise`` 自行重启, 走到这里的异常来自 supervisor 自身; ``asyncio.wait``
        不因主循环被取消而向调用者抛出, 因此被取消与失败在此记录并继续.
        """
        main = self._main_task
        if main is None:
            return
        await asyncio.wait({main})
        if main.cancelled():
            logger.warning("worker main loop cancelled")
        elif main.exception() is not None:
            logger.error("worker main loop crashed", exc_info=main.exception())

    async def drain(self) -> None:
        """等主循环退出后再等活跃任务自然结束; 不取消、不清扫. 必须先 ``retire``."""
        await self.wait_stopped()
        if self._active_tasks:
            await asyncio.gather(*list(self._active_tasks.values()), return_exceptions=True)
        self._close_recorders()

    async def shutdown_active(self, *, graceful: bool = True) -> None:
        """处置活跃任务; 不取消主循环, 不清扫数据库.

        ``graceful`` 只在服务真的要停时打开: 那时值得给任务一段收尾时间, 被打断的任务记「服务停止」.
        配置热切换引起的 worker 换代不走宽限 —— 换代会重新入队, 等 10 秒只是把请求拖慢,
        而任务是被有意打断的, 记「已由用户取消」才与实际相符.
        """
        self._shutting_down = graceful
        active = list(self._active_tasks.values())
        if active:
            if graceful:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*active, return_exceptions=True), timeout=self._shutdown_timeout
                    )
                    active = []
                except TimeoutError:
                    logger.warning(
                        "shutdown timeout, cancelling active tasks",
                        timeout=self._shutdown_timeout,
                        active_count=len(active),
                    )
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
        self._close_recorders()

    def _close_recorders(self) -> None:
        for rec in list(self._active_recorders.values()):
            rec.close()
        self._active_recorders.clear()

    def _on_task_done(self, task_id: int, task: asyncio.Task[None]) -> None:
        """移除登记; 已登记但未首次调度即被取消的任务由兜底写入补终态."""
        self._active_tasks.pop(task_id, None)
        if task.cancelled():
            cleanup = asyncio.create_task(self._repo.fail_running_task(task_id, error=CANCEL_ERROR))
            self._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._cleanup_tasks.discard)

    async def _run_loop(self) -> None:
        logger.info("worker started", concurrency=self._concurrency, poll_interval=self._poll_interval)

        while self._is_main:
            # 未暂停且有空闲容量时认领
            if not self._paused and self._semaphore._value > 0:
                task = await self._repo.claim_next_task()
                if task is not None:
                    task_id = task.id
                    assert task_id is not None
                    claimed = asyncio.create_task(self._execute(task))
                    self._active_tasks[task_id] = claimed
                    claimed.add_done_callback(lambda done, tid=task_id: self._on_task_done(tid, done))
                    continue  # 立即检查更多任务, 不等待 poll_interval

            # 可被 retire() 立刻唤醒, 不必等满 poll_interval.
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop_signal.wait(), self._poll_interval)

        logger.info("worker stopped")

    async def cancel_task(self, task_id: int) -> bool:
        asyncio_task = self._active_tasks.get(task_id)
        if asyncio_task is None:
            return False
        asyncio_task.cancel()
        logger.info("task cancellation requested", task_id=task_id)
        return True

    async def _execute(self, task: Task) -> None:
        assert task.id is not None
        task_id = task.id
        task_type_str = str(task.type)

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(task_id=task_id, task_type=task_type_str)

        try:
            await self._execute_claimed(task)
        except asyncio.CancelledError:
            # 取消落在信号量等待 / recorder 初始化 / 完成事务等无终态窗口: 条件补写后传播.
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            with suppress(Exception):
                await asyncio.shield(self._repo.fail_running_task(task_id, error=CANCEL_ERROR))
            raise

    async def _execute_claimed(self, task: Task) -> None:
        assert task.id is not None
        task_id = task.id
        task_type_str = str(task.type)

        async with self._semaphore:
            handler = self._handlers.get(task.type)

            if handler is None:
                await self._repo.fail_task(task_id, error=f"任务类型 {task.type} 没有处理器")
                self._done_queue.put_nowait(task_id)
                return

            try:
                typed_payload = handler.parse_payload(task.payload)
            except (TypeError, KeyError, ValueError) as e:
                await self._repo.fail_task(task_id, error=f"参数无效: {e}")
                self._done_queue.put_nowait(task_id)
                return

            async def _report_progress(current_val: int, total: int, message: str = "") -> None:
                if self._event_bus:
                    await self._event_bus.emit(
                        EventType.TASK_PROGRESS,
                        {"task_id": task_id, "current": current_val, "total": total, "message": message},
                    )

            handler.set_progress_callback(_report_progress)

            rec: Recorder | None = None
            if self._log_dir is not None:
                try:
                    started = await self._repo.get_task(task_id) or task
                    hot = self._get_hot() if self._get_hot is not None else HotSettings()
                    rec = Recorder.begin(self._log_dir, started, hot)
                    self._active_recorders[task_id] = rec
                except Exception:
                    logger.exception("task recorder begin failed", task_id=task_id)
                    rec = None

            if self._event_bus:
                await self._event_bus.emit(EventType.TASK_STARTED, {"task_id": task_id, "type": task_type_str})

            start_time = time.monotonic()
            # mode="json": payload 经 WS 广播时会被 json 序列化, set/enum/Path 等需先转为原生类型,
            # 否则 EventBus.broadcast 序列化失败 (见 events.py 对该类异常的处理).
            payload_dump = (
                typed_payload.model_dump(mode="json") if isinstance(typed_payload, BaseModel) else typed_payload
            )
            logger.info("task started", payload=payload_dump)

            def _debug_capture() -> bool:
                if self._get_hot is None:
                    return False
                return self._get_hot().logging.debug_capture

            def _failure_payload() -> dict[str, object] | None:
                """崩溃 / 取消时让 handler 补一份失败载荷; 钩子出错不影响失败处理."""
                try:
                    failure = handler.failure_result(typed_payload)
                except Exception:
                    logger.exception("failure result build failed", task_id=task_id)
                    return None
                return TaskResult(success=False, result=failure).as_dict()

            async def _notify_failure(task_type_str: str, error: str) -> None:
                """任务失败时推一条通知.

                「通知」自身失败不再推, 否则配置错一个地址就会自激; 未启用、或队列写失败都只记日志 ——
                通知是旁路, 不能反过来把任务判失败.
                """
                hot = self._get_hot() if self._get_hot is not None else None
                if hot is None or not hot.notify.enabled or task_type_str == TaskType.NOTIFY.value:
                    return
                try:
                    # 队列里已有一条未发出的失败通知就不再排: 站点挂掉时是成批失败, 逐条推会把真正的
                    # 异常刷没; 一条待发通知足以说明有人失败, 具体清单仍看任务列表.
                    # 只算 kind == "message" 的那些: 例行周报 (kind="digest") 排队时不能把失败通知吞掉.
                    if await self._repo.count_pending_notify_messages() > 0:
                        logger.info("failure notify coalesced", task_type=task_type_str)
                        return
                    await self._repo.create_task(
                        TaskType.NOTIFY,
                        NotifyPayload(
                            kind="message", title=f"任务失败: {task_type_str}", body=error[:NOTIFY_BODY_LIMIT]
                        ),
                    )
                except Exception:
                    logger.warning("failure notify failed", task_type=task_type_str)

            async def _enqueue(pending: list[FollowupTask]) -> None:
                """批量入队; 与排队中任务完全同型的后继跳过, 整批失败只记日志, 不影响任务结算.

                批量入库时每个文件都扇出一次整库刷新, 逐条建会把队列塞满同款扫描; 判重放在写锁内
                一次完成, 两个并发父任务不会都插进去.
                """
                if not pending:
                    return
                rows = [(f.task_type, f.payload, f.priority) for f in pending]
                try:
                    await self._repo.create_followups_unless_queued(rows)
                except Exception:
                    logger.exception("followup enqueue failed", task_id=task_id, keys=[f.key for f in pending])

            async def _failure_followups(result: TaskResult | None) -> None:
                """失败后继: 钩子出错不影响失败处理, 后继逐个入队且不与失败任务结对.

                失败任务的链不写 (``handlers/protocol.py``), 但 handler 挂在失败结果上的后继照旧入队 ——
                部分成功时盘上已经变了, 媒体服务器仍需重扫.
                """
                try:
                    pending = list(await handler.failure_followups(typed_payload))
                except Exception:
                    logger.exception("failure followups build failed", task_id=task_id)
                    pending = []
                if result is not None:
                    pending.extend(result.followups or [])
                await _enqueue(pending)

            async def _finalize_recorder(*, success: bool, error: str | None) -> None:
                if rec is None:
                    return
                try:
                    final = await self._repo.get_task(task_id) or task
                    rec.finalize(final, success=success, error=error, debug_capture=_debug_capture())
                except Exception:
                    logger.exception("task recorder finalize failed", task_id=task_id)
                    rec.close()

            try:
                try:
                    result = await handler.handle(typed_payload)
                except asyncio.CancelledError:
                    duration_s = round(time.monotonic() - start_time, 2)
                    reason = SHUTDOWN_ERROR if self._shutting_down else CANCEL_ERROR
                    logger.info("task cancelled", duration_s=duration_s, reason=reason)
                    await self._repo.fail_running_task(task_id, error=reason, result=_failure_payload())
                    await _finalize_recorder(success=False, error=reason)
                    if self._event_bus:
                        await self._event_bus.emit(
                            EventType.TASK_FAILED,
                            {"task_id": task_id, "type": task_type_str, "error": reason},
                        )
                    self._done_queue.put_nowait(task_id)
                    return
                except Exception as e:
                    duration_s = round(time.monotonic() - start_time, 2)
                    logger.exception("task crashed", error=str(e), duration_s=duration_s)
                    await self._repo.fail_task(task_id, error=str(e), result=_failure_payload())
                    await _failure_followups(None)
                    await _notify_failure(task_type_str, str(e))
                    await _finalize_recorder(success=False, error=str(e))
                    if self._event_bus:
                        await self._event_bus.emit(
                            EventType.TASK_FAILED,
                            {"task_id": task_id, "type": task_type_str, "error": str(e)},
                        )
                    self._done_queue.put_nowait(task_id)
                    return

                if result.success:
                    duration_s = round(time.monotonic() - start_time, 2)
                    followups = [(f.key, f.task_type, f.payload, f.priority) for f in (result.followups or [])]
                    try:
                        await self._repo.complete_task_with_followups(task_id, result.as_dict(), followups)
                    except Exception as e:
                        logger.exception("task complete with followups failed", error=str(e), duration_s=duration_s)
                        await self._repo.fail_task(task_id, error=str(e))
                        # 结算写库失败是最该被推开的事故: 整批任务其实都没落地, 只在列表里看不出来.
                        await _notify_failure(task_type_str, str(e))
                        await _finalize_recorder(success=False, error=str(e))
                        if self._event_bus:
                            await self._event_bus.emit(
                                EventType.TASK_FAILED,
                                {"task_id": task_id, "type": task_type_str, "error": str(e)},
                            )
                        self._done_queue.put_nowait(task_id)
                        return
                    logger.info("task completed", duration_s=duration_s, followups=[key for key, _, _, _ in followups])
                    await _finalize_recorder(success=True, error=None)
                    if self._event_bus:
                        await self._event_bus.emit(
                            EventType.TASK_COMPLETED,
                            {"task_id": task_id, "type": task_type_str},
                        )
                else:
                    duration_s = round(time.monotonic() - start_time, 2)
                    err = result.error or "Unknown error"
                    await self._repo.fail_task(task_id, error=err, result=result.as_dict())
                    await _failure_followups(result)
                    await _notify_failure(task_type_str, err)
                    logger.warning("task failed", error=result.error, duration_s=duration_s)
                    await _finalize_recorder(success=False, error=err)
                    if self._event_bus:
                        await self._event_bus.emit(
                            EventType.TASK_FAILED,
                            {"task_id": task_id, "type": task_type_str, "error": err},
                        )

                self._done_queue.put_nowait(task_id)
            finally:
                if rec is not None:
                    self._active_recorders.pop(task_id, None)
                    rec.close()
