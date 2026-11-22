"""历史回收站 (``.amane_trash``): 统计占用与按保留期永久删除.

目录里只有相对路径, 没有原位信息 —— 无法判定一个文件原本属于哪个目录, 因此这里**不提供还原**:
猜错会把文件搬进别人的番号目录. 要还原就手动移出, 移出后按新文件重新入库 (见 data-model.md).

删除按修改时间与保留天数判定, 不看库内索引: 回收站里的文件本就不在索引里.
"""

import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ...utils.threads import in_thread

#: 保留天数上限; 超过一年半的保留期等于不清理, 取值过大多半是误输入.
MAX_KEEP_DAYS = 3650


@dataclass(frozen=True)
class TrashStats:
    """一个回收站目录的现状; ``oldest_days`` 为空表示目录里没有文件."""

    files: int
    bytes: int
    oldest_days: int | None


@dataclass
class PurgeOutcome:
    scanned: int = 0
    purged: int = 0
    bytes_freed: int = 0
    kept: int = 0
    failed: int = 0
    samples: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _File:
    path: Path
    size: int
    age_days: int


def _walk(trash_dir: Path, now: datetime) -> list[_File]:
    files: list[_File] = []
    for root, _dirs, names in os.walk(trash_dir):
        for name in names:
            path = Path(root, name)
            try:
                stat = path.stat()
            except OSError:
                continue
            age = (now - datetime.fromtimestamp(stat.st_mtime, UTC)).days
            files.append(_File(path=path, size=stat.st_size, age_days=age))
    return files


def _stats(files: list[_File]) -> TrashStats:
    if not files:
        return TrashStats(files=0, bytes=0, oldest_days=None)
    oldest = max(file.age_days for file in files)
    return TrashStats(files=len(files), bytes=sum(file.size for file in files), oldest_days=oldest)


@in_thread
def scan(trash_dir: Path) -> TrashStats:
    """统计一个回收站目录, 不动文件."""
    if not trash_dir.is_dir():
        return TrashStats(files=0, bytes=0, oldest_days=None)
    return _stats(_walk(trash_dir, datetime.now(UTC)))


@in_thread
def purge(trash_dir: Path, keep_days: int, *, dry_run: bool) -> PurgeOutcome:
    """永久删除修改时间早于 ``keep_days`` 的文件, 并回收腾空的目录.

    ``dry_run`` 只统计不删除. 单个文件删不掉时记账继续, 不中断其余文件.
    """
    outcome = PurgeOutcome()
    if not trash_dir.is_dir():
        return outcome
    cutoff = timedelta(days=max(1, keep_days))
    now = datetime.now(UTC)
    for item in _walk(trash_dir, now):
        outcome.scanned += 1
        if timedelta(days=item.age_days) < cutoff:
            outcome.kept += 1
            continue
        if dry_run:
            outcome.purged += 1
            outcome.bytes_freed += item.size
            outcome.samples.append(item.path.relative_to(trash_dir).as_posix())
            continue
        try:
            item.path.unlink()
        except OSError:
            outcome.failed += 1
            continue
        outcome.purged += 1
        outcome.bytes_freed += item.size
        if len(outcome.samples) < 20:
            outcome.samples.append(item.path.relative_to(trash_dir).as_posix())
    if not dry_run and outcome.purged:
        _prune_dirs(trash_dir)
    return outcome


def _prune_dirs(trash_dir: Path) -> None:
    """删掉清空后的子目录; 回收站根目录保留, 它的存在本身是「回收站已启用」的信号."""
    for root, _dirs, _names in os.walk(trash_dir, topdown=False):
        path = Path(root)
        if path == trash_dir:
            continue
        try:
            if any(path.iterdir()):
                continue
        except OSError:
            continue
        shutil.rmtree(path, ignore_errors=True)
