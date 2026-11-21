from __future__ import annotations

from typing import TYPE_CHECKING

from .models import BackupPayload, BackupResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..backup import BackupManager


class BackupHandler(TaskHandler[BackupPayload, BackupResult]):
    """生成一份备份并按 ``backup.keep_db`` / ``backup.keep_assets`` 修剪更早的产物.

    备份失败时不抛出异常: 任务结果里的 ``error`` 是唯一会呈现给用户的位置, 抛出只会
    让任务进入重试, 而重试同样会失败 (磁盘满、目录不可写).
    """

    def __init__(self, manager: BackupManager) -> None:
        super().__init__(payload_t=BackupPayload, result_t=BackupResult)
        self._manager = manager

    async def handle(self, payload: BackupPayload) -> TaskResult[BackupResult]:
        try:
            outcome = await self._manager.run(payload.kind)
        except RuntimeError as error:
            return TaskResult(success=False, error=str(error))
        except OSError as error:
            return TaskResult(success=False, error=f"备份写入失败: {error}")
        return TaskResult(
            success=True,
            result=BackupResult(files=outcome.files, bytes_written=outcome.bytes_written, pruned=outcome.pruned),
        )
