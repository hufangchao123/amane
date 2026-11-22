"""体检判据: 只覆盖「当前配置下一定出问题」的项, 未启用的功能一律通过."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from amane.backup.manager import ROOT_MARKER, _stamp
from amane.config.manager import ColdSettings, HotSettings
from amane.db import TaskStatus, TaskType
from amane.diagnostics import SelfCheck, selfcheck

if TYPE_CHECKING:
    from pathlib import Path

    from amane.db.repository import Repository


def _cold(tmp_path: Path, **overrides: object) -> ColdSettings:
    """显式给出关键字段, 不继承其它测试留在进程环境里的 ``AMANE_*`` 变量."""
    kwargs: dict[str, object] = {
        "data_dir": tmp_path,
        "_env_file": None,
        "token": None,
        "image_token": None,
        "safe_dirs": None,
    }
    kwargs.update(overrides)
    return ColdSettings(**kwargs)  # type: ignore[arg-type]


async def _report(tmp_path: Path, repo: Repository, hot: HotSettings | None = None, **cold: Any) -> SelfCheck:
    return SelfCheck(_cold(tmp_path, **cold), hot or HotSettings(), repo)


async def _keys(tmp_path: Path, repo: Repository, **cold: Any) -> dict[str, bool]:
    report = await (await _report(tmp_path, repo, **cold)).run()
    return {item.key: item.ok for item in report.items}


@pytest.mark.asyncio(loop_scope="function")
async def test_default_config_passes(tmp_path: Path, repo: Repository) -> None:
    """默认配置下没有失败项: 五个功能都关着, 不该出现故障."""
    report = await (await _report(tmp_path, repo)).run()

    assert report.failed == []
    assert {item.key for item in report.items} >= {"data_dir", "disk", "db", "libraries", "token", "queue"}


@pytest.mark.asyncio(loop_scope="function")
async def test_missing_data_dir_fails(tmp_path: Path, repo: Repository) -> None:
    state = await _keys(tmp_path / "absent", repo)

    assert state["data_dir"] is False


@pytest.mark.asyncio(loop_scope="function")
async def test_token_state_is_informational(tmp_path: Path, repo: Repository) -> None:
    """显式关闭鉴权是部署选择, 不影响功能, 因此只陈述状态而不计入失败项."""
    report = await (await _report(tmp_path, repo)).run()
    assert next(item for item in report.items if item.key == "token").detail == "接口鉴权已启用"

    report = await (await _report(tmp_path, repo, token="off")).run()
    token = next(item for item in report.items if item.key == "token")
    assert token.ok is True
    assert "已显式关闭" in token.detail


@pytest.mark.asyncio(loop_scope="function")
async def test_missing_library_path_fails(tmp_path: Path, repo: Repository) -> None:
    await repo.create_library(name="gone", path=str(tmp_path / "no-such-dir"))

    state = await _keys(tmp_path, repo)
    assert state["libraries"] is False


@pytest.mark.asyncio(loop_scope="function")
async def test_transfer_requires_paths_and_cookies(tmp_path: Path, repo: Repository) -> None:
    """启用但缺配置时报告缺项, 而不是让任务运行到一半失败."""
    partial = HotSettings.model_validate({"transfer": {"enabled": True, "cookies": "c=1"}})
    report = await (await _report(tmp_path, repo, partial)).run()
    item = next(entry for entry in report.items if entry.key == "transfer")

    assert item.ok is False
    assert "source_cid" in item.detail

    complete = HotSettings.model_validate(
        {"transfer": {"enabled": True, "cookies": "c=1", "source_cid": "100", "target_cid": "5000"}}
    )
    report = await (await _report(tmp_path, repo, complete)).run()
    assert next(entry for entry in report.items if entry.key == "transfer").ok is True


@pytest.mark.asyncio(loop_scope="function")
async def test_wash_requires_a_kind(tmp_path: Path, repo: Repository) -> None:
    hot = HotSettings.model_validate({"wash": {"enabled": True, "local": False, "strm": False}})

    report = await (await _report(tmp_path, repo, hot)).run()
    assert next(entry for entry in report.items if entry.key == "wash").ok is False


@pytest.mark.asyncio(loop_scope="function")
async def test_backup_freshness(tmp_path: Path, repo: Repository) -> None:
    """启用备份却长期没有产物等同于没有备份; 新鲜的产物通过."""
    hot = HotSettings.model_validate({"backup": {"enabled": True}})
    check = await _report(tmp_path, repo, hot)

    assert next(entry for entry in (await check.run()).items if entry.key == "backup").ok is False

    root = tmp_path / "backups"
    root.mkdir()
    fresh = root / f"amane-db-{_stamp(datetime.now(UTC))}.sqlite"
    sqlite3.connect(fresh).close()
    assert next(entry for entry in (await check.run()).items if entry.key == "backup").ok is True

    stale = root / f"amane-db-{_stamp(datetime.now(UTC) - timedelta(days=30))}.sqlite"
    sqlite3.connect(stale).close()
    fresh.unlink()
    assert next(entry for entry in (await check.run()).items if entry.key == "backup").ok is False


@pytest.mark.asyncio(loop_scope="function")
async def test_disk_free_space_is_graded(tmp_path: Path, repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    """三级水位: 告急判失败, 偏低只写在 detail 里, 够用什么都不提."""
    item_by_free: dict[str, Any] = {"detail": ""}

    def fake_usage(path: Path) -> SimpleNamespace:
        return SimpleNamespace(total=0, used=0, free=item_by_free["free"])

    monkeypatch.setattr(selfcheck.shutil, "disk_usage", fake_usage)

    async def disk_item(free: int) -> Any:
        item_by_free["free"] = free
        report = await (await _report(tmp_path, repo)).run()
        return next(item for item in report.items if item.key == "disk")

    critical = await disk_item(512 << 20)
    assert critical.ok is False
    assert "告急" in critical.detail

    low = await disk_item(4 << 30)
    assert low.ok is True
    assert "偏低" in low.detail

    plenty = await disk_item(50 << 30)
    assert plenty.ok is True
    assert "偏低" not in plenty.detail and "告急" not in plenty.detail


@pytest.mark.asyncio(loop_scope="function")
async def test_backup_age_is_graded_and_assets_reported(tmp_path: Path, repo: Repository) -> None:
    """备份偏旧不判失败但要提示; 资源归档的年龄单列, 缺了也只是说明一句."""
    root = tmp_path / "backups"
    root.mkdir()
    sqlite3.connect(root / f"amane-db-{_stamp(datetime.now(UTC) - timedelta(days=5))}.sqlite").close()
    hot = HotSettings.model_validate({"backup": {"enabled": True}})
    check = await _report(tmp_path, repo, hot)

    stale_but_ok = next(item for item in (await check.run()).items if item.key == "backup")
    assert stale_but_ok.ok is True
    assert "偏旧" in stale_but_ok.detail
    assert "无资源归档" in stale_but_ok.detail

    import tarfile

    archive = root / f"assets-{_stamp(datetime.now(UTC))}.tar.gz"
    with tarfile.open(archive, "w:gz"):
        pass
    with_assets = next(item for item in (await check.run()).items if item.key == "backup")
    assert "资源归档 0 天前" in with_assets.detail


@pytest.mark.asyncio(loop_scope="function")
async def test_backup_root_outside_data_dir_needs_marker(tmp_path: Path, repo: Repository) -> None:
    """备份根指到别人的目录: 体检直接说出「要标记文件」, 不等定时任务每轮失败."""
    foreign = tmp_path.parent / "elsewhere"
    foreign.mkdir()
    (foreign / "movie.mkv").write_bytes(b"x")
    hot = HotSettings.model_validate({"backup": {"enabled": True, "dir": str(foreign)}})
    item = next(item for item in (await (await _report(tmp_path, repo, hot)).run()).items if item.key == "backup")

    assert item.ok is False
    assert ROOT_MARKER in item.detail

    (foreign / ROOT_MARKER).write_text("", encoding="utf-8")
    marked = next(item for item in (await (await _report(tmp_path, repo, hot)).run()).items if item.key == "backup")
    assert "尚无数据库备份" in marked.detail


@pytest.mark.asyncio(loop_scope="function")
async def test_queue_reports_oldest_wait_and_stalled_worker(tmp_path: Path, repo: Repository) -> None:
    """有任务排队却没人执行: 最久等待时长越过阈值就判失败, 否则只是把时长报出来."""
    check = await _report(tmp_path, repo)

    empty = next(item for item in (await check.run()).items if item.key == "queue")
    assert empty.ok is True
    assert "最久一条" not in empty.detail

    task = await repo.create_task(TaskType.CLEANUP, {})
    queued_at = datetime.now(UTC) - timedelta(minutes=45)

    async def _set(status: Any, created_at: datetime | None = None) -> None:
        async with repo._session() as session:  # 造「等了很久」或「已收工」的既成事实
            row = await session.get(type(task), task.id)
            assert row is not None
            row.status = status
            if created_at is not None:
                row.created_at = created_at
            session.add(row)
            await session.commit()

    await _set(TaskStatus.QUEUED, queued_at)

    stalled = next(item for item in (await check.run()).items if item.key == "queue")
    assert stalled.ok is False
    assert "worker 可能无响应" in stalled.detail

    await _set(TaskStatus.QUEUED, datetime.now(UTC))
    waiting = next(item for item in (await check.run()).items if item.key == "queue")
    assert waiting.ok is True
    assert "最久一条已等待 0 分钟" in waiting.detail

    await _set(TaskStatus.DONE)
