"""备份产物: 在线备份的可读性、按时间戳分组的修剪、资源归档与未启用时的失败路径."""

from __future__ import annotations

import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from amane.backup import BackupManager
from amane.backup.manager import ROOT_MARKER, BackupRootError, _stamp
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


def _manager_at(tmp_path: Path, root: Path, **overrides: object) -> BackupManager:
    """指定备份根的管理器; ``data_dir`` 固定为 ``<tmp>/data``."""
    return BackupManager(BackupConfig(enabled=True, dir=str(root), **overrides), tmp_path / "data")  # type: ignore[arg-type]


@pytest.mark.asyncio(loop_scope="function")
async def test_root_equal_to_data_dir_is_rejected(tmp_path: Path) -> None:
    """把库目录本身当备份根等于把修剪目标指向库里, 动手前就拒绝."""
    (tmp_path / "data").mkdir()
    _make_db(tmp_path / "data" / "amane.db", 1)

    with pytest.raises(BackupRootError, match="data_dir"):
        await _manager_at(tmp_path, tmp_path / "data").run("db")


@pytest.mark.asyncio(loop_scope="function")
async def test_root_above_data_dir_is_rejected(tmp_path: Path) -> None:
    """上级目录同样不行: 修剪会波及 data_dir 旁边的任何目录."""
    (tmp_path / "data").mkdir()
    _make_db(tmp_path / "data" / "amane.db", 1)

    with pytest.raises(BackupRootError, match="data_dir"):
        await _manager_at(tmp_path, tmp_path).run("db")


@pytest.mark.asyncio(loop_scope="function")
async def test_external_root_with_foreign_files_needs_marker(tmp_path: Path) -> None:
    """外部目录里已有无关文件时停下来要标记, 不在这类目录里删任何东西."""
    (tmp_path / "data").mkdir()
    _make_db(tmp_path / "data" / "amane.db", 1)
    root = tmp_path / "elsewhere"
    root.mkdir()
    (root / "notes.txt").write_text("keep me", encoding="utf-8")
    stale = _stamp(datetime.now(UTC) - timedelta(days=30))
    (root / f"amane-db-{stale}.sqlite").write_bytes(b"stale")

    with pytest.raises(BackupRootError, match=ROOT_MARKER):
        await _manager_at(tmp_path, root).run("db")

    assert (root / "notes.txt").exists()
    assert (root / f"amane-db-{stale}.sqlite").exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_marker_grants_external_root(tmp_path: Path) -> None:
    """放了标记文件即视为声明归属, 该备份照常进行, 目录里其它文件不受影响."""
    (tmp_path / "data").mkdir()
    _make_db(tmp_path / "data" / "amane.db", 1)
    root = tmp_path / "elsewhere"
    root.mkdir()
    (root / "notes.txt").write_text("keep me", encoding="utf-8")
    (root / ROOT_MARKER).write_text("", encoding="utf-8")

    outcome = await _manager_at(tmp_path, root).run("db")

    assert len(outcome.files) == 1
    assert (root / "notes.txt").exists()
    assert (root / outcome.files[0]).exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_root_with_only_backup_products_is_allowed(tmp_path: Path) -> None:
    """历史备份目录只装自己的产物, 不需要额外标记就能继续用."""
    (tmp_path / "data").mkdir()
    _make_db(tmp_path / "data" / "amane.db", 1)
    root = tmp_path / "elsewhere"
    root.mkdir()
    stale = _stamp(datetime.now(UTC) - timedelta(days=30))
    _make_db(root / f"amane-db-{stale}.sqlite", 1)

    outcome = await _manager_at(tmp_path, root, keep_db=1).run("db")

    assert len(outcome.files) == 1
    assert not (root / f"amane-db-{stale}.sqlite").exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_handler_surfaces_root_error(tmp_path: Path) -> None:
    """边界校验失败走任务结果的 ``error``, 与其它备份失败同一出口."""
    (tmp_path / "data").mkdir()
    handler = BackupHandler(_manager_at(tmp_path, tmp_path / "data"))

    result = await handler.handle(BackupPayload(kind="db"))

    assert result.success is False
    assert result.error is not None and "data_dir" in result.error
