"""把库索引与磁盘实况拼成洗版的输入.

候选来自 ``MediaFile`` 索引而不是目录遍历: 只有入过库的文件才有番号与整理后的位置, 未入库的
文件属于整理流程的职责, 光凭文件名猜重复风险太大.

磁盘现状只用来做两件事: 确认文件还在 (索引可能比磁盘旧), 取真实体积 (索引里的 size 可能为空
或过期). 解析文件名与 stat 都在线程池里一次做完: 几千个文件逐个 ``await`` 会把事件循环切碎,
``docs/dev/ops.md`` 的预算是 5 ms.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ...db.models import WashRecordKind
from ...parsing import parse_file_info
from ...utils.path import existing_disk_path
from ...utils.threads import in_thread
from ..rules import MEDIA_EXTENSIONS, is_in_trash
from .model import WashEntry
from .trash import is_trash_path

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from ...db.models import MediaFile, Mosaic
    from ...db.repository import Repository

logger = structlog.get_logger()

#: STRM 是云端指针, 洗版从不按体积判定它 (二次审计 P0-7).
STRM_EXTENSION = ".strm"


@dataclass(frozen=True, slots=True)
class IndexRow:
    """``MediaFile`` 里与洗版相关的部分; 单独取出来是为了让解析与 stat 能进线程池."""

    path: str
    number: str | None
    size: int | None
    duration: float | None
    definition: str | None
    mosaic: Mosaic | None


def index_row(media: MediaFile) -> IndexRow:
    return IndexRow(
        path=media.path,
        number=media.number,
        size=media.size,
        duration=media.duration,
        definition=media.definition,
        mosaic=media.mosaic,
    )


def _mtime_of(path: str) -> float | None:
    """文件的修改时间; 读不到就返回 None, 让排序把它排到「没有时间」那一侧."""
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


@in_thread
def entries_from_rows(rows: Sequence[IndexRow], *, library_id: int) -> list[WashEntry]:
    """把一批索引行变成参与比较的条目; 磁盘上已经不存在的本地文件直接丢弃.

    丢弃而不是记一条候选: 洗版的落选者要被搬走, 搬不存在的文件只会制造一条无法解释的失败记录.
    """
    entries: list[WashEntry] = []
    for row in rows:
        path = row.path
        if not path:
            continue
        # 索引理论上不该含回收站里的文件, 但库根嵌套时 ``.amane_wash`` 会落在别的库内.
        if is_in_trash(Path(path)) or is_trash_path(path):
            continue
        kind = _kind_of(path)
        if kind is None:
            continue
        info = parse_file_info(path=path)
        number = (row.number or "").strip() or (info.number or "").strip()
        if not number:
            # 番号解析不出来的文件没有分组依据.
            continue
        size = 0
        if kind is WashRecordKind.LOCAL:
            size = _disk_size(path)
            if size is None:
                continue
        elif existing_disk_path(path, follow_symlinks=False) is None:
            # STRM 不看体积, 但仍要确认指针文件本身还在: 已经洗进回收站的那份不能再进候选,
            # 否则每次扫描都会把它重新算一遍, 幂等与「可回收组数」都会失真.
            continue
        entries.append(
            WashEntry(
                path=path,
                kind=kind,
                library_id=library_id,
                number=number,
                size=size,
                mtime=_mtime_of(path),
                duration=row.duration,
                definition=row.definition or info.definition,
                mosaic=row.mosaic or info.mosaic,
                cd=info.cd,
            )
        )
    return entries


def _kind_of(path: str) -> WashRecordKind | None:
    """按扩展名分本地与 STRM; STRM 不当作本地视频 (P0-7)."""
    suffix = Path(path).suffix.casefold()
    if suffix == STRM_EXTENSION:
        return WashRecordKind.STRM
    if suffix in MEDIA_EXTENSIONS:
        return WashRecordKind.LOCAL
    return None


def _disk_size(path: str) -> int | None:
    """磁盘实况体积; 不存在或不是普通文件时返回 None. 符号链接跟随目标 (排序看目标大小)."""
    disk_path = existing_disk_path(path)
    if disk_path is None:
        return None
    try:
        file_stat = disk_path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(file_stat.st_mode):
        return None
    return file_stat.st_size


async def collect_entries(
    repo: Repository,
    *,
    kinds: Iterable[WashRecordKind],
    library_id: int | None = None,
) -> list[WashEntry]:
    """读取库索引 (可选限定一个库) 并拼出条目.

    ``library_id`` 不存在时按「没有库」处理, 由调用方决定要不要报错 —— 扫描任务用的是「全部库」,
    路由用的是明确的单个库.
    """
    wanted = set(kinds)
    if not wanted:
        return []
    libraries = [await repo.get_library(library_id)] if library_id is not None else await repo.list_libraries()
    entries: list[WashEntry] = []
    for library in libraries:
        if library is None or library.id is None:
            continue
        rows = [index_row(media) for media in await repo.list_media_files(library_id=library.id, limit=None)]
        entries.extend(await entries_from_rows(rows, library_id=library.id))
    if len(wanted) == 1:
        only = next(iter(wanted))
        entries = [entry for entry in entries if entry.kind is only]
    return entries
