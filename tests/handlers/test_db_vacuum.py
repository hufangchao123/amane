"""``db_vacuum`` 任务: 校验结果字段与「文件不存在」的失败路径.

真库里 VACUUM 的主要收益是消除碎片, 而碎片是否消除无法在测试里可靠断言 (取决于文件系统),
所以这里只验: 页数不增、空闲页被回收、数据仍在、耗时与结果类型正确.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from amane.db.models import TaskType
from amane.handlers.db_vacuum import DbVacuumHandler, vacuum_database
from amane.handlers.models import DbVacuumPayload, DbVacuumResult


def _make_fragmented_db(path: Path, *, rows: int = 400) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE blob (id INTEGER PRIMARY KEY, payload BLOB)")
        connection.executemany("INSERT INTO blob (payload) VALUES (?)", [(b"x" * 2048,) for _ in range(rows)])
        connection.commit()
        # 删掉大部分行: 留下大量空闲页, VACUUM 会把它们回收.
        connection.execute("DELETE FROM blob WHERE id % 10 != 0")
        connection.commit()
    finally:
        connection.close()


def test_vacuum_reclaims_free_pages_and_keeps_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "amane.db"
    _make_fragmented_db(db_path)

    result = vacuum_database(db_path)

    assert isinstance(result, DbVacuumResult)
    assert result.type is TaskType.DB_VACUUM
    assert result.free_pages_before > 0
    assert result.pages_after <= result.pages_before
    assert result.after_bytes <= result.before_bytes
    assert result.duration_ms >= 0
    assert result.wal_checkpointed is True

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        assert connection.execute("SELECT count(*) FROM blob").fetchone() == (40,)
        assert connection.execute("PRAGMA freelist_count").fetchone() == (0,)
    finally:
        connection.close()


def test_vacuum_is_idempotent_on_a_clean_db(tmp_path: Path) -> None:
    db_path = tmp_path / "amane.db"
    _make_fragmented_db(db_path, rows=20)
    vacuum_database(db_path)

    second = vacuum_database(db_path)

    assert second.free_pages_before == 0
    assert second.pages_after == second.pages_before


@pytest.mark.asyncio
async def test_handler_reports_success(tmp_path: Path) -> None:
    db_path = tmp_path / "amane.db"
    _make_fragmented_db(db_path, rows=120)

    outcome = await DbVacuumHandler(db_path).handle(DbVacuumPayload())

    assert outcome.success is True
    assert isinstance(outcome.result, DbVacuumResult)
    assert outcome.result.pages_before > 0


@pytest.mark.asyncio
async def test_handler_fails_when_the_file_is_missing(tmp_path: Path) -> None:
    outcome = await DbVacuumHandler(tmp_path / "nope.db").handle(DbVacuumPayload())

    assert outcome.success is False
    assert outcome.error is not None
    assert "数据库文件不存在" in outcome.error


@pytest.mark.asyncio
async def test_handler_reports_broken_file(tmp_path: Path) -> None:
    db_path = tmp_path / "amane.db"
    db_path.write_bytes(b"not a database at all" * 100)

    outcome = await DbVacuumHandler(db_path).handle(DbVacuumPayload())

    assert outcome.success is False
    assert outcome.error is not None
    assert "数据库整理失败" in outcome.error
