"""TRASH_PURGE handler: 默认只统计, 关掉 dry_run 才真删; 未知库直接失败."""

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from amane.handlers import TrashPurgeHandler, TrashPurgePayload
from amane.handlers._common import LibraryTaskLocks
from amane.library.rules import TRASH_DIRNAME

if TYPE_CHECKING:
    from amane.db.repository import Repository


async def _library_with_trash(repo: Repository, root: Path) -> int:
    """建库并放一份 60 天前的回收站文件."""
    root.mkdir(parents=True, exist_ok=True)
    library = await repo.create_library(name="日韩", path=str(root))
    assert library.id is not None
    stale = root / TRASH_DIRNAME / "dedupe" / "20260101" / "ABC-123.mp4"
    stale.parent.mkdir(parents=True)
    stale.write_text("x", encoding="utf-8")
    old = time.time() - 60 * 24 * 3600
    os.utime(stale, (old, old))
    return library.id


@pytest.mark.asyncio(loop_scope="function")
async def test_dry_run_reports_without_deleting(repo: Repository, tmp_path: Path) -> None:
    """默认 ``dry_run``: 报出将删除的数量, 文件仍在."""
    await _library_with_trash(repo, tmp_path / "media")
    result = await TrashPurgeHandler(repo, LibraryTaskLocks()).handle(TrashPurgePayload(keep_days=30))
    assert result.success
    assert result.result is not None
    assert (result.result.dry_run, result.result.purged, result.result.libraries) == (True, 1, 1)
    assert (tmp_path / "media" / TRASH_DIRNAME / "dedupe" / "20260101" / "ABC-123.mp4").exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_purge_deletes_when_dry_run_off(repo: Repository, tmp_path: Path) -> None:
    """关掉 ``dry_run`` 才真删, 并清掉腾空的子目录."""
    await _library_with_trash(repo, tmp_path / "media")
    handler = TrashPurgeHandler(repo, LibraryTaskLocks())
    result = await handler.handle(TrashPurgePayload(keep_days=30, dry_run=False))
    assert result.result is not None
    assert (result.result.dry_run, result.result.purged, result.result.bytes_freed) == (False, 1, 1)
    assert not (tmp_path / "media" / TRASH_DIRNAME / "dedupe").exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_unknown_library_fails(repo: Repository) -> None:
    """指定不存在的库时报错, 不去遍历别处."""
    result = await TrashPurgeHandler(repo, LibraryTaskLocks()).handle(TrashPurgePayload(library_id=4242))
    assert not result.success
    assert result.error == "媒体库 4242 不存在"


@pytest.mark.asyncio(loop_scope="function")
async def test_library_without_trash_is_skipped(repo: Repository, tmp_path: Path) -> None:
    """没有回收站的库不计入 ``libraries``: 多数库从未写过回收站."""
    root = tmp_path / "空库"
    root.mkdir()
    await repo.create_library(name="空库", path=str(root))
    result = await TrashPurgeHandler(repo, LibraryTaskLocks()).handle(TrashPurgePayload())
    assert result.result is not None
    assert (result.result.libraries, result.result.scanned) == (0, 0)
