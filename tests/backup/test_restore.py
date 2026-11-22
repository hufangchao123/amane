"""离线恢复: 选备份、校验、留底、换库、清 WAL、资源归档.

用例全部跑在临时目录: 恢复是覆盖式操作, 只有把「坏备份不许上桌」「库还在被用就不动」这两条
写成断言, 才敢在真机上按 --yes.
"""

from __future__ import annotations

import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from amane.backup.manager import _stamp
from amane.backup.restore import (
    LATEST,
    RestoreError,
    build_plan,
    db_target_name,
    main,
    restore,
)


def _make_db(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t (v TEXT)")
        connection.execute("INSERT INTO t VALUES (?)", (value,))
    return path


def _read(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT v FROM t").fetchone()
    assert row is not None
    return str(row[0])


def _backup_name(prefix: str = "amane", *, minutes_ago: int = 0) -> str:
    moment = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return f"{prefix}-db-{_stamp(moment)}.sqlite"


def test_db_target_name_maps_each_db_and_rejects_others(tmp_path: Path) -> None:
    """备份名决定覆盖哪个库; 认不出来的名字只能中止, 不能落到某个默认库上."""
    assert db_target_name(Path(_backup_name())) == "amane.db"
    assert db_target_name(Path(_backup_name("embeddings"))) == "embeddings.db"
    with pytest.raises(RestoreError):
        db_target_name(Path("random.sqlite"))


def test_build_plan_prefers_main_db_and_maps_target(tmp_path: Path) -> None:
    """两个库的最新备份时间戳相同: 默认恢复主库, 且目标就是 data_dir 里那个文件."""
    backup_dir = tmp_path / "backups"
    _make_db(backup_dir / _backup_name(), "main")
    _make_db(backup_dir / _backup_name("embeddings"), "vec")

    plan = build_plan(backup_dir, tmp_path / "data")

    assert plan.db_source.name == _backup_name()
    assert plan.db_target == tmp_path / "data" / "amane.db"
    assert plan.assets_source is None


def test_restore_replaces_db_and_keeps_safety_copy(tmp_path: Path) -> None:
    """覆盖前先留底: 恢复错了还能把现库原样放回去."""
    data_dir = tmp_path / "data"
    _make_db(tmp_path / "backups" / _backup_name(), "from-backup")
    target = _make_db(data_dir / "amane.db", "current")

    report = restore(build_plan(tmp_path / "backups", data_dir))

    assert _read(target) == "from-backup"
    assert report.safety_copy is not None
    assert _read(report.safety_copy) == "current"
    assert not target.with_name("amane.db.restore-tmp").exists()


def test_restore_clears_stale_wal_side_files(tmp_path: Path) -> None:
    """旧库的 -wal / -shm 必须带走: 留着会被新库回放成损坏."""
    data_dir = tmp_path / "data"
    _make_db(tmp_path / "backups" / _backup_name(), "from-backup")
    target = _make_db(data_dir / "amane.db", "current")
    side = [target.with_name("amane.db-wal"), target.with_name("amane.db-shm")]
    for path in side:
        path.write_bytes(b"\x00")

    restore(build_plan(tmp_path / "backups", data_dir))

    assert [path for path in side if path.exists()] == []


def test_restore_rejects_corrupt_backup(tmp_path: Path) -> None:
    """坏备份不许覆盖唯一一份好数据: 校验失败要中止, 且目标库一个字节都不变."""
    data_dir = tmp_path / "data"
    backup = tmp_path / "backups" / _backup_name()
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"not a sqlite database")
    target = _make_db(data_dir / "amane.db", "current")

    with pytest.raises(RestoreError, match="SQLite"):
        restore(build_plan(tmp_path / "backups", data_dir))

    assert _read(target) == "current"


def test_restore_refuses_while_db_is_locked(tmp_path: Path) -> None:
    """库还被占着就中止: 覆盖正在用的库等于把服务写坏."""
    data_dir = tmp_path / "data"
    _make_db(tmp_path / "backups" / _backup_name(), "from-backup")
    target = _make_db(data_dir / "amane.db", "current")

    holder = sqlite3.connect(target, timeout=0, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(RestoreError, match="停服务"):
            restore(build_plan(tmp_path / "backups", data_dir))
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert _read(target) == "current"


def test_restore_assets_swaps_directory_and_keeps_old(tmp_path: Path) -> None:
    """资源归档解到临时目录再整体换上去; 原目录改名留底, 不删任何东西."""
    data_dir = tmp_path / "data"
    _make_db(tmp_path / "backups" / _backup_name(), "from-backup")
    staged = tmp_path / "stage" / "resources"
    staged.mkdir(parents=True)
    (staged / "new.bin").write_bytes(b"new")
    archive = tmp_path / "backups" / f"assets-{_stamp(datetime.now(UTC))}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(staged, arcname="resources")
    (data_dir / "resources").mkdir(parents=True)
    (data_dir / "resources" / "old.bin").write_bytes(b"old")

    report = restore(build_plan(tmp_path / "backups", data_dir, assets=LATEST))

    resources = data_dir / "resources"
    assert sorted(path.name for path in resources.iterdir()) == ["new.bin"]
    assert report.assets_backup is not None
    assert (report.assets_backup / "old.bin").exists()


def test_main_refuses_to_overwrite_without_confirmation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """没给 --yes 时只拒绝: 覆盖是破坏性动作, 默认不答应."""
    data_dir = tmp_path / "data"
    _make_db(data_dir / "backups" / _backup_name(), "from-backup")
    _make_db(data_dir / "amane.db", "current")

    code = main(["--data-dir", str(data_dir)])

    assert code == 1
    assert "--yes" in capsys.readouterr().err
    assert _read(data_dir / "amane.db") == "current"


def test_main_dry_run_prints_plan_and_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--dry-run 只读: 计划打印出来, 目标库与目录内容都不变."""
    data_dir = tmp_path / "data"
    _make_db(data_dir / "backups" / _backup_name(), "from-backup")
    target = _make_db(data_dir / "amane.db", "current")

    code = main(["--data-dir", str(data_dir), "--dry-run"])

    assert code == 0
    assert "将覆盖" in capsys.readouterr().out
    assert _read(target) == "current"
    assert not [path for path in data_dir.iterdir() if "pre-restore" in path.name]
