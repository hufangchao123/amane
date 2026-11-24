"""状态卡的读数是只读查询: 缺口数与排队数不能靠遍历磁盘."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from amane.config.manager import SubtitleFetchConfig, SubtitleProvider
from amane.db.models import TaskType
from amane.subtitles import SubtitleFetcher, usable_providers
from tests.subtitles.support import seed_media

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from amane.db.repository import Repository


class TestSummary:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_counts_scraped_files_without_subtitle(self, repo: Repository, tmp_path: Path):
        library, _ = await seed_media(repo, tmp_path / "lib" / "A", number="AD-1")
        await seed_media(repo, tmp_path / "lib" / "B", number="AD-2", library=library)
        # 已有外挂字幕的不算缺口
        _, covered = await seed_media(repo, tmp_path / "lib" / "C", number="AD-3", library=library)
        assert covered.id is not None
        await repo.update_media_file(covered.id, has_external_subtitle=True)
        # 未刮削的不算: 连番号都未必有, 抓也抓不到
        await seed_media(repo, tmp_path / "lib" / "D", number="AD-4", library=library, scraped=False)

        fetcher = SubtitleFetcher(repo, SubtitleFetchConfig(enabled=True, providers=["subtitlecat"]))
        summary = await fetcher.summary()

        assert summary.missing == 2
        assert summary.multi_disc == 0
        assert summary.queued == 0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_multi_disc_is_counted_apart(self, repo: Repository, tmp_path: Path):
        """分卷片的整片字幕对不齐, 单独计数, 不算缺口."""
        library, _ = await seed_media(repo, tmp_path / "lib" / "A", number="AD-1")
        await seed_media(repo, tmp_path / "lib" / "CD1", number="AD-2", library=library)
        await seed_media(repo, tmp_path / "lib" / "CD2", number="AD-2", library=library)

        fetcher = SubtitleFetcher(repo, SubtitleFetchConfig(enabled=True, providers=["subtitlecat"]))
        summary = await fetcher.summary()

        assert summary.missing == 1
        assert summary.multi_disc == 2

    @pytest.mark.asyncio(loop_scope="function")
    async def test_pointer_files_are_counted_apart(self, repo: Repository, tmp_path: Path):
        """.strm 没有本地音轨, 转写跳过, 因此单独计数不算缺口."""
        library, _ = await seed_media(repo, tmp_path / "lib" / "A", number="AD-1")
        await seed_media(repo, tmp_path / "lib" / "S", number="AD-2", library=library, suffix=".strm")

        fetcher = SubtitleFetcher(repo, SubtitleFetchConfig(enabled=True, providers=["subtitlecat"]))
        summary = await fetcher.summary()

        assert summary.missing == 1
        assert summary.pointer == 1
        assert summary.multi_disc == 0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_buckets_do_not_overlap(self, repo: Repository, tmp_path: Path):
        """既是分卷又是 .strm 的文件只算一类: 两个桶各记一次会让 ``missing`` 变成负数."""
        _, _ = await seed_media(repo, tmp_path / "lib" / "CD1", number="AD-5", suffix=".strm")

        fetcher = SubtitleFetcher(repo, SubtitleFetchConfig(enabled=True, providers=["subtitlecat"]))
        summary = await fetcher.summary()

        assert (summary.missing, summary.multi_disc, summary.pointer) == (0, 1, 0)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_counts_queued_tasks(self, repo: Repository):
        await repo.create_task(TaskType.SUBTITLE_FETCH, {})
        await repo.create_task(TaskType.SUBTITLE_FETCH, {"library_id": 1})
        await repo.create_task(TaskType.SCRAPE, {"library_id": 1})

        fetcher = SubtitleFetcher(repo, SubtitleFetchConfig(enabled=True, providers=["subtitlecat"]))
        summary = await fetcher.summary()

        assert summary.queued == 2


class TestEnabled:
    def test_enabled_needs_switch_and_a_usable_source(self, repo: Repository):
        """``enabled`` 只看配置, 但构造要 repo; 用真 repo 免得为省一个 fixture 引入假类型."""
        assert SubtitleFetcher(repo, _config(enabled=True, providers=["subtitlecat"])).enabled is True
        assert SubtitleFetcher(repo, _config(enabled=False, providers=["subtitlecat"])).enabled is False
        assert SubtitleFetcher(repo, _config(enabled=True, providers=[])).enabled is False

    def test_providers_are_kept_in_configured_order(self):
        """两个来源都不要凭据, 可用性就是配置顺序本身; 重复项去重."""
        config = SubtitleFetchConfig(enabled=True, providers=["avsubtitles", "subtitlecat", "avsubtitles"])
        assert usable_providers(config) == ("avsubtitles", "subtitlecat")

    def test_empty_provider_list_is_not_enabled(self, repo: Repository):
        """勾掉所有来源等于未启用: 任务不该白跑一轮."""
        config = SubtitleFetchConfig(enabled=True, providers=[])
        assert usable_providers(config) == ()
        assert SubtitleFetcher(repo, config).enabled is False


def _config(*, enabled: bool, providers: Sequence[SubtitleProvider]) -> SubtitleFetchConfig:
    return SubtitleFetchConfig(enabled=enabled, providers=list(providers))
