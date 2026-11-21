"""备份产物: 数据库用 SQLite 在线备份接口, 资源目录打成归档.

在线备份接口在源库处于 WAL 且正在写入时仍然可用, 直接复制库文件则会得到损坏的副本,
因此数据库只走 ``sqlite3.Connection.backup``. 备份产物按时间戳命名, 修剪依据名字里的时间戳,
不依据文件修改时间: 拷贝与恢复都会改写修改时间.
"""

from __future__ import annotations

import re
import sqlite3
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from ..utils.threads import in_thread

if TYPE_CHECKING:
    from ..config.manager import BackupConfig

BackupKind = Literal["db", "assets"]

DB_FILES: tuple[str, ...] = ("amane.db", "embeddings.db")
"""``data_dir`` 下需要备份的 SQLite 库; 后者是向量 sidecar, 缺失时跳过."""

ASSETS_DIR_NAME = "resources"
"""资源目录: URL 级下载缓存. 不入备份的内容是媒体文件本身, 它们不在 ``data_dir`` 内."""

_TIMESTAMP = r"(\d{8}-\d{6})"
_DB_NAME = re.compile(rf"^(?:amane|embeddings)-db-{_TIMESTAMP}\.sqlite$")
_ASSETS_NAME = re.compile(rf"^assets-{_TIMESTAMP}\.tar\.gz$")


class BackupEntry(BaseModel):
    name: str
    kind: BackupKind
    size_bytes: int
    created_at: datetime


class BackupOutcome(BaseModel):
    files: list[str] = []
    bytes_written: int = 0
    pruned: int = 0


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%d-%H%M%S")


def _parse_stamp(name: str) -> datetime | None:
    match = _DB_NAME.match(name) or _ASSETS_NAME.match(name)
    if match is None:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=UTC)


@in_thread
def _copy_database(source: Path, target: Path) -> int:
    """在线备份单个库; 返回目标文件大小. 源库以只读 URI 打开, 不触发迁移与写入."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    return target.stat().st_size


@in_thread
def _archive_assets(source: Path, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(target, "w:gz") as archive:
        archive.add(source, arcname=source.name)
    return target.stat().st_size


class BackupManager:
    """备份产物的生成、列举与修剪; 生成走 ``BACKUP`` 任务, 路由只负责列举."""

    def __init__(self, config: BackupConfig, data_dir: Path) -> None:
        self._config = config
        self._data_dir = data_dir

    @property
    def root(self) -> Path:
        return Path(self._config.dir) if self._config.dir else self._data_dir / "backups"

    async def list_backups(self) -> list[BackupEntry]:
        """按时间倒序列出产物; 目录不存在时返回空列表而不是报错 (首次运行前是正常状态)."""
        return await _scan(self.root)

    async def run(self, kind: Literal["db", "assets", "all"] = "all") -> BackupOutcome:
        """生成备份并按 ``keep_db`` / ``keep_assets`` 修剪更早的产物."""
        outcome = BackupOutcome()
        if not self._config.enabled:
            raise RuntimeError("备份未启用")
        moment = datetime.now(UTC)
        if kind in ("db", "all"):
            outcome.files.extend(await self._backup_databases(moment))
        if kind in ("assets", "all"):
            assets = await self._backup_assets(moment)
            if assets is not None:
                outcome.files.append(assets)
        outcome.bytes_written = sum((self.root / name).stat().st_size for name in outcome.files)
        outcome.pruned = await self._prune(kind)
        return outcome

    async def _backup_databases(self, moment: datetime) -> list[str]:
        stamp = _stamp(moment)
        written: list[str] = []
        for name in DB_FILES:
            source = self._data_dir / name
            if not source.is_file():
                continue
            target_name = f"{name.removesuffix('.db')}-db-{stamp}.sqlite"
            await _copy_database(source, self.root / target_name)
            written.append(target_name)
        return written

    async def _backup_assets(self, moment: datetime) -> str | None:
        source = self._data_dir / ASSETS_DIR_NAME
        if not source.is_dir():
            return None
        target_name = f"assets-{_stamp(moment)}.tar.gz"
        await _archive_assets(source, self.root / target_name)
        return target_name

    async def _prune(self, kind: Literal["db", "assets", "all"]) -> int:
        """修剪超出保留份数的产物; 只删除本类前缀, 不触碰目录内其它文件."""
        pruned = 0
        if kind in ("db", "all"):
            pruned += await _prune_group(self.root, _DB_NAME, self._config.keep_db)
        if kind in ("assets", "all"):
            pruned += await _prune_group(self.root, _ASSETS_NAME, self._config.keep_assets)
        return pruned


@in_thread
def _scan(root: Path) -> list[BackupEntry]:
    if not root.is_dir():
        return []
    entries: list[BackupEntry] = []
    for path in root.iterdir():
        if not path.is_file():
            continue
        kind: BackupKind | None = None
        if _DB_NAME.match(path.name):
            kind = "db"
        elif _ASSETS_NAME.match(path.name):
            kind = "assets"
        if kind is None:
            continue
        created_at = _parse_stamp(path.name) or datetime.fromtimestamp(path.stat().st_mtime, UTC)
        entries.append(BackupEntry(name=path.name, kind=kind, size_bytes=path.stat().st_size, created_at=created_at))
    entries.sort(key=lambda entry: entry.created_at, reverse=True)
    return entries


@in_thread
def _prune_group(root: Path, pattern: re.Pattern[str], keep: int) -> int:
    """按时间戳分组修剪. 一次数据库备份产出两个文件, 份数按时间戳计算, 不按文件数计算."""
    if not root.is_dir():
        return 0
    stamps: dict[str, list[Path]] = {}
    for path in root.iterdir():
        match = pattern.match(path.name)
        if match is None or not path.is_file():
            continue
        stamps.setdefault(match.group(1), []).append(path)
    removed = 0
    for stamp in sorted(stamps, reverse=True)[keep:]:
        for path in stamps[stamp]:
            path.unlink()
            removed += 1
    return removed
