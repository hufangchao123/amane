from typing import TYPE_CHECKING

from .models import WashApplyPayload, WashApplyResult, WashScanPayload, WashScanResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config.manager import HotSettings
    from ..db.repository import Repository
    from ._common import LibraryTaskLocks


class WashScanHandler(TaskHandler[WashScanPayload, WashScanResult]):
    """扫描同番号多份文件, 产出候选分组与可回收体积.

    只读, 不改库内路径, 因此不取 ``LibraryTaskLocks``.
    """

    def __init__(self, repo: Repository, hot: HotSettings) -> None:
        super().__init__(payload_t=WashScanPayload, result_t=WashScanResult)
        self._repo = repo
        self._hot = hot

    async def handle(self, payload: WashScanPayload) -> TaskResult[WashScanResult]:
        # TODO(phase1): 见 amane-merge/notes/wash.md
        return TaskResult(success=False, error="洗版扫描尚未实现")


class WashApplyHandler(TaskHandler[WashApplyPayload, WashApplyResult]):
    """同番号多份文件留最优的一份, 其余移入回收站并登记 ``wash_records``.

    会移动库内文件, 因此与整理 / 删除共用 ``LibraryTaskLocks``, 不得绕过锁直接改库内路径.
    """

    def __init__(self, repo: Repository, hot: HotSettings, library_locks: LibraryTaskLocks) -> None:
        super().__init__(payload_t=WashApplyPayload, result_t=WashApplyResult)
        self._repo = repo
        self._hot = hot
        self._library_locks = library_locks

    async def handle(self, payload: WashApplyPayload) -> TaskResult[WashApplyResult]:
        # TODO(phase1): 见 amane-merge/notes/wash.md
        return TaskResult(success=False, error="洗版执行尚未实现")
