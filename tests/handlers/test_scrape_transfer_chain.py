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
    repo: Repository, tmp_path: Path, *, ok_dir: Path | None, fail_dir: Path | None
) -> tuple[int, int]:
    lib_root = tmp_path / "lib"
    lib_root.mkdir(exist_ok=True)
    lib = await repo.create_library(
        name="t",
        path=str(lib_root),
        scrape_transfer=ok_dir is not None or fail_dir is not None,
        scrape_ok_dir=str(ok_dir) if ok_dir is not None else None,
        scrape_fail_dir=str(fail_dir) if fail_dir is not None else None,
    )
    assert lib.id is not None
    media = await register_media_file(repo, lib.id, lib_root / "incoming" / "MIDV-123.mp4")
    assert media.id is not None
    return lib.id, media.id


def _transfers(result: TaskResult[ScrapeResult]) -> list[FollowupTask]:
    return [f for f in result.followups if f.task_type == TaskType.SCRAPE_TRANSFER]


@pytest.mark.asyncio(loop_scope="function")
async def test_success_chains_transfer_into_ok_dir(repo: Repository, tmp_path: Path) -> None:
    """开关与成功目录都在时, 成功刮削为其文件描述一条转移后继."""
    _, media_id = await _library_with_file(repo, tmp_path, ok_dir=tmp_path / "done", fail_dir=None)

    result = await _handler(repo).handle(ScrapePayload(number="MIDV-123", media_file_id=media_id))

    assert result.success is True
    transfers = _transfers(result)
    assert len(transfers) == 1
    assert transfers[0].key == f"scrape-transfer:{media_id}"
    assert transfers[0].priority == -1
    assert transfers[0].payload == {"media_file_id": media_id, "outcome": "ok"}


@pytest.mark.asyncio(loop_scope="function")
async def test_success_without_ok_dir_chains_nothing(repo: Repository, tmp_path: Path) -> None:
    """只配了失败目录: 成功时不搬."""
    _, media_id = await _library_with_file(repo, tmp_path, ok_dir=None, fail_dir=tmp_path / "failed")

    result = await _handler(repo).handle(ScrapePayload(number="MIDV-123", media_file_id=media_id))

    assert result.success is True
    assert _transfers(result) == []


@pytest.mark.asyncio(loop_scope="function")
async def test_failure_followups_use_fail_dir(repo: Repository, tmp_path: Path) -> None:
    """刮削失败的后继由 worker 取得, outcome 是 failed."""
    _, media_id = await _library_with_file(repo, tmp_path, ok_dir=None, fail_dir=tmp_path / "failed")

    followups = await _handler(repo).failure_followups(ScrapePayload(number="MIDV-123", media_file_id=media_id))

    assert [f.task_type for f in followups] == [TaskType.SCRAPE_TRANSFER]
    assert followups[0].payload == {"media_file_id": media_id, "outcome": "failed"}


@pytest.mark.asyncio(loop_scope="function")
async def test_failure_followups_empty_when_switch_off(repo: Repository, tmp_path: Path) -> None:
    _, media_id = await _library_with_file(repo, tmp_path, ok_dir=None, fail_dir=None)

    followups = await _handler(repo).failure_followups(ScrapePayload(number="MIDV-123", media_file_id=media_id))

    assert followups == []


@pytest.mark.asyncio(loop_scope="function")
async def test_failure_followups_ignore_by_number_scrape(repo: Repository, tmp_path: Path) -> None:
    """按番号刮削没有对应文件, 失败也无可搬."""
    await _library_with_file(repo, tmp_path, ok_dir=None, fail_dir=tmp_path / "failed")

    followups = await _handler(repo).failure_followups(ScrapePayload(number="MIDV-123", media_file_id=None))

    assert followups == []


@pytest.mark.asyncio(loop_scope="function")
async def test_failure_followups_skip_deleted_file(repo: Repository, tmp_path: Path) -> None:
    """索引与任务不一致时静默跳过, 不让失败处理再多一层异常."""
    _, media_id = await _library_with_file(repo, tmp_path, ok_dir=None, fail_dir=tmp_path / "failed")
    await repo.delete_media_file(media_id)

    followups = await _handler(repo).failure_followups(ScrapePayload(number="MIDV-123", media_file_id=media_id))

    assert followups == []
