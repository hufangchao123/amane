"""刮削后转移的后继: 成功随完成的链, 失败由 worker 另起; 两处都按库配置决定.

fixture 无法表达的部分: 后继的 key / 优先级 / outcome, 缺库配置与缺文件时的降级.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from amane.config import ActorScrapingConfig, HotSettings, ScrapingConfig
from amane.crawlers.base import Crawler, CrawlerProfile
from amane.crawlers.factory import CrawlerFactory
from amane.crawlers.models import MediaMetadata
from amane.db.models import TaskType
from amane.enums import SiteName
from amane.handlers import FollowupTask, ScrapeHandler, ScrapePayload, ScrapeResult, TaskResult
from amane.handlers._common import register_media_file

if TYPE_CHECKING:
    from amane.db.repository import Repository


class _FakeCrawler(Crawler):
    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(name=SiteName.JAVDB, base_url="https://fake.example.com")

    def __init__(self, metadata: MediaMetadata) -> None:
        self._profile = self.profile()
        self.name = self._profile.name
        self._metadata = metadata

    async def _search(self, query: object, options: object = None) -> str | None:
        return "https://fake.example.com/v/1"

    async def _scrape(self, url: str, options: object = None) -> MediaMetadata | None:
        return self._metadata


def _handler(repo: Repository) -> ScrapeHandler:
    metadata = MediaMetadata.model_validate({"number": "MIDV-123", "title": "Test", "studio": "Studio X"})
    factory = AsyncMock(spec=CrawlerFactory)
    factory.get_crawlers.return_value = {"javdb": _FakeCrawler(metadata)}
    cfg = HotSettings(
        scraping=ScrapingConfig(field_priority={}),
        actor_scraping=ActorScrapingConfig(auto_scrape=False, download_images=False),
    )
    return ScrapeHandler(repo, factory, AsyncMock(), cfg)


async def _library_with_file(
    repo: Repository, tmp_path: Path, *, pending: Path | None, fail_dir: Path | None
) -> tuple[int, int]:
    lib_root, source = tmp_path / "done", pending if pending is not None else tmp_path / "lib"
    lib_root.mkdir(exist_ok=True)
    source.mkdir(exist_ok=True)
    lib = await repo.create_library(
        name="t",
        path=str(lib_root),
        scrape_transfer=pending is not None,
        scrape_input_dir=str(source) if pending is not None else None,
        scrape_fail_dir=str(fail_dir) if fail_dir is not None else None,
    )
    assert lib.id is not None
    media = await register_media_file(repo, lib.id, source / "incoming" / "MIDV-123.mp4")
    assert media.id is not None
    return lib.id, media.id


def _transfers(result: TaskResult[ScrapeResult]) -> list[FollowupTask]:
    return [f for f in result.followups if f.task_type == TaskType.SCRAPE_TRANSFER]


@pytest.mark.asyncio(loop_scope="function")
async def test_success_chains_transfer_into_library_root(repo: Repository, tmp_path: Path) -> None:
    """开关打开时成功的落点是库根, 成功刮削为其文件描述一条转移后继."""
    _, media_id = await _library_with_file(repo, tmp_path, pending=tmp_path / "in", fail_dir=None)

    result = await _handler(repo).handle(ScrapePayload(number="MIDV-123", media_file_id=media_id))

    assert result.success is True
    transfers = _transfers(result)
    assert len(transfers) == 1
    assert transfers[0].key == f"scrape-transfer:{media_id}"
    assert transfers[0].priority == -1
    assert transfers[0].payload["media_file_id"] == media_id
    assert transfers[0].payload["outcome"] == "ok"
    # 重试补搬附属文件时按这个目录找留下来的一份.
    assert transfers[0].payload["source_dir"] == str(tmp_path / "in" / "incoming")


@pytest.mark.asyncio(loop_scope="function")
async def test_success_without_switch_chains_nothing(repo: Repository, tmp_path: Path) -> None:
    """未开启时不搬."""
    _, media_id = await _library_with_file(repo, tmp_path, pending=None, fail_dir=tmp_path / "failed")

    result = await _handler(repo).handle(ScrapePayload(number="MIDV-123", media_file_id=media_id))

    assert result.success is True
    assert _transfers(result) == []


@pytest.mark.asyncio(loop_scope="function")
async def test_failure_followups_use_fail_dir(repo: Repository, tmp_path: Path) -> None:
    """刮削失败的后继由 worker 取得, outcome 是 failed."""
    _, media_id = await _library_with_file(repo, tmp_path, pending=tmp_path / "in", fail_dir=tmp_path / "failed")

    followups = await _handler(repo).failure_followups(ScrapePayload(number="MIDV-123", media_file_id=media_id))

    assert [f.task_type for f in followups] == [TaskType.SCRAPE_TRANSFER]
    assert followups[0].payload["media_file_id"] == media_id
    assert followups[0].payload["outcome"] == "failed"


@pytest.mark.asyncio(loop_scope="function")
async def test_failure_followups_empty_without_fail_dir(repo: Repository, tmp_path: Path) -> None:
    """没配失败目录时失败留在输入目录, 不描述后继."""
    _, media_id = await _library_with_file(repo, tmp_path, pending=tmp_path / "in", fail_dir=None)

    followups = await _handler(repo).failure_followups(ScrapePayload(number="MIDV-123", media_file_id=media_id))

    assert followups == []


@pytest.mark.asyncio(loop_scope="function")
async def test_failure_followups_empty_when_switch_off(repo: Repository, tmp_path: Path) -> None:
    _, media_id = await _library_with_file(repo, tmp_path, pending=None, fail_dir=None)

    followups = await _handler(repo).failure_followups(ScrapePayload(number="MIDV-123", media_file_id=media_id))

    assert followups == []


@pytest.mark.asyncio(loop_scope="function")
async def test_failure_followups_ignore_by_number_scrape(repo: Repository, tmp_path: Path) -> None:
    """按番号刮削没有对应文件, 失败也无可搬."""
    await _library_with_file(repo, tmp_path, pending=tmp_path / "in", fail_dir=tmp_path / "failed")

    followups = await _handler(repo).failure_followups(ScrapePayload(number="MIDV-123", media_file_id=None))

    assert followups == []


@pytest.mark.asyncio(loop_scope="function")
async def test_failure_followups_skip_deleted_file(repo: Repository, tmp_path: Path) -> None:
    """索引与任务不一致时静默跳过, 不让失败处理再多一层异常."""
    _, media_id = await _library_with_file(repo, tmp_path, pending=tmp_path / "in", fail_dir=tmp_path / "failed")
    await repo.delete_media_file(media_id)

    followups = await _handler(repo).failure_followups(ScrapePayload(number="MIDV-123", media_file_id=media_id))

    assert followups == []


@pytest.mark.asyncio(loop_scope="function")
async def test_transfer_replaces_organize(repo: Repository, tmp_path: Path) -> None:
    """同时开自动整理与转移时只排转移: 两条链争同一把库锁, 谁先赢决定文件最终在哪."""
    lib_root, pending = tmp_path / "done", tmp_path / "in"
    lib_root.mkdir()
    pending.mkdir()
    lib = await repo.create_library(
        name="t",
        path=str(lib_root),
        auto_organize=True,
        scrape_transfer=True,
        scrape_input_dir=str(pending),
    )
    assert lib.id is not None
    media = await register_media_file(repo, lib.id, pending / "MIDV-123" / "MIDV-123.mp4")
    assert media.id is not None

    result = await _handler(repo).handle(ScrapePayload(number="MIDV-123", media_file_id=media.id))

    types = [f.task_type for f in result.followups]
    assert types.count(TaskType.SCRAPE_TRANSFER) == 1
    assert TaskType.ORGANIZE not in types


@pytest.mark.asyncio(loop_scope="function")
async def test_rescrape_of_a_library_file_chains_no_transfer(repo: Repository, tmp_path: Path) -> None:
    """成品区里的文件再刮削时不搬: 落点等于库根时搬过去等于把整理好的目录拆成平铺."""
    lib_root, pending = tmp_path / "done", tmp_path / "in"
    lib_root.mkdir()
    pending.mkdir()
    lib = await repo.create_library(
        name="t",
        path=str(lib_root),
        scrape_transfer=True,
        scrape_input_dir=str(pending),
    )
    assert lib.id is not None
    media = await register_media_file(repo, lib.id, lib_root / "演员" / "MIDV-123" / "MIDV-123.mp4")
    assert media.id is not None

    result = await _handler(repo).handle(ScrapePayload(number="MIDV-123", media_file_id=media.id))

    assert result.success is True
    assert _transfers(result) == []


@pytest.mark.asyncio(loop_scope="function")
async def test_rescrape_inside_ok_dir_chains_no_transfer(repo: Repository, tmp_path: Path) -> None:
    """显式成功目录里也一样: 那是使用者排好的树."""
    lib_root, pending, ok_dir = tmp_path / "done", tmp_path / "in", tmp_path / "out"
    for directory in (lib_root, pending, ok_dir):
        directory.mkdir()
    lib = await repo.create_library(
        name="t",
        path=str(lib_root),
        scrape_transfer=True,
        scrape_input_dir=str(pending),
        scrape_ok_dir=str(ok_dir),
    )
    assert lib.id is not None
    media = await register_media_file(repo, lib.id, ok_dir / "MIDV-123.mp4")
    assert media.id is not None

    result = await _handler(repo).handle(ScrapePayload(number="MIDV-123", media_file_id=media.id))

    assert _transfers(result) == []
