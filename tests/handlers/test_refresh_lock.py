"""刷新与洗版/整理/删除共用库锁: 洗版搬到一半时刷新不能把文件判成"已消失"."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from amane.handlers._common import LibraryTaskLocks
from amane.handlers.models import RefreshPayload
from amane.handlers.refresh import RefreshHandler

if TYPE_CHECKING:
    from pathlib import Path

    from amane.db.models import Library
    from amane.db.repository import Repository


@pytest.mark.asyncio(loop_scope="function")
async def test_refresh_waits_for_the_library_lock(repo: Repository, tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    (root / "ABC-123.mp4").write_bytes(b"x")
    library: Library = await repo.create_library(name="t", path=str(root))
    assert library.id is not None
    locks = LibraryTaskLocks()
    handler = RefreshHandler(repo, library_locks=locks)

    async with await locks.get(library.id):
        task = asyncio.create_task(handler.handle(RefreshPayload(library_id=library.id, path=str(root))))
        await asyncio.sleep(0.05)
        assert not task.done(), "库锁被占用时刷新不应开始扫描"

    result = await task
    assert result.success is True
    assert result.result is not None
    assert result.result.added == 1
