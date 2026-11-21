from pydantic import BaseModel

from ...config.manager import WashConfig


class WashCandidate(BaseModel):
    """一组同番号文件的洗版候选; 只在 ``dry_run`` 面板预览时返回."""

    group_key: str
    keep_path: str
    drop_paths: list[str] = []
    bytes_freed: int = 0


class WashSummary(BaseModel):
    groups: int = 0
    """存在多份文件的分组数."""
    trashed: int = 0
    """回收站内可还原的记录数."""
    restorable_bytes: int = 0


class WashService:
    """洗版的只读视图: 候选分组与回收站现状; 移动与还原都在任务里.

    本模块不导出到 ``amane.library`` 的公共面, 因为配置层反过来依赖 ``library``,
    在此处汇入会形成循环导入.
    """

    def __init__(self, config: WashConfig) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        return bool(self._config.enabled and (self._config.local or self._config.strm))

    async def summary(self) -> WashSummary:
        # TODO(phase1): wash_records 计数与体积聚合
        return WashSummary()
