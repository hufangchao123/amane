"""库内文件与目录的删除执行单元.

调用方给出目标与库根, 本模块只做磁盘操作: 不读配置, 不访问数据库,
不推导调用方未给出的路径 — 清单之外的路径一律拒绝.

边界判定使用字面路径 (``path_is_under``), 基准是调用方给出的库根与附加根, 不是当前库设置:
库内可能存在指向库外的符号链接, 解析符号链接会把库外内容纳入范围. 符号链接以外的目标
另按真实路径复核一次 (``resolved_path``): 目录链接 (Windows 的 junction 与挂载点) 在枚举与
删除里都当普通目录处理, 字面路径判定拦不到它里面指向库外的内容.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import structlog

from ..utils.path import disk_probe, path_is_under, resolved_path
from ..utils.threads import in_thread
from .rules import TRASH_DIRNAME, WASH_TRASH_DIRNAME, is_under_any

logger = structlog.get_logger()

DeleteStatus = Literal["deleted", "changed", "failed"]

# Windows 上 junction 与挂载点卷带 reparse point 标记 (同卷时设备号不变), 其他平台的 stat 没有这两个字段.
_REPARSE_POINT_ATTRIBUTE: int = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


class _BoundaryRefusal(Exception):
    """目标跨越设备或 reparse point 边界, 或者设备无法判定."""


@dataclass(frozen=True)
class DeletedFile:
    """已从磁盘移除的普通文件. 释放空间按 ``(dev, ino)`` 归并后计算."""

    dev: int
    ino: int
    size: int
    nlink: int


@dataclass
class DeleteOutcome:
    """单个目标的删除结果. ``changed`` 表示磁盘上已经不存在."""

    status: DeleteStatus
    error: str | None = None
    files: list[DeletedFile] = field(default_factory=list)


@dataclass
class DeleteTally:
    """一批目标的结果计数.

    释放空间按 inode 归并: 只有当同一 inode 在执行集合内的名字数等于链接数时,
    删除才会真正释放数据 — 硬链接整理的源文件在库外, 不会被收进集合.
    """

    deleted: int = 0
    changed: int = 0
    failed: int = 0
    _groups: dict[tuple[int, int], list[DeletedFile]] = field(default_factory=dict, repr=False)

    def record(self, outcome: DeleteOutcome) -> None:
        match outcome.status:
            case "deleted":
                self.deleted += 1
            case "changed":
                self.changed += 1
            case "failed":
                self.failed += 1
        for item in outcome.files:
            self._groups.setdefault((item.dev, item.ino), []).append(item)

    @property
    def freed_bytes(self) -> int:
        """链接数不可用 (平台给 0) 时按 1 计: 宁可把可能未释放的数据算进来, 也不要恒为 0."""
        return sum(group[0].size for group in self._groups.values() if len(group) >= _links(group[0]))

    @property
    def hardlink_items(self) -> int:
        return sum(len(group) for group in self._groups.values() if len(group) != _links(group[0]))


@dataclass(frozen=True)
class PruneResult:
    removed: int = 0
    failed: int = 0


@in_thread
def delete_target(path: Path, *, library_root: Path, bases: Sequence[Path] = ()) -> DeleteOutcome:
    """删除一个目标 (文件、符号链接或目录).

    授权范围是库根与调用方给出的 ``bases`` (成功目录: 成品可以落在库根之外), 范围外的一切一律
    拒绝; 这些根自身与 ``.amane_trash`` 自身同样拒绝.
    """
    probe = disk_probe(path, follow_symlinks=False)
    if probe.unreadable:
        # 挂载抖动 / 权限变更不等于「文件没了」: 报失败让人重试, 不能回「已变更」假装删掉了.
        return DeleteOutcome(status="failed", error=f"path unreadable: {path}")
    disk_path = probe.path
    if disk_path is None:
        return DeleteOutcome(status="changed")

    refusal = _refuse_reason(disk_path, library_root=library_root, bases=bases)
    if refusal is not None:
        logger.warning("delete refused", path=str(disk_path), reason=refusal)
        return DeleteOutcome(status="failed", error=refusal)

    # 符号链接按字面路径删除链接自身, 真实路径在哪里都不影响; 其余目标按真实路径复核一次.
    is_link = disk_path.is_symlink()
    if not is_link:
        resolved_refusal = _resolved_refusal(disk_path, library_root=library_root, bases=bases)
        if resolved_refusal is not None:
            logger.warning("delete refused", path=str(disk_path), reason=resolved_refusal)
            return DeleteOutcome(status="failed", error=resolved_refusal)

    files: list[DeletedFile] = []
    try:
        # 先判符号链接: 断链链接正是要删除的对象, 而 is_dir 会跟随目标.
        if is_link:
            disk_path.unlink()
        elif disk_path.is_dir():
            _remove_tree(disk_path, files)
        else:
            _remove_file(disk_path, files)
    except (OSError, _BoundaryRefusal) as exc:
        logger.warning("delete failed", path=str(disk_path), error=str(exc))
        return DeleteOutcome(status="failed", error=str(exc), files=files)
    return DeleteOutcome(status="deleted", files=files)


def _links(item: DeletedFile) -> int:
    return max(item.nlink, 1)


def ancestor_dirs(path: Path, *, library_root: Path, bases: Sequence[Path] = ()) -> list[Path]:
    """``path`` 到「最近的授权根」之间的祖先目录, 由深到浅.

    不含根本身与 ``.amane_trash``; 传入路径不在任何授权根之下时返回空列表 ——
    剪枝只在授权范围内进行, 库外目录不在清单的授权范围内.

    ``bases`` 是库根之外的授权根 (刮削的输入目录、成品目录): 文件从那里搬进库根后,
    空掉的是那边的一层层目录, 只看库根会让它们永远留着.
    """
    roots = (library_root, *bases)
    if any(same_path(path, root) for root in roots):
        return []
    if not any(path_is_under(path, root) for root in roots):
        return []
    out: list[Path] = []
    for parent in Path(path).parents:
        if same_path(parent, library_root) or _is_recycle_dir(parent):
            break
        if not any(same_path(parent, root) for root in roots):
            out.append(parent)
            continue
        break
    return out


@in_thread
def prune_empty_dirs(directories: Iterable[Path], *, library_root: Path, bases: Sequence[Path] = ()) -> PruneResult:
    """自底向上删除已经空的目录, 只尝试传入的目录, 不向授权根扩展.

    只有 ``ENOTEMPTY`` 视为「非空」并跳过: 依赖目录删除对非空目录报错来保证确实为空,
    权限、忙、只读等其余错误记入 ``failed``, 不当作「非空」静默停下.
    ``bases`` 与 :func:`ancestor_dirs` 同义: 库根之外还允许剪的目录.
    """
    removed = 0
    failed = 0
    roots = (library_root, *bases)
    candidates = sorted({Path(directory) for directory in directories}, key=_depth, reverse=True)
    for directory in candidates:
        if any(same_path(directory, root) for root in roots) or _is_recycle_dir(directory):
            continue
        if not any(path_is_under(directory, root) for root in roots):
            continue
        try:
            directory.rmdir()
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                continue
            failed += 1
            logger.warning("prune failed", path=str(directory), error=str(exc))
            continue
        removed += 1
    return PruneResult(removed=removed, failed=failed)


def _depth(path: Path) -> int:
    return len(path.parts)


def _is_wash_dir(path: Path) -> bool:
    """是否落在洗版回收站里: ``<库根父目录>/.amane_wash/<库名>/...``.

    回收站挂在库根**父目录**下, 因此不能要求路径落在库根之内 —— 那样常规布局永远判不出来,
    直指回收站内某一项时会把可还原的备份当真文件删掉. 按目录名拦, 该名字是保留的.
    """
    return WASH_TRASH_DIRNAME in path.parts


def _is_recycle_dir(path: Path) -> bool:
    """是否是回收站目录本身 (``.amane_trash`` 或 ``.amane_wash``).

    洗版的回收站挂在库根父目录 (``.amane_wash/<库名>``), 嵌套库的布局下它会落进外层库,
    因此按目录名拦, 不只看库根直属的那一个. 剪枝与祖先列表都到此为止.
    """
    return path.name in (TRASH_DIRNAME, WASH_TRASH_DIRNAME)


def _remove_file(path: Path, files: list[DeletedFile]) -> None:
    st = path.lstat()
    path.unlink()
    files.append(DeletedFile(dev=st.st_dev, ino=st.st_ino, size=st.st_size, nlink=st.st_nlink))


def _remove_tree(target: Path, files: list[DeletedFile]) -> None:
    """删除目录内容后删除目录本身; 不跟随符号链接, 不跨越挂载点边界."""
    target_stat = target.lstat()
    parent_stat = target.parent.lstat()
    if _crosses_boundary(target_stat, dev=parent_stat.st_dev):
        raise _BoundaryRefusal(f"target is a mount point: {target}")
    _remove_contents(target, dev=target_stat.st_dev, files=files)
    target.rmdir()


def _remove_contents(directory: Path, *, dev: int, files: list[DeletedFile]) -> None:
    with os.scandir(directory) as entries:
        for entry in entries:
            path = Path(entry.path)
            # 真正的文件查询: 目录枚举顺带给出的统计在 Windows 上不含设备 / 文件编号 / 链接数.
            entry_stat = path.lstat()
            if stat.S_ISLNK(entry_stat.st_mode):
                path.unlink()
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                if entry.name in (TRASH_DIRNAME, WASH_TRASH_DIRNAME):
                    # 两个回收站都还能还原, 展开进来就等于「清空回收站」.
                    raise _BoundaryRefusal(f"refuse to delete trash subtree: {path}")
                if _crosses_boundary(entry_stat, dev=dev):
                    raise _BoundaryRefusal(f"mount boundary crossed: {path}")
                _remove_contents(path, dev=dev, files=files)
                path.rmdir()
                continue
            path.unlink()
            files.append(
                DeletedFile(
                    dev=entry_stat.st_dev,
                    ino=entry_stat.st_ino,
                    size=entry_stat.st_size,
                    nlink=entry_stat.st_nlink,
                )
            )


def _is_reparse_point(st: os.stat_result) -> bool:
    attributes: int = getattr(st, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT_ATTRIBUTE)


def _crosses_boundary(st: os.stat_result, *, dev: int) -> bool:
    """子项是否跨越设备或 reparse point 边界. 挂载盘断连时 lstat 先抛错, 同样记为失败."""
    return st.st_dev != dev or _is_reparse_point(st)


def _refuse_reason(target: Path, *, library_root: Path, bases: Sequence[Path] = ()) -> str | None:
    if same_path(target, library_root):
        return f"refuse to delete library root: {target}"
    if same_path(target, library_root / TRASH_DIRNAME):
        return f"refuse to delete trash root: {target}"
    if _is_wash_dir(target):
        # 洗版回收站还在可还原窗口里, 删掉会让 wash_records 指向不存在的文件.
        return f"refuse to delete wash trash: {target}"
    if any(same_path(target, base) for base in bases):
        # 与库根同理: 删掉成品目录自身等于删掉整批成品, 而清单里的目标是其中若干条目.
        return f"refuse to delete publish root: {target}"
    if is_under_any(target, (library_root, *bases)):
        return None
    return f"outside allowed roots: {target}"


def _resolved_refusal(target: Path, *, library_root: Path, bases: Sequence[Path] = ()) -> str | None:
    """目标的真实路径是否落在某个授权根的真实路径内.

    字面路径在授权范围内、真实路径在范围外只可能来自路径中的目录链接: 它把路径引到别处, 而字面
    比较看不出这一点. 两侧都取真实路径比较, 库路径自身是别名时同样成立.
    """
    real_target = resolved_path(target)
    if any(path_is_under(real_target, resolved_path(root)) for root in (library_root, *bases)):
        return None
    return f"resolved path outside allowed roots: {target} -> {real_target}"


def same_path(left: Path, right: Path) -> bool:
    """字面路径是否相等. ``path_is_under`` 已比较 NFC、大小写与归一, 双向包含即相等."""
    return path_is_under(left, right) and path_is_under(right, left)
