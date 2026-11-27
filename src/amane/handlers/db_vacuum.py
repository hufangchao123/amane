"""数据库文件整理: 用 ``VACUUM`` 把库重写成连续文件.

SQLite 按 4 KB 页往表尾随机写, btrfs 又是写时复制, 所以 ``amane.db`` 运行一段时间后会碎成大量小 extent;
机械盘上冷读只有几 MB/s, 表现为「重启后第一次打开页面卡一两秒」. 空闲页为 0 也照样需要整理,
目的是消除碎片而不是腾空间.

``VACUUM`` 需要短暂独占写锁, 因此由例行任务在低峰期触发; 有其它写事务时按 ``busy_timeout`` 等待后失败,
失败原因写进任务错误里, 下个周期再跑.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

from .models import DbVacuumPayload, DbVacuumResult
from .protocol import TaskHandler, TaskResult

#: 等写锁的上限; 超时说明有别的任务在长写, 让例行下个周期再来.
BUSY_TIMEOUT_MS = 30_000


class DbVacuumHandler(TaskHandler[DbVacuumPayload, DbVacuumResult]):
    """整理主库文件; 只动 ``amane.db`` 本身, 不碰迁移备份与向量库."""

    def __init__(self, db_path: Path) -> None:
        super().__init__(payload_t=DbVacuumPayload, result_t=DbVacuumResult)
        self._db_path = db_path

    async def handle(self, payload: DbVacuumPayload) -> TaskResult[DbVacuumResult]:
        if not await asyncio.to_thread(self._db_path.is_file):
            return TaskResult(success=False, error=f"数据库文件不存在: {self._db_path}")
        try:
            result = await asyncio.to_thread(vacuum_database, self._db_path)
        except (sqlite3.Error, OSError) as error:
            return TaskResult(success=False, error=f"数据库整理失败: {error}")
        return TaskResult(success=True, result=result)


def vacuum_database(path: Path) -> DbVacuumResult:
    """同步执行一整轮整理; 单独拆出来便于测试与在容器里直接调用."""

    started = time.perf_counter()
    before_bytes = path.stat().st_size
    # isolation_level=None 走自动提交: VACUUM 不能在显式事务里执行.
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    try:
        pages_before = _pragma_int(connection, "page_count")
        free_pages_before = _pragma_int(connection, "freelist_count")
        checkpointed = _checkpoint(connection)
        connection.execute("VACUUM")
        pages_after = _pragma_int(connection, "page_count")
    finally:
        connection.close()
    after_bytes = path.stat().st_size
    return DbVacuumResult(
        before_bytes=before_bytes,
        after_bytes=after_bytes,
        pages_before=pages_before,
        pages_after=pages_after,
        free_pages_before=free_pages_before,
        duration_ms=int((time.perf_counter() - started) * 1000),
        wal_checkpointed=checkpointed,
    )


def _pragma_int(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    assert row is not None
    return int(row[0])


def _checkpoint(connection: sqlite3.Connection) -> bool:
    """先把 WAL 合回主库; 合不上不阻断 VACUUM (它自己会处理)."""

    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error:
        return False
    return True
