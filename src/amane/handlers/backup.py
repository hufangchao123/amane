from typing import TYPE_CHECKING

from .models import BackupPayload, BackupResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..backup import BackupManager


class BackupHandler(TaskHandler[BackupPayload, BackupResult]):
    """生成一份备份并按 ``backup.keep_db`` / ``backup.keep_assets`` 修剪旧份数."""

    def __init__(self, manager: BackupManager) -> None:
        super().__init__(payload_t=BackupPayload, result_t=BackupResult)
        self._manager = manager

    async def handle(self, payload: BackupPayload) -> TaskResult[BackupResult]:
        # TODO(phase2): 见 amane-merge/notes/backup.md
        return TaskResult(success=False, error="备份尚未实现")
