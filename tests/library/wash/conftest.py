"""洗版用例的夹具: 造一个带文件的媒体库, 给出默认配置."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from amane.config.manager import WashConfig
from amane.handlers._common import LibraryTaskLocks

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from amane.db.models import Library
    from amane.db.repository import Repository


@dataclass
class WashLibrary:
    """一个库根与它的索引行; ``add`` 同时落盘文件与登记 ``MediaFile``."""

    repo: Repository
    library: Library
    root: Path

    async def add(
        self,
        relative: str,
        *,
        size: int = 4096,
        number: str | None = None,
        duration: float | None = None,
        path: str | None = None,
    ) -> Path:
        """在库内放一份文件并登记索引.

        番号默认不写进索引, 走 ``collect`` 从文件名解析的那条路 —— 与线上主流程一致;
        ``path`` 用于把文件放到库根之外 (造非法输入).
        """
        target = Path(path) if path is not None else self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
        assert self.library.id is not None
        await self.repo.create_media_file(
            self.library.id, path=str(target), number=number, size=size, duration=duration
        )
        return target


@pytest_asyncio.fixture
async def wash_repo(repo: Repository) -> AsyncGenerator[Repository]:
    """``wash_records`` 已由 alembic 迁移建表, 夹具只做转手."""
    yield repo


@pytest_asyncio.fixture
async def wash_lib(wash_repo: Repository, tmp_path: Path) -> WashLibrary:
    """一个空的媒体库; 库根在 ``tmp_path/lib``, 回收站因此落在 ``tmp_path/.amane_wash``."""
    root = tmp_path / "lib"
    root.mkdir()
    library = await wash_repo.create_library(name="wash", path=str(root), write_nfo=False)
    return WashLibrary(repo=wash_repo, library=library, root=root)


@pytest.fixture
def wash_config() -> WashConfig:
    """默认开着的洗版配置: ``min_size_mb=0`` 让 4 KB 的用例文件也参与比较."""
    return WashConfig(enabled=True, min_size_mb=0, keep_days=7)


@pytest.fixture
def locks() -> LibraryTaskLocks:
    """与任务同一份锁表; 用例里只用到 ``get``."""
    return LibraryTaskLocks()
