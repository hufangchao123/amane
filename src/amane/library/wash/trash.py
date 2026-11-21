"""洗版回收站: 落点、搬移、还原与真删.

不写 ``.amane_trash``: 那个目录是历史遗留, ``docs/dev/data-model.md`` 已声明不再写入新内容,
清理任务还会按旧规则整体回收它 —— 洗版把文件放进去等于放弃还原窗口, 也等于把两种语义混在
一个目录里. 这里用独立的 ``.amane_wash``.

## 布局

``<库根父目录>/.amane_wash/<库根名>/<local|strm>/<库内相对路径>``

三条理由:

* **与库根同级**: 绝大多数情况下同一文件系统, ``shutil.move`` 退化成 ``rename``, 挪 10 GB 不
  复制. 放到 ``data_dir`` 下则很可能跨盘, 一次洗版等于整盘抄一遍.
* **镜像库内相对路径**: ``removed_path`` 去掉 ``.amane_wash/<库根名>/<kind>`` 三级就是原位,
  还原不需要额外的表字段 —— ``wash_records`` 没有 origin_path 列, 也不允许改它.
* **带库根名**: 同一父目录下常常并排多个库, 不收拢会互相覆盖同名文件.

目标位置已被占用时一律跳过, 绝不覆盖: 同一原位在回收站窗口内又出现第二份, 说明有人手工放回来
了, 覆盖会丢数据, 交给人来看.

库根嵌套 (库 A 的根包含库 B 的根) 时回收站可能落在库 A 内部, 因此扫描要跳过回收站内的路径,
见 ``is_trash_path``; 要彻底避免需要把 ``.amane_wash`` 加进 ``library.rules`` 的忽略名单,
那是共享文件, 见 ``amane-merge/notes/wash.md`` 的「需整合会话」.

## 真删

只有 ``purge`` 会真删, 且只删 ``expires_at`` 已过的记录 (到期判定在调用方). 删完自底向上
``rmdir`` 到 ``.amane_wash`` 根, 根自身保留. 不复用 ``library.delete``: 那个执行单元拒绝库根
之外的目标, 而回收站按设计就在库根外.
"""

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

from ...db.models import WashRecordKind
from ...utils.path import existing_disk_path, path_is_under
from ...utils.threads import in_thread

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = structlog.get_logger()

#: 回收站根名. 与 ``library.rules.TRASH_DIRNAME`` (``.amane_trash``) 刻意不同: 两者语义不同.
TRASH_DIRNAME = ".amane_wash"

MoveStatus = Literal["moved", "occupied", "missing", "failed"]


@dataclass(frozen=True)
class MoveOutcome:
    """一次搬移的结果. ``size`` 是这次搬动真正腾出的字节数 (符号链接按 0 计)."""

    status: MoveStatus
    target: Path | None = None
    size: int = 0
    error: str | None = None


@dataclass
class PurgeReport:
    """一批过期记录的清理结果."""

    purged: int = 0
    missing: int = 0
    failed: int = 0
    freed_bytes: int = 0
    errors: list[str] = field(default_factory=list)


def trash_root(library_root: str | Path) -> Path:
    """某个库的回收站根: ``<库根父目录>/.amane_wash/<库根名>``."""
    root = Path(library_root)
    return root.parent / TRASH_DIRNAME / root.name


def is_trash_path(path: str | Path) -> bool:
    """路径是否落在洗版回收站里 (任一段等于 ``.amane_wash``)."""
    return any(part == TRASH_DIRNAME for part in Path(path).parts)


def _trash_anchor(path: Path) -> Path | None:
    """``.amane_wash`` 这一级目录本身, 用于把剪枝停在回收站根上."""
    parts = Path(path).parts
    for index, part in enumerate(parts):
        if part == TRASH_DIRNAME:
            return Path(*parts[: index + 1])
    return None


def trash_target(library_root: str | Path, *, kind: WashRecordKind, relative: Path) -> Path:
    """库内文件在回收站里的落点; ``relative`` 是相对库根的路径."""
    return trash_root(library_root) / kind.value / relative


def restore_target(removed_path: str | Path, *, library_root: str | Path, kind: WashRecordKind) -> Path | None:
    """还原落点 = 库根 + 回收站内的相对路径; 记录与库根对不上时返回 None.

    库根可能在记录之后被改名或移动, 因此这里不缓存任何路径, 每次按当前库根重算.
    """
    base = trash_root(library_root) / kind.value
    removed = Path(removed_path)
    if not path_is_under(removed, base):
        return None
    try:
        relative = removed.relative_to(base)
    except ValueError:
        # path_is_under 比较 NFC 与大小写, 字面 relative_to 可能在别名路径上失败.
        return None
    if not relative.parts:
        return None
    return Path(library_root) / relative


def _freed_size(path: Path) -> int:
    """搬走或删除一个路径真正腾出的字节: 符号链接只删链接自身, 按 0 计."""
    st = path.lstat()
    if path.is_symlink():
        return 0
    return st.st_size


@in_thread
def move_into_trash(source: str | Path, *, library_root: str | Path, kind: WashRecordKind) -> MoveOutcome:
    """把库内的一份落选文件搬进回收站, 返回落点与实际腾出的字节.

    拒绝库根之外、回收站之内与 ``.amane_trash`` 之内的源; 目标已存在时返回 ``occupied``
    而不是覆盖.
    """
    src = existing_disk_path(source, follow_symlinks=False)
    if src is None:
        return MoveOutcome(status="missing")
    root = Path(library_root)
    if not path_is_under(src, root):
        return MoveOutcome(status="failed", error=f"source outside library root: {src}")
    if is_trash_path(src):
        return MoveOutcome(status="failed", error=f"source already in wash trash: {src}")
    if src.is_dir():
        return MoveOutcome(status="failed", error=f"source is a directory: {src}")

    relative = Path(os.path.relpath(src, root))
    if relative.parts and relative.parts[0] == "..":
        return MoveOutcome(status="failed", error=f"source outside library root: {src}")
    target = trash_target(root, kind=kind, relative=relative)
    try:
        if existing_disk_path(target, follow_symlinks=False) is not None:
            return MoveOutcome(status="occupied", target=target, error=f"trash slot occupied: {target}")
        size = _freed_size(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
    except OSError as exc:
        logger.warning("wash move failed", source=str(src), target=str(target), error=str(exc))
        return MoveOutcome(status="failed", target=target, error=str(exc))
    return MoveOutcome(status="moved", target=target, size=size)


@in_thread
def restore_from_trash(removed_path: str | Path, *, library_root: str | Path, kind: WashRecordKind) -> MoveOutcome:
    """把回收站里的一份搬回库内原位; 原位已被占用时返回 ``occupied`` 而不是覆盖."""
    src = existing_disk_path(removed_path, follow_symlinks=False)
    if src is None:
        return MoveOutcome(status="missing")
    if not is_trash_path(src):
        return MoveOutcome(status="failed", error=f"not a wash trash path: {src}")
    target = restore_target(src, library_root=library_root, kind=kind)
    if target is None:
        return MoveOutcome(status="failed", error=f"trash record does not match library root: {src}")
    try:
        if existing_disk_path(target, follow_symlinks=False) is not None:
            return MoveOutcome(status="occupied", target=target, error=f"restore target occupied: {target}")
        size = _freed_size(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))
    except OSError as exc:
        logger.warning("wash restore failed", source=str(src), target=str(target), error=str(exc))
        return MoveOutcome(status="failed", target=target, error=str(exc))
    _prune_ancestors(src)
    return MoveOutcome(status="moved", target=target, size=size)


@in_thread
def purge_trash_paths(paths: Sequence[str | Path]) -> PurgeReport:
    """删除一批已到期的回收站文件并回收变空的目录.

    整批一次性进线程池: 到期清理动辄上百个文件, 逐个 ``await`` 会把事件循环切碎
    (``docs/dev/ops.md`` 的 5 ms 预算). 调用方保证这些记录已经到期.
    """
    report = PurgeReport()
    purged_paths: list[Path] = []
    for raw in paths:
        path = existing_disk_path(raw, follow_symlinks=False)
        if path is None:
            report.missing += 1
            continue
        if not is_trash_path(path):
            report.failed += 1
            report.errors.append(f"not a wash trash path: {path}")
            continue
        try:
            size = _freed_size(path)
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            logger.warning("wash purge failed", path=str(path), error=str(exc))
            report.failed += 1
            report.errors.append(f"{path}: {exc}")
            continue
        report.purged += 1
        report.freed_bytes += size
        purged_paths.append(path)
    _prune_purged_dirs(purged_paths)
    return report


def _prune_purged_dirs(paths: Iterable[Path]) -> None:
    """删完文件后自底向上收拾空目录, 每一步都停在 ``.amane_wash`` 根上."""
    for path in paths:
        for parent in Path(path).parents:
            anchor = _trash_anchor(parent)
            if anchor is None or parent == anchor:
                break
            if not _rmdir_if_empty(parent):
                break


def _prune_ancestors(path: Path) -> None:
    """单个文件被搬走后的剪枝, 语义同 ``_prune_purged_dirs``."""
    for parent in Path(path).parents:
        anchor = _trash_anchor(parent)
        if anchor is None or parent == anchor:
            break
        if not _rmdir_if_empty(parent):
            break


def _rmdir_if_empty(path: Path) -> bool:
    """只在目录已经空掉时删除; 非空/不存在都算「停下」, 其余错误记日志后同样停下."""
    try:
        path.rmdir()
    except OSError as exc:
        if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
            return False
        logger.warning("wash prune failed", path=str(path), error=str(exc))
        return False
    return True
