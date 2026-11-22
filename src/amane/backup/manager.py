"""备份产物: 数据库用 SQLite 在线备份接口, 资源目录打成归档.

在线备份接口在源库处于 WAL 且正在写入时仍然可用, 直接复制库文件则会得到损坏的副本,
因此数据库只走 ``sqlite3.Connection.backup``. 备份产物按时间戳命名, 修剪依据名字里的时间戳,
不依据文件修改时间: 拷贝与恢复都会改写修改时间.

## 目录边界

修剪会删文件, 而 ``backup.dir`` 是自由配置, 所以写入前先确认目录归 amane 使用: ``data_dir``
之内直接放行; 之外只接受不存在、空、或只装本工具产物的目录, 其余要求放一个空标记文件
``ROOT_MARKER`` 才算声明过归属. ``data_dir`` 本身及其上级一律拒绝 —— 那等于把库目录当成
修剪目标.

备份内容只有 ``DB_FILES`` 与 ``ASSETS_DIR_NAME``, **不含** ``config.toml``: 配置里有凭据,
归档出去等于把凭据复制一份到备份目录.
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

ROOT_MARKER = ".amane-backup-root"
"""外部备份目录的归属标记: 目录里已有非备份文件时, 运维需要放这个空文件确认归 amane 使用."""


class BackupRootError(RuntimeError):
    """备份根目录不可用, 或无法确认它归 amane 使用."""


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
        return Path(self._config.dir).expanduser() if self._config.dir else self._data_dir / "backups"

    async def list_backups(self) -> list[BackupEntry]:
        """按时间倒序列出产物; 目录不存在时返回空列表而不是报错 (首次运行前是正常状态)."""
        return await _scan(self.root)

    async def run(self, kind: Literal["db", "assets", "all"] = "all") -> BackupOutcome:
        """生成备份并按 ``keep_db`` / ``keep_assets`` 修剪更早的产物."""
        outcome = BackupOutcome()
        if not self._config.enabled:
            raise RuntimeError("备份未启用")
        root = self._checked_root()
        moment = datetime.now(UTC)
        if kind in ("db", "all"):
            outcome.files.extend(await self._backup_databases(moment, root))
        if kind in ("assets", "all"):
            assets = await self._backup_assets(moment, root)
            if assets is not None:
                outcome.files.append(assets)
        outcome.bytes_written = sum((root / name).stat().st_size for name in outcome.files)
        outcome.pruned = await self._prune(kind, root)
        return outcome

    def _checked_root(self) -> Path:
        """解析备份根并校验边界; 不通过时抛 ``BackupRootError``, 由 handler 转成任务失败."""
        resolved = self.root.resolve()
        data_dir = self._data_dir.resolve()
        if resolved == data_dir or resolved in data_dir.parents:
            raise BackupRootError(f"备份根不能是 data_dir 本身或其上级目录: {resolved}")
        if data_dir in resolved.parents:
            return resolved
        _ensure_owned(resolved)
        return resolved

    async def _backup_databases(self, moment: datetime, root: Path) -> list[str]:
        stamp = _stamp(moment)
        written: list[str] = []
        for name in DB_FILES:
            source = self._data_dir / name
            if not source.is_file():
                continue
            target_name = f"{name.removesuffix('.db')}-db-{stamp}.sqlite"
            await _copy_database(source, root / target_name)
            written.append(target_name)
        return written

    async def _backup_assets(self, moment: datetime, root: Path) -> str | None:
        source = self._data_dir / ASSETS_DIR_NAME
        if not source.is_dir():
            return None
        target_name = f"assets-{_stamp(moment)}.tar.gz"
        await _archive_assets(source, root / target_name)
        return target_name

    async def _prune(self, kind: Literal["db", "assets", "all"], root: Path) -> int:
        """修剪超出保留份数的产物; 只删除本类前缀, 不触碰目录内其它文件."""
        pruned = 0
        if kind in ("db", "all"):
            pruned += await _prune_group(root, _DB_NAME, self._config.keep_db)
        if kind in ("assets", "all"):
            pruned += await _prune_group(root, _ASSETS_NAME, self._config.keep_assets)
        return pruned


def _is_backup_name(name: str) -> bool:
    """文件名是否是本工具自己产出的产物 (修剪的唯一删除范围)."""
    return _DB_NAME.match(name) is not None or _ASSETS_NAME.match(name) is not None


def _ensure_owned(root: Path) -> None:
    """外部目录须能自证归属: 不存在、空、或只装本工具产物, 否则要求标记文件.

    判据刻意不看目录名与权限: 配置写错时 (指到媒体库、``$HOME``) 目录里会有一堆无关文件,
    那就是最该拦下的情形; 反过来历史备份目录只装 ``*-db-*.sqlite``, 不需要任何额外操作.
    隐藏文件不计入「无关」, ``.DS_Store`` 之类不该拦住备份.
    """
    if not root.exists():
        return
    if not root.is_dir():
        raise BackupRootError(f"备份根不是目录: {root}")
    if (root / ROOT_MARKER).is_file():
        return
    foreign = sorted(
        path.name for path in root.iterdir() if not path.name.startswith(".") and not _is_backup_name(path.name)
    )
    if not foreign:
        return
    shown = ", ".join(foreign[:3]) + (" 等" if len(foreign) > 3 else "")
    raise BackupRootError(
        f"备份根 {root} 里有非备份文件 ({shown}); 确认该目录归 amane 使用, 请在其中放置空标记文件 {ROOT_MARKER}"
    )


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
