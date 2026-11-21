from pydantic import BaseModel

from ..config.manager import TransferConfig


class TransferSummary(BaseModel):
    pending: int = 0
    moved: int = 0
    skipped: int = 0
    failed: int = 0


class TransferService:
    """115 中转的状态聚合; 搬移本体在 ``TRANSFER_115`` 任务里, 本类不执行 I/O.

    构造时只拿配置, 请求内即可安全新建, 不持有跨请求状态.
    """

    def __init__(self, config: TransferConfig) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        """配置齐全才算启用: 缺 cookies 或目录时任务会立刻失败, 不如面板直接显示未启用."""
        return bool(
            self._config.enabled and self._config.cookies and self._config.source_dir and self._config.target_dir
        )

    async def summary(self) -> TransferSummary:
        # TODO(phase1): 按 transfer_items 的 state 分组计数
        return TransferSummary()
