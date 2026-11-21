"""115 中转的夹具: 带生命周期的测试 app."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from httpx2 import ASGITransport, AsyncClient

from amane.api.routes import API_PREFIX
from amane.config import HotSettings
from tests.api.conftest import make_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from fastapi import FastAPI

    from amane.db.repository import Repository


@pytest_asyncio.fixture
async def transfer_repo(repo: Repository) -> Repository:
    """``transfer_items`` 已由 alembic 迁移建表, 夹具只做转手."""
    return repo


@pytest_asyncio.fixture
async def ops_app(tmp_path: Path) -> AsyncGenerator[FastAPI]:
    """带 lifespan 的 app: 仓储与运行时要在启动时就位, 用例才能直接写数据."""
    app = make_app(HotSettings(), tmp_path / "data", tmp_path / "logs", tmp_path / "files")
    async with app.router.lifespan_context(app):
        yield app


@pytest_asyncio.fixture
async def ops_client(ops_app: FastAPI) -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=ops_app), base_url=f"http://test{API_PREFIX}/") as client:
        yield client


@pytest.fixture
def api_repo(ops_app: FastAPI) -> Repository:
    return ops_app.state.runtime.repo
