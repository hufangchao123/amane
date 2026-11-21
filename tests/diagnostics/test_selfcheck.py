"""体检判据: 只覆盖「当前配置下一定出问题」的项, 未启用的功能一律通过."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from amane.backup.manager import _stamp
from amane.config.manager import ColdSettings, HotSettings
from amane.diagnostics import SelfCheck

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
    assert "source_dir" in item.detail

    complete = HotSettings.model_validate(
        {"transfer": {"enabled": True, "cookies": "c=1", "source_dir": "/a", "target_dir": "/b"}}
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
