"""离线恢复: 把备份产物换回 ``data_dir``.

必须停服再跑: 服务运行时持有连接池与 WAL, 覆盖库文件会让正在跑的连接读到半个库. 脚本因此
先对目标库取排他锁, 取不到就中止并要求先停服 —— 这是唯一可靠的「还有没有人在用」判据, 单看
``-wal`` / ``-shm`` 存在与否都不够 (上次异常退出也会留下它们).

覆盖顺序刻意如此:

1. 先校验备份本身 (``PRAGMA integrity_check``), 坏备份绝不覆盖掉唯一一份好数据;
2. 取锁成功后才清掉目标库旁边的 ``-wal`` / ``-shm``: 它们属于旧库, 留着会被新库回放成损坏;
3. 先把现有库复制一份 ``*.pre-restore-<时间戳>``, 再走「临时文件 + ``os.replace``」换上去;
4. 资源归档默认不恢复 (它只是下载缓存, 丢了会自己补), 要恢复得显式给 ``--assets``.

用法::

    docker compose stop amane
    docker compose run --rm --no-deps amane python -m amane.backup.restore --dry-run
    docker compose run --rm --no-deps amane python -m amane.backup.restore --yes

没有停服前提的手工替代做法仍是 ``docs/dev/ops.md`` 里那三步, 本模块只是把其中「挑文件、校验、
备份现库、换文件」自动化.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import tarfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .manager import _ASSETS_NAME, _DB_NAME, ASSETS_DIR_NAME, DB_FILES, _parse_stamp, _stamp

if TYPE_CHECKING:
    from collections.abc import Generator

LATEST = "latest"
"""``--file`` / ``--assets`` 的取值: 取目录里时间戳最新的一份."""

NONE = "none"
"""``--assets`` 的取值: 不恢复资源归档."""


class RestoreError(RuntimeError):
    """恢复无法进行 (备份不可用、库仍被占用、参数矛盾)."""


@dataclass(frozen=True, slots=True)
class RestorePlan:
    """一次恢复要做的事; 只描述, 不动文件."""

    db_source: Path
    db_target: Path
    assets_source: Path | None = None
    assets_target: Path | None = None


@dataclass(slots=True)
class RestoreReport:
    """实际做过的步骤, 按发生顺序; ``dry_run`` 时只有计划那一步."""

    steps: list[str] = field(default_factory=list)
    safety_copy: Path | None = None
    assets_backup: Path | None = None
    dry_run: bool = False


def db_target_name(backup: Path) -> str:
    """备份文件名 → 它覆盖的库名. 认不出来就中止, 不猜."""
    match = _DB_NAME.match(backup.name)
    if match is None:
        raise RestoreError(f"不是数据库备份的文件名: {backup.name}")
    prefix = backup.name.split("-db-", 1)[0]
    target = f"{prefix}.db"
    if target not in DB_FILES:
        raise RestoreError(f"不认识的备份前缀 {prefix!r}: {backup.name}")
    return target


def _candidates(backup_dir: Path, pattern: re.Pattern[str]) -> list[Path]:
    if not backup_dir.is_dir():
        raise RestoreError(f"备份目录不存在: {backup_dir}")
    found = [path for path in backup_dir.iterdir() if path.is_file() and pattern.match(path.name)]
    if not found:
        raise RestoreError(f"备份目录里没有匹配 {pattern.pattern} 的产物: {backup_dir}")
    return found


def _latest(backup_dir: Path, pattern: re.Pattern[str], *, prefer: str = "") -> Path:
    """目录里时间戳最新的一份; ``prefer`` 用于在两个库并存时偏向主库."""
    found = _candidates(backup_dir, pattern)
    if prefer:
        preferred = [path for path in found if path.name.startswith(prefer)]
        found = preferred or found
    return max(found, key=lambda path: _parse_stamp(path.name) or datetime.min.replace(tzinfo=UTC))


def build_plan(
    backup_dir: Path,
    data_dir: Path,
    *,
    file: str = LATEST,
    assets: str = NONE,
) -> RestorePlan:
    """把命令行参数解析成计划: 选哪份备份、覆盖哪个库、要不要恢复资源."""
    db_source = _pick(backup_dir, file, _DB_NAME, prefer="amane-db-")
    target = data_dir / db_target_name(db_source)
    assets_source: Path | None = None
    if assets != NONE:
        assets_source = _pick(backup_dir, assets, _ASSETS_NAME)
    return RestorePlan(
        db_source=db_source,
        db_target=target,
        assets_source=assets_source,
        assets_target=data_dir / ASSETS_DIR_NAME if assets_source is not None else None,
    )


def _pick(backup_dir: Path, value: str, pattern: re.Pattern[str], *, prefer: str = "") -> Path:
    if value == LATEST:
        return _latest(backup_dir, pattern, prefer=prefer)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = backup_dir / candidate
    if not candidate.is_file():
        raise RestoreError(f"备份文件不存在: {candidate}")
    return candidate


def verify_backup(path: Path) -> None:
    """读前先确认它是个完整的 SQLite 库: 覆盖是单向的, 坏备份不该有机会上桌."""
    try:
        with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as connection:
            row = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as error:
        raise RestoreError(f"备份不是可用的 SQLite 库: {path}: {error}") from error
    status = "ok" if row is None else str(row[0])
    if status != "ok":
        raise RestoreError(f"备份完整性检查未通过 ({status}): {path}")


@contextmanager
def exclusive(db: Path) -> Generator[None]:
    """对目标库取排他锁; 取不到说明还有进程在用, 直接中止."""
    if not db.exists():
        yield
        return
    connection = sqlite3.connect(db, timeout=0, isolation_level=None)
    try:
        try:
            connection.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as error:
            raise RestoreError(f"{db} 仍被占用, 先停服务再恢复: {error}") from error
        try:
            yield
        finally:
            connection.execute("ROLLBACK")
    finally:
        connection.close()


def restore(plan: RestorePlan, *, dry_run: bool = False) -> RestoreReport:
    """按计划执行; 返回实际做过的步骤, 便于运维留痕."""
    verify_backup(plan.db_source)
    report = RestoreReport(dry_run=dry_run)
    report.steps.append(f"备份校验通过: {plan.db_source}")
    if dry_run:
        report.steps.append(f"将覆盖 {plan.db_target}")
        if plan.assets_source is not None:
            report.steps.append(f"将恢复资源目录 {plan.assets_target} ← {plan.assets_source}")
        return report

    plan.db_target.parent.mkdir(parents=True, exist_ok=True)
    with exclusive(plan.db_target):
        _drop_side_files(plan.db_target)
        if plan.db_target.exists():
            report.safety_copy = _safety_copy(plan.db_target)
            report.steps.append(f"已保留现库: {report.safety_copy}")
        _install(plan.db_source, plan.db_target)
        report.steps.append(f"已覆盖: {plan.db_target}")
    if plan.assets_source is not None and plan.assets_target is not None:
        report.assets_backup = _restore_assets(plan.assets_source, plan.assets_target)
        if report.assets_backup is not None:
            report.steps.append(f"已保留原资源目录: {report.assets_backup}")
        report.steps.append(f"已恢复资源目录: {plan.assets_target}")
    return report


def _drop_side_files(db: Path) -> None:
    """旧库的 WAL / SHM 必须一起清掉: 只换主库会让它们被回放成损坏."""
    for suffix in ("-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)


def _safety_copy(db: Path) -> Path:
    target = db.with_name(f"{db.name}.pre-restore-{_stamp(datetime.now(UTC))}")
    shutil.copyfile(db, target)
    return target


def _install(source: Path, target: Path) -> None:
    """先写临时文件再替换: 中途失败时目标库还是旧的完整版本."""
    staging = target.with_name(f"{target.name}.restore-tmp")
    shutil.copyfile(source, staging)
    staging.replace(target)


def _restore_assets(archive: Path, target: Path) -> Path | None:
    """解到临时目录再整体换上去; 原目录改名留底, 不删任何东西."""
    stamp = _stamp(datetime.now(UTC))
    staging = target.with_name(f"{target.name}.restore-tmp")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(staging, filter="data")
        inner = staging / target.name
        prepared = inner if inner.is_dir() else staging
        backup = target.with_name(f"{target.name}.pre-restore-{stamp}") if target.exists() else None
        if backup is not None:
            target.replace(backup)
        prepared.replace(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return backup


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="amane.backup.restore",
        description="离线恢复: 用备份覆盖 data_dir 里的库 (须先停服务)",
    )
    parser.add_argument("--data-dir", type=Path, default=None, help="AMANE_DATA_DIR 或 ./data")
    parser.add_argument("--from", dest="backup_dir", type=Path, default=None, help="备份目录, 默认 <data-dir>/backups")
    parser.add_argument("--file", default=LATEST, help=f"数据库备份文件名, 默认 {LATEST}")
    parser.add_argument("--assets", default=NONE, help=f"资源归档文件名 / {LATEST} / {NONE}")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划, 不动文件")
    parser.add_argument("--yes", action="store_true", help="确认覆盖目标库")
    args = parser.parse_args(argv)

    data_dir = (args.data_dir or Path(os.environ.get("AMANE_DATA_DIR") or "./data")).expanduser()
    backup_dir = args.backup_dir or data_dir / "backups"
    try:
        plan = build_plan(backup_dir, data_dir, file=args.file, assets=args.assets)
    except RestoreError as error:
        print(f"恢复中止: {error}", file=sys.stderr)  # noqa: T201
        return 1
    if plan.db_target.exists() and not (args.yes or args.dry_run):
        print(  # noqa: T201
            f"会覆盖现有库 {plan.db_target}; 确认停服并接受覆盖后加 --yes (先 --dry-run 可看计划)",
            file=sys.stderr,
        )
        return 1
    try:
        report = restore(plan, dry_run=args.dry_run)
    except (RestoreError, OSError) as error:
        print(f"恢复失败: {error}", file=sys.stderr)  # noqa: T201
        return 1
    for step in report.steps:
        print(step)  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
