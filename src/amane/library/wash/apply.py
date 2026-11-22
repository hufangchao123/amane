"""洗版的执行编排: 洗掉落选者、还原、到期清理.

为什么三步放在库里而不是 handler 里: 三者的顺序都是「确认库 → 取锁 → 动盘 → 推进记录」,
差别只在动盘那一步. 放在一处才能保证「库内路径只在 ``LibraryTaskLocks`` 下被改」这一条不会有
漏洞. handler 只负责解码 payload、提供取锁器、把计数填进任务结果.

为什么取锁器是回调而不是 ``LibraryTaskLocks`` 本身: 那个类在 ``handlers`` 里, 反过来 import
会让 ``handlers`` ↔ ``library`` 循环. 这里只要 ``async def get(library_id) -> asyncio.Lock``,
``LibraryTaskLocks.get`` 正好是它.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ...db.models import WashRecordState
from ...utils.threads import path_is_dir
from ..delete import prune_empty_dirs
from . import trash
from .model import library_id_of

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from ...db.models import WashRecord
    from ...db.repository import Repository
    from .model import WashGroup

logger = structlog.get_logger()

#: 取锁器: 每个库一把锁, 与整理 / 删除共用.
LockGetter = Callable[[int], Awaitable[asyncio.Lock]]

#: 进度回调; 形状与 ``handlers.protocol.ProgressCallback`` 一致, 但那边在 ``handlers`` 里,
#: 由 handler 直接把自己传进来.
ProgressFn = Callable[[int, int, str], Awaitable[None]]

#: 错误信息带上限: 任务结果会整条存进数据库, 上千条路径的报错没有阅读价值.
_MAX_ERRORS = 10


@dataclass
class ApplyReport:
    """一次执行的计数. ``done`` 的含义随动作变化: 洗掉 / 还原 / 真删的条数."""

    done: int = 0
    bytes: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def absorb(self, other: ApplyReport) -> None:
        self.done += other.done
        self.bytes += other.bytes
        self.skipped += other.skipped
        self.failed += other.failed
        self.errors.extend(other.errors[:_MAX_ERRORS])

    def fail(self, message: str) -> None:
        self.failed += 1
        if len(self.errors) < _MAX_ERRORS:
            self.errors.append(message)


async def wash_groups(
    repo: Repository,
    groups: Sequence[WashGroup],
    *,
    keep_days: int,
    lock_for: LockGetter,
    progress: ProgressFn | None = None,
) -> ApplyReport:
    """把每组落选的文件搬进回收站并登记 ``wash_records``.

    三层循环一层都不 ``break``: 一个组失败只记账, 后面的组照常处理; 一个库取不到根也只影响
    它自己的组. 洗版本质上是批处理, 中途退出会留下一半做过一半没做的状态, 比全做更糟.

    幂等靠两处: 源文件已经在回收站里的组不会出现在 ``groups`` 里 (``collect`` 跳过), 而落点已
    被占用时 ``move_into_trash`` 返回 ``occupied`` —— 两次执行不会把同一份文件搬两遍.
    """
    report = ApplyReport()
    if not groups:
        return report
    known = await repo.trashed_wash_paths()
    expires_at = datetime.now(UTC) + timedelta(days=keep_days)

    total = sum(len(group.drops) for group in groups)
    done = 0
    await _report(progress, 0, total, "wash")
    by_library: dict[int, list[WashGroup]] = {}
    for group in groups:
        by_library.setdefault(group.library_id, []).append(group)

    for library_id, library_groups in sorted(by_library.items()):
        library = await repo.get_library(library_id)
        if library is None:
            report.failed += sum(len(group.drops) for group in library_groups)
            report.errors.append(f"媒体库 {library_id} 不存在, 已跳过 {len(library_groups)} 组")
            continue
        root = Path(library.path)
        if not await path_is_dir(root):
            report.failed += sum(len(group.drops) for group in library_groups)
            report.errors.append(f"库目录不可用: {root}")
            continue
        async with await lock_for(library_id):
            emptied: set[Path] = set()
            for group in library_groups:
                for drop in group.drops:
                    await _trash_one(
                        repo, drop.path, group=group, root=root, expires_at=expires_at, known=known, report=report
                    )
                    emptied.add(Path(drop.path).parent)
                    done += 1
                    await _report(progress, done, total, Path(drop.path).name)
            await _prune(root, emptied)
    await _report(progress, total, total, "done")
    return report


async def _trash_one(
    repo: Repository,
    source: str,
    *,
    group: WashGroup,
    root: Path,
    expires_at: datetime,
    known: set[str],
    report: ApplyReport,
) -> None:
    """搬一份落选文件; 任何结果都只影响这一份, 由调用方继续下一份."""
    outcome = await trash.move_into_trash(source, library_root=root, kind=group.kind)
    if outcome.status == "missing":
        # 索引比磁盘旧: 文件已经被人删了, 不是错误, 但也没腾出空间.
        report.skipped += 1
        return
    if outcome.status == "occupied":
        report.skipped += 1
        return
    if outcome.status == "failed" or outcome.target is None:
        report.fail(f"{source}: {outcome.error or '搬移失败'}")
        return
    report.done += 1
    report.bytes += outcome.size
    removed_path = str(outcome.target)
    if removed_path in known:
        # 上一轮已经登记过同一个落点 (文件后来被人放回原位又洗了一次): 只搬不重复记.
        logger.warning("wash record already exists", removed_path=removed_path)
        return
    known.add(removed_path)
    await repo.add_wash_record(
        kind=group.kind,
        group_key=group.group_key,
        kept_path=group.keep.path,
        removed_path=removed_path,
        size_bytes=outcome.size,
        expires_at=expires_at,
    )


async def restore_records(
    repo: Repository,
    records: Sequence[WashRecord],
    *,
    lock_for: LockGetter,
    progress: ProgressFn | None = None,
) -> ApplyReport:
    """把回收站里的记录搬回原位并推进到 ``restored``.

    还原要往库里写文件, 因此同样持锁. 原位已经被占用时只跳过: 覆盖会丢别人的数据, 交给人工判断.
    """
    report = ApplyReport()
    total = len(records)
    done = 0
    await _report(progress, 0, total, "restore")
    by_library: dict[int, list[WashRecord]] = {}
    for record in records:
        library_id = library_id_of(record.group_key)
        if library_id is None:
            report.fail(f"记录 {record.id} 的分组键无法解析出媒体库: {record.group_key}")
            continue
        by_library.setdefault(library_id, []).append(record)

    for library_id, library_records in sorted(by_library.items()):
        library = await repo.get_library(library_id)
        if library is None:
            report.failed += len(library_records)
            report.errors.append(f"媒体库 {library_id} 不存在, 无法还原 {len(library_records)} 条")
            continue
        async with await lock_for(library_id):
            for record in library_records:
                if record.state is not WashRecordState.TRASHED:
                    report.skipped += 1
                    continue
                outcome = await trash.restore_from_trash(
                    record.removed_path, library_root=library.path, kind=record.kind
                )
                if outcome.status == "occupied":
                    report.skipped += 1
                    continue
                if outcome.status == "missing":
                    report.fail(f"回收站里已找不到: {record.removed_path}")
                    continue
                if outcome.status == "failed":
                    report.fail(f"{record.removed_path}: {outcome.error or '还原失败'}")
                    continue
                assert record.id is not None
                await repo.set_wash_record_state([record.id], WashRecordState.RESTORED)
                report.done += 1
                report.bytes += outcome.size
                done += 1
                await _report(progress, done, total, Path(record.removed_path).name)
    return report


async def purge_records(
    repo: Repository,
    records: Sequence[WashRecord],
    *,
    now: datetime | None = None,
    progress: ProgressFn | None = None,
) -> ApplyReport:
    """真删已到期的回收站文件, 记录推进到 ``purged``.

    这是洗版里唯一允许 ``unlink`` 的路径, 且只在 ``expires_at`` 已过时走. 不取库锁: 回收站在库根
    之外, 动它不改变任何库内路径.

    文件已经不在回收站里的记录同样置 ``purged``: 它已经无法还原, 留在 ``trashed`` 里会让状态卡
    永远显示一份不存在的可还原体积.

    删失败 (权限、占用) 的记录**留在** ``trashed``: 文件还在回收站里, 推进 ``purged`` 会同时抹掉
    还原入口与下一轮重试的机会 —— 与面板一审 P0-9 的「假成功」同一类错误. 失败原因回注报告.
    """
    report = ApplyReport()
    moment = now or datetime.now(UTC)
    expired: list[WashRecord] = []
    for record in records:
        if record.state is not WashRecordState.TRASHED:
            report.skipped += 1
            continue
        if not _is_expired(record.expires_at, moment):
            report.skipped += 1
            continue
        expired.append(record)
    if not expired:
        return report

    await _report(progress, 0, len(expired), "purge")
    outcome = await trash.purge_trash_paths([record.removed_path for record in expired])
    report.done = outcome.purged
    report.bytes = outcome.freed_bytes
    report.failed = outcome.failed
    report.skipped += outcome.missing
    report.errors.extend(outcome.errors[:_MAX_ERRORS])
    cleared = [
        record.id
        for record, item in zip(expired, outcome.outcomes, strict=True)
        if item.status != "failed" and record.id is not None
    ]
    if cleared:
        await repo.set_wash_record_state(cleared, WashRecordState.PURGED)
    await _report(progress, len(expired), len(expired), "done")
    return report


def _is_expired(expires_at: datetime | None, now: datetime) -> bool:
    """到期判定. 没有过期时间 (历史行) 的记录不动: 宁可留着, 也不猜它该不该删.

    SQLite 读回来的时间是不带时区的, 按 UTC 解释 —— 与 ``db.models._utcnow`` 写入时一致.
    """
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now


async def _report(progress: ProgressFn | None, current: int, total: int, message: str) -> None:
    """进度上报; 没有回调时静默跳过."""
    if progress is not None:
        await progress(current, total, message)


async def _prune(root: Path, directories: Collection[Path]) -> None:
    """搬空之后回收库内空目录; 失败只记日志, 不影响已经成功的搬移.

    ``prune_empty_dirs`` 自己就是 ``@in_thread`` 的, 这里直接 ``await``: 再套一层
    ``asyncio.to_thread`` 只会拿到一个没人等待的协程.
    """
    if not directories:
        return
    try:
        await prune_empty_dirs(directories, library_root=root)
    except OSError as exc:
        logger.warning("wash prune failed", library_root=str(root), error=str(exc))


__all__ = ["ApplyReport", "LockGetter", "ProgressFn", "purge_records", "restore_records", "wash_groups"]
