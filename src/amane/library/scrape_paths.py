"""刮削后转移的三个目录的边界校验.

路由与内置助理共用这一份: 重叠会让扫描与搬运互相触发 —— 同一个文件被反复登记, 或被反复搬动.
函数返回中文说明而不是抛 HTTP 错误, 由调用方转成各自的口径 (422 或工具错误).
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["check_scrape_dirs"]


def _reject_overlap(path: Path, other: Path, *, field: str, other_name: str) -> str | None:
    if path == other or other in path.parents or path in other.parents:
        return f"{field}不能与{other_name}重叠: {path}"
    return None


def _outside_only(path: Path, root: Path, *, field: str, other_name: str) -> str | None:
    """目的目录可以等于上位目录 (等于就是留在原地), 但不能落在它下面, 也不能包含它.

    包含库根时成品落在库根旁边的同层目录, 那已经是库外: 转移把文件搬出库外, 之后扫描与清理
    都不认它, 因此按同一道门拒绝.
    """
    if path != root and root in path.parents:
        return f"{field}不能位于{other_name}之下: {path}"
    if path in root.parents:
        return f"{field}不能包含{other_name}: {path}"
    return None


def check_scrape_dirs(
    *,
    transfer: bool,
    input_dir: str | None,
    ok_dir: str | None,
    fail_dir: str | None,
    library_path: str,
) -> str | None:
    """三个目录都可空, 但要留一个; 返回第一处不合法之处, 全部合法时返回 None."""
    if not transfer:
        return None
    if not input_dir and not ok_dir and not fail_dir:
        return "开启刮削后转移时至少填一个目录"

    root = Path(library_path).resolve()
    source = Path(input_dir).resolve() if input_dir else None
    if source is not None:
        problem = _reject_overlap(source, root, field="输入目录", other_name="库根")
        if problem is not None:
            return problem

    targets: list[tuple[str, Path]] = []
    for field, raw in (("成功目录", ok_dir), ("失败目录", fail_dir)):
        if not raw:
            continue
        target = Path(raw).resolve()
        problem = _outside_only(target, root, field=field, other_name="库根")
        if problem is not None:
            return problem
        if source is not None:
            problem = _reject_overlap(target, source, field=field, other_name="输入目录")
            if problem is not None:
                return problem
        targets.append((field, target))

    if len(targets) == 2 and targets[0][1] == targets[1][1]:
        return "成功目录与失败目录不能相同"
    return None
