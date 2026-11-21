"""115 中转的状态聚合; 搬移本体在 ``TRANSFER_115`` 任务里, 本类不执行 I/O."""

from collections.abc import Mapping

from pydantic import BaseModel

from ..config.manager import TransferConfig
from ..db.models import TransferItemState


class TransferSummary(BaseModel):
    pending: int = 0
    moved: int = 0
    skipped: int = 0
    failed: int = 0

    @classmethod
    def from_counts(cls, counts: Mapping[TransferItemState, int]) -> TransferSummary:
        """缺省状态按 0 计: 空表时状态卡显示全零, 而不是等某个状态先出现."""
        return cls(
            pending=counts.get(TransferItemState.PENDING, 0),
            moved=counts.get(TransferItemState.MOVED, 0),
            skipped=counts.get(TransferItemState.SKIPPED, 0),
            failed=counts.get(TransferItemState.FAILED, 0),
        )


class TransferService:
    """构造时只拿配置, 请求内即可安全新建, 不持有跨请求状态."""

    def __init__(self, config: TransferConfig) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        """配置齐全才算启用: 缺 cookie 或目录时任务会立刻失败, 不如直接显示未启用."""
        return bool(
            self._config.enabled and self._config.cookies and self._config.source_dir and self._config.target_dir
        )

    def unavailable_reason(self) -> str:
        """缺什么就说什么: 这条文案同时是配置指引."""
        missing = [
            label
            for label, ok in (
                ("enabled 开关", self._config.enabled),
                ("Cookie", self._config.cookies),
                ("源目录", self._config.source_dir),
                ("目标目录", self._config.target_dir),
            )
            if not ok
        ]
        return f"115 中转未启用: 缺 {'、'.join(missing)}"
