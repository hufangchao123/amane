from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..config.manager import BackupConfig


class BackupEntry(BaseModel):
    name: str
    kind: Literal["db", "assets"]
    size_bytes: int
    created_at: datetime


class BackupManager:
    """备份产物列举与修剪; 生成走 ``BACKUP`` 任务.

    数据库备份使用 SQLite 在线备份接口, 不复制正在写入的库文件.
    """

    def __init__(self, config: BackupConfig, data_dir: Path) -> None:
        self._config = config
        self._data_dir = data_dir

    @property
    def root(self) -> Path:
        return Path(self._config.dir) if self._config.dir else self._data_dir / "backups"

    async def list_backups(self) -> list[BackupEntry]:
        # TODO(phase2): 读 root 下的清单索引
        return []
