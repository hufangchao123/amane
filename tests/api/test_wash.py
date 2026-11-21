"""洗版 HTTP 接口: 状态卡、候选与回收站两张列表、两个入队动作.

动作只入队不等待 (202): 搬文件的活由任务承担, 这里只验「入口校验 + 入队载荷」.
非法输入一律 422, 不允许 409 —— 冲突码会让前端以为要用户手动解决.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from amane.api.routes import API_PREFIX
from amane.config.manager import HotSettings, WashConfig
from amane.db.models import WashRecord, WashRecordKind, WashRecordState
from tests.api.conftest import _SilentFeedHttp, make_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

    from amane.db.repository import Repository

LOCAL = WashRecordKind.LOCAL


def create_wash_table(db_path: Path) -> None:
    """``wash_records`` 的迁移还在整合会话那边, 这里按模型直接补建表.

    真实的 ``upgrade_sqlite_database`` 里没有这张表, 不补建就只能验到「表缺失」降级分支.
    """
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine, tables=[WashRecord.__table__])
    engine.dispose()


@pytest.fixture
def wash_db(app: FastAPI, tmp_path: Path) -> Path:
    """默认 (关闭洗版) 应用上的库, 已补建 ``wash_records``."""
    db_path = tmp_path / "data" / "amane.db"
    create_wash_table(db_path)
    return db_path


@pytest.fixture
def wash_app(tmp_path: Path) -> FastAPI:
    """洗版开启的应用; 门限压到 0 让 4 KB 的测试文件也能成组."""
    hot = HotSettings(wash=WashConfig(enabled=True, min_size_mb=0, keep_days=7))
    return make_app(hot, tmp_path / "wash-data", tmp_path / "wash-logs", tmp_path / "wash-files")


@pytest.fixture
def wash_open_db(wash_app: FastAPI, tmp_path: Path) -> Path:
    db_path = tmp_path / "wash-data" / "amane.db"
    create_wash_table(db_path)
    return db_path


@pytest_asyncio.fixture
async def wash_client(wash_app: FastAPI, wash_open_db: Path) -> AsyncIterator[AsyncClient]:
    ctx = wash_app.router.lifespan_context(wash_app)
    await ctx.__aenter__()
    if wash_app.state.runtime.feed_service is not None:
        wash_app.state.runtime.feed_service.set_web_client(_SilentFeedHttp())
    transport = ASGITransport(app=wash_app)
    async with AsyncClient(transport=transport, base_url=f"http://test{API_PREFIX}/") as client:
        yield client
    await ctx.__aexit__(None, None, None)


@pytest_asyncio.fixture
async def wash_repo(wash_app: FastAPI, wash_client: AsyncClient) -> Repository:
    return wash_app.state.runtime.repo


async def add_record(
    repo: Repository,
    *,
    removed: Path,
    size: int,
    state: WashRecordState = WashRecordState.TRASHED,
    expires_in: timedelta = timedelta(days=7),
    kind: WashRecordKind = LOCAL,
) -> int:
    record_id = await repo.add_wash_record(
        kind=kind,
        group_key="lib1:ABC-123",
        kept_path=str(removed.parent / "kept.mkv"),
        removed_path=str(removed),
        size_bytes=size,
        expires_at=datetime.now(UTC) + expires_in,
    )
    if state is not WashRecordState.TRASHED:
        await repo.set_wash_record_state([record_id], state)
    return record_id


@pytest.mark.asyncio(loop_scope="function")
async def test_status_shape(client: AsyncClient, wash_db: Path) -> None:
    """默认关闭: 三个数字都是 0, 但接口本身要活着."""
    resp = await client.get("wash/status")

    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "groups": 0, "trashed": 0, "restorable_bytes": 0}


@pytest.mark.asyncio(loop_scope="function")
async def test_status_reports_trash(client: AsyncClient, wash_db: Path, repo: Repository) -> None:
    """关闭洗版也要显示回收站内容 —— 它还需要被清理."""
    await add_record(repo, removed=Path(wash_db).parent / "t" / "a.mkv", size=1234)

    body = (await client.get("wash/status")).json()

    assert (body["enabled"], body["trashed"], body["restorable_bytes"]) == (False, 1, 1234)


@pytest.mark.asyncio(loop_scope="function")
async def test_status_enabled(wash_client: AsyncClient, wash_open_db: Path) -> None:
    """开启后 ``enabled`` 为真."""
    body = (await wash_client.get("wash/status")).json()

    assert body["enabled"] is True


@pytest.mark.asyncio(loop_scope="function")
async def test_candidates_disabled_returns_empty_page(client: AsyncClient, wash_db: Path) -> None:
    """停用时返回空页而不是报错: 页面照旧能打开."""
    resp = await client.get("wash/candidates")

    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


@pytest.mark.asyncio(loop_scope="function")
async def test_candidates_list_real_groups(
    wash_client: AsyncClient,
    wash_open_db: Path,
    wash_repo: Repository,
    tmp_path: Path,
) -> None:
    """候选来自索引 + 磁盘: 同番号两份列出一组."""
    root = tmp_path / "media"
    root.mkdir()
    (root / "ABC-123-1080p.mkv").write_bytes(b"x" * 3000)
    (root / "ABC-123-720p.mp4").write_bytes(b"x" * 1000)
    library = await wash_repo.create_library(name="media", path=str(root), write_nfo=False)
    assert library.id is not None
    for name in ("ABC-123-1080p.mkv", "ABC-123-720p.mp4"):
        await wash_repo.create_media_file(library.id, path=str(root / name), number="ABC-123")

    body = (await wash_client.get("wash/candidates", params={"kind": "local"})).json()

    assert body["total"] == 1
    (item,) = body["items"]
    assert item["group_key"] == f"lib{library.id}:ABC-123"
    assert item["keep_path"] == str(root / "ABC-123-1080p.mkv")
    assert item["drop_paths"] == [str(root / "ABC-123-720p.mp4")]
    assert item["bytes_freed"] == 1000


BAD_QUERY_CASES = [
    pytest.param("wash/candidates", {"limit": 0}, id="candidates-limit-zero"),
    pytest.param("wash/candidates", {"sort_by": "nope"}, id="candidates-bad-sort"),
    pytest.param("wash/candidates", {"kind": "both"}, id="candidates-bad-kind"),
    pytest.param("wash/records", {"offset": -1}, id="records-negative-offset"),
    pytest.param("wash/records", {"sort_by": "nope"}, id="records-bad-sort"),
]


@pytest.mark.parametrize(("url", "params"), BAD_QUERY_CASES)
@pytest.mark.asyncio(loop_scope="function")
async def test_bad_query_is_422(
    client: AsyncClient,
    wash_db: Path,
    url: str,
    params: dict[str, object],
) -> None:
    """查询参数非法时给 422, 不是 409."""
    resp = await client.get(url, params=params)

    assert resp.status_code == 422
    assert resp.status_code != 409


@pytest.mark.asyncio(loop_scope="function")
async def test_records_list_paging_and_filter(client: AsyncClient, wash_db: Path, repo: Repository) -> None:
    """回收站列表: 分页 + 状态过滤 + 体积排序."""
    base = Path(wash_db).parent
    await add_record(repo, removed=base / "t" / "small.mkv", size=10)
    await add_record(repo, removed=base / "t" / "big.mkv", size=20)
    await add_record(repo, removed=base / "t" / "done.mkv", size=30, state=WashRecordState.PURGED)

    body = (await client.get("wash/records", params={"sort_by": "size_bytes", "order": "desc"})).json()
    page = (await client.get("wash/records", params={"limit": 1, "offset": 1})).json()
    trashed = (await client.get("wash/records", params={"state": "trashed"})).json()

    assert body["total"] == 3
    assert [item["size_bytes"] for item in body["items"]] == [30, 20, 10]
    assert body["items"][0]["state"] == "purged"
    assert (page["total"], len(page["items"])) == (3, 1)
    assert trashed["total"] == 2


@pytest.mark.asyncio(loop_scope="function")
async def test_restore_enqueues_task(
    client: AsyncClient,
    wash_db: Path,
    repo: Repository,
    stop_worker: None,
) -> None:
    """还原入队 ``wash_apply``, 记录 id 用保留前缀携带."""
    record_id = await add_record(repo, removed=Path(wash_db).parent / "t" / "a.mkv", size=5)

    resp = await client.post("wash/records/restore", json={"ids": [record_id]})

    assert resp.status_code == 202
    body = resp.json()
    assert body["type"] == "wash_apply"
    assert body["payload"]["group_keys"] == [f"@restore:{record_id}"]


@pytest.mark.asyncio(loop_scope="function")
async def test_purge_enqueues_task(
    client: AsyncClient,
    wash_db: Path,
    repo: Repository,
    stop_worker: None,
) -> None:
    """清理同样入队, 是否到期由任务判定."""
    record_id = await add_record(repo, removed=Path(wash_db).parent / "t" / "a.mkv", size=5)

    resp = await client.post("wash/records/purge", json={"ids": [record_id]})

    assert resp.status_code == 202
    assert resp.json()["payload"]["group_keys"] == [f"@purge:{record_id}"]


BAD_ACTION_CASES = [
    pytest.param("wash/records/restore", {"ids": []}, id="restore-empty-ids"),
    pytest.param("wash/records/purge", {"ids": []}, id="purge-empty-ids"),
    pytest.param("wash/records/restore", {"ids": [9999]}, id="restore-unknown-id"),
    pytest.param("wash/records/purge", {"ids": [9999]}, id="purge-unknown-id"),
]


@pytest.mark.parametrize(("url", "body"), BAD_ACTION_CASES)
@pytest.mark.asyncio(loop_scope="function")
async def test_bad_action_is_422(
    client: AsyncClient,
    wash_db: Path,
    url: str,
    body: dict[str, object],
) -> None:
    """空 ids 与不存在的记录都是 422, 且绝不返回 409."""
    resp = await client.post(url, json=body)

    assert resp.status_code == 422
    assert resp.status_code != 409


@pytest.mark.asyncio(loop_scope="function")
async def test_action_rejects_wrong_state(
    client: AsyncClient,
    wash_db: Path,
    repo: Repository,
    stop_worker: None,
) -> None:
    """已还原的记录不能再还原一次: 状态不符同样 422, 并说清是哪一条."""
    record_id = await add_record(
        repo,
        removed=Path(wash_db).parent / "t" / "a.mkv",
        size=5,
        state=WashRecordState.RESTORED,
    )

    resp = await client.post("wash/records/restore", json={"ids": [record_id]})

    assert resp.status_code == 422
    assert str(record_id) in resp.json()["detail"]
