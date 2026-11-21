from typing import TYPE_CHECKING

from .models import Transfer115Payload, Transfer115Result
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config.manager import HotSettings
    from ..db.repository import Repository


class Transfer115Handler(TaskHandler[Transfer115Payload, Transfer115Result]):
    """把网盘源目录里的新增文件搬进本地 STRM 根目录, 逐文件状态落 ``transfer_items``.

    搬移在网盘侧发生, 本地只落 STRM 清单; 网盘客户端在这里构造, 因为只有本任务用到它.
    """

    def __init__(self, repo: Repository, hot: HotSettings) -> None:
        super().__init__(payload_t=Transfer115Payload, result_t=Transfer115Result)
        self._repo = repo
        self._hot = hot

    async def handle(self, payload: Transfer115Payload) -> TaskResult[Transfer115Result]:
        # TODO(phase1): 见 amane-merge/notes/transfer.md
        return TaskResult(success=False, error="115 中转尚未实现")
