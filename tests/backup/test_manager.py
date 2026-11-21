"""备份产物: 在线备份的可读性、按时间戳分组的修剪、资源归档与未启用时的失败路径."""

from __future__ import annotations

import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from amane.backup import BackupManager
from amane.backup.manager import _stamp
from amane.config.manager import BackupConfig
from amane.handlers.backup import BackupHandler
from amane.handlers.models import BackupPayload, BackupResult

if TYPE_CHECKING:
    from pathlib import Path


def _make_db(path: Path, rows: int) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("create table t (id integer primary key, name text)")
        conn.executemany("insert into t (name) values (?)", [(f"row-{index}",) for index in range(rows)])
        conn.commit()


def _manager(tmp_path: Path, **overrides: object) -> BackupManager:
    config = BackupConfig(enabled=True, dir=str(tmp_path / "backups"), **overrides)  # type: ignore[arg-type]
    return BackupManager(config, tmp_path / "data")


@pytest.mark.asyncio(loop_scope="function")
async def test_db_backup_is_readable_and_listed(tmp_path: Path) -> None:
    """产物必须能作为数据库打开并读到原表; 列举按时间倒序."""
    data = tmp_path / "data"
    data.mkdir()
    _make_db(data / "amane.db", 3)
    _make_db(data / "embeddings.db", 1)

    manager = _manager(tmp_path)
    outcome = await manager.run("db")

    assert len(outcome.files) == 2
    assert outcome.bytes_written > 0
    with sqlite3.connect(manager.root / outcome.files[0]) as conn:
        assert conn.execute("select count(*) from t").fetchone() == (3,)

    entries = await manager.list_backups()
    assert {entry.kind for entry in entries} == {"db"}
    assert entries == sorted(entries, key=lambda entry: entry.created_at, reverse=True)


@pytest.mark.asyncio(loop_scope="function")
async def test_missing_embeddings_db_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    _make_db(tmp_path / "data" / "amane.db", 1)

    outcome = await _manager(tmp_path).run("db")

    assert len(outcome.files) == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_prune_keeps_newest_by_timestamp(tmp_path: Path) -> None:
    """份数按时间戳计算: 一次备份两个文件不能算两份."""
    data = tmp_path / "data"
    data.mkdir()
    _make_db(data / "amane.db", 1)
    _make_db(data / "embeddings.db", 1)
    manager = _manager(tmp_path, keep_db=2)
    manager.root.mkdir(parents=True)
    for days in (10, 5):
        stamp = _stamp(datetime.now(UTC) - timedelta(days=days))
        _make_db(manager.root / f"amane-db-{stamp}.sqlite", 1)
        _make_db(manager.root / f"embeddings-db-{stamp}.sqlite", 1)

    outcome = await manager.run("db")

    remaining = {entry.created_at for entry in await manager.list_backups()}
    assert len(remaining) == 2
    assert outcome.pruned == 2
    assert all((manager.root / name).exists() for name in outcome.files)


@pytest.mark.asyncio(loop_scope="function")
async def test_assets_archive_and_prune(tmp_path: Path) -> None:
    resources = tmp_path / "data" / "resources"
    resources.mkdir(parents=True)
    (resources / "poster.jpg").write_bytes(b"jpeg")
    manager = _manager(tmp_path, keep_assets=1)
    manager.root.mkdir(parents=True)
    old = _stamp(datetime.now(UTC) - timedelta(days=9))
    (manager.root / f"assets-{old}.tar.gz").write_bytes(b"stale")

    outcome = await manager.run("assets")

    assert len(outcome.files) == 1
    with tarfile.open(manager.root / outcome.files[0]) as archive:
        assert "resources/poster.jpg" in archive.getnames()
    assert not (manager.root / f"assets-{old}.tar.gz").exists()
    assert outcome.pruned == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_missing_assets_dir_yields_no_archive(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()

    outcome = await _manager(tmp_path).run("assets")

    assert outcome.files == []


@pytest.mark.asyncio(loop_scope="function")
async def test_foreign_files_are_ignored(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.root.mkdir(parents=True)
    (manager.root / "notes.txt").write_text("keep me", encoding="utf-8")

    assert await manager.list_backups() == []


@pytest.mark.asyncio(loop_scope="function")
async def test_disabled_backup_reports_error_instead_of_raising(tmp_path: Path) -> None:
    """未启用时任务失败并给出原因; 异常只会触发无意义的重试."""
    handler = BackupHandler(BackupManager(BackupConfig(enabled=False), tmp_path))

    result = await handler.handle(BackupPayload(kind="db"))

    assert result.success is False
    assert result.error == "备份未启用"


@pytest.mark.asyncio(loop_scope="function")
async def test_handler_returns_outcome(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    _make_db(tmp_path / "data" / "amane.db", 2)
    handler = BackupHandler(_manager(tmp_path))

    result = await handler.handle(BackupPayload(kind="db"))

    assert result.success is True
    assert isinstance(result.result, BackupResult)
    assert result.result and len(result.result.files) == 1
