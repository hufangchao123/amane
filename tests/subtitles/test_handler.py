"""``SUBTITLE_FETCH`` 的编排用例: 选目标、跳过、失败分类与"落位走 organize"这条不变量."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from amane.config.manager import HotSettings, SubtitleFetchConfig
from amane.db.models import MediaFileStatus
from amane.handlers import subtitle_fetch as module
from amane.handlers.models import SubtitleFetchPayload
from amane.handlers.subtitle_fetch import SubtitleFetchHandler
from tests.subtitles.support import FakeHttp, seed_media

if TYPE_CHECKING:
    from pathlib import Path

    from amane.db.repository import Repository


def _handler(repo: Repository, web: FakeHttp, config: SubtitleFetchConfig) -> SubtitleFetchHandler:
    return SubtitleFetchHandler(repo, HotSettings(subtitles=config), web=web)


class TestTargets:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_strm_video_is_selected_and_placed(
        self,
        repo: Repository,
        tmp_path: Path,
        subtitle_config: SubtitleFetchConfig,
        subtitlecat_pages: dict[str, str],
    ):
        """.strm 也算成片: 115 直链库全是 .strm, 认不出它等于整个库都补不到字幕."""
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root, number="AD-1", suffix=".strm")

        web = FakeHttp(texts=subtitlecat_pages)
        assert media.id is not None
        result = await _handler(repo, web, subtitle_config).handle(SubtitleFetchPayload(media_file_id=media.id))

        assert result.success is True
        assert result.result is not None
        assert result.result.fetched == 1
        assert result.result.failed == 0
        assert (root / "AD-1.zh.srt").exists()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_existing_subtitle_is_skipped_without_network(
        self,
        repo: Repository,
        tmp_path: Path,
        subtitle_config: SubtitleFetchConfig,
        subtitlecat_pages: dict[str, str],
    ):
        """已有外挂字幕的文件既不抓也不覆盖: 覆盖等于把用户的字幕换掉."""
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root, number="AD-1", subtitle="AD-1.zh.srt")

        web = FakeHttp(texts=subtitlecat_pages)
        assert media.id is not None
        result = await _handler(repo, web, subtitle_config).handle(SubtitleFetchPayload(media_file_id=media.id))

        assert result.success is True
        assert result.result is not None
        assert result.result.fetched == 0
        assert result.result.missing == 0
        assert web.calls == []
        refreshed = await repo.get_media_file(media.id)
        assert refreshed is not None
        assert refreshed.has_external_subtitle is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_file_outside_the_library_gets_the_subtitle_next_to_it(
        self,
        repo: Repository,
        tmp_path: Path,
        subtitle_config: SubtitleFetchConfig,
        subtitlecat_pages: dict[str, str],
    ):
        """还在输入目录里的文件按库模板算不出位置: 字幕落在它旁边, 随文件一起被转移带走.

        库模板默认用绝对路径 ``{link_dir}/{raw_srt_name}.{ext}``, 基准目录只有库根, 于是
        「落位」会抛 ``ValueError: 路径越界`` —— 2026-11-24 实测就是这样把整条补全任务打成失败的.
        """
        root = tmp_path / "lib"
        root.mkdir()
        library = await repo.create_library(
            name="lib-downloads",
            path=str(root),
            subtitle_extensions=[".srt"],
            write_nfo=False,
        )
        assert library.id is not None
        staging = tmp_path / "downloads"
        staging.mkdir()
        video = staging / "AD-2.mp4"
        video.write_bytes(b"not a real video")
        await repo.upsert_metadata(number="AD-2", title="AD-2 标题")
        metadata = await repo.get_metadata_by_number("AD-2")
        assert metadata is not None
        media = await repo.create_media_file(
            library.id,
            path=str(video),
            number="AD-2",
            metadata_id=metadata.id,
            status=MediaFileStatus.SCRAPED,
        )
        assert media.id is not None

        pages = dict(subtitlecat_pages)
        pages["https://www.subtitlecat.com/index.php?search=AD-2"] = (
            '<html><body><a href="subs/300/ad-2-chinese.html">AD-2 中文字幕</a></body></html>'
        )
        pages["https://www.subtitlecat.com/subs/300/ad-2-chinese.html"] = (
            '<html><body><a href="/subs/300/ad-2-zh-CN.srt">简体</a></body></html>'
        )
        web = FakeHttp(texts=pages)
        result = await _handler(repo, web, subtitle_config).handle(SubtitleFetchPayload(media_file_id=media.id))

        assert result.success is True
        assert result.result is not None
        assert result.result.fetched == 1
        assert (staging / "AD-2.zh.srt").exists()
        assert not (root / "AD-2.zh.srt").exists()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_library_without_subtitle_extensions_is_skipped(
        self,
        repo: Repository,
        tmp_path: Path,
        subtitle_config: SubtitleFetchConfig,
    ):
        """库关掉了字幕发现就等于用户不想要字幕, 不该在这里擅自补."""
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root, number="AD-1", subtitle_extensions=[])

        web = FakeHttp()
        assert media.id is not None
        result = await _handler(repo, web, subtitle_config).handle(SubtitleFetchPayload(media_file_id=media.id))

        assert result.success is True
        assert result.result is not None
        assert result.result.fetched == 0
        assert web.calls == []

    @pytest.mark.asyncio(loop_scope="function")
    async def test_library_payload_only_picks_missing_files(
        self,
        repo: Repository,
        tmp_path: Path,
        subtitle_config: SubtitleFetchConfig,
        subtitlecat_pages: dict[str, str],
    ):
        library, _ = await seed_media(repo, tmp_path / "lib" / "AD-1", number="AD-1")
        # 同库另两份: 一份已有字幕、一份还没刮削, 按缺口挑时都不该进目标.
        await seed_media(repo, tmp_path / "lib" / "AD-2", number="AD-2", subtitle="AD-2.zh.srt", library=library)
        await seed_media(repo, tmp_path / "lib" / "AD-3", number="AD-3", scraped=False, library=library)

        web = FakeHttp(texts=subtitlecat_pages)
        result = await _handler(repo, web, subtitle_config).handle(SubtitleFetchPayload(library_id=library.id))

        assert result.success is True
        assert result.result is not None
        assert result.result.fetched == 1
        assert [url for url in web.calls if "index.php" in url] == ["https://www.subtitlecat.com/index.php?search=AD-1"]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_missing_media_file_fails_loudly(self, repo: Repository, subtitle_config: SubtitleFetchConfig):
        result = await _handler(repo, FakeHttp(), subtitle_config).handle(SubtitleFetchPayload(media_file_id=999999))

        assert result.success is False
        assert result.error is not None
        assert "媒体文件不存在" in result.error

    @pytest.mark.asyncio(loop_scope="function")
    async def test_missing_library_fails_loudly(self, repo: Repository, subtitle_config: SubtitleFetchConfig):
        result = await _handler(repo, FakeHttp(), subtitle_config).handle(SubtitleFetchPayload(library_id=999999))

        assert result.success is False
        assert result.error is not None
        assert "媒体库不存在" in result.error

    @pytest.mark.asyncio(loop_scope="function")
    async def test_empty_library_succeeds_with_zero(self, repo: Repository, subtitle_config: SubtitleFetchConfig):
        result = await _handler(repo, FakeHttp(), subtitle_config).handle(SubtitleFetchPayload())

        assert result.success is True
        assert result.result is not None
        assert result.result.fetched == 0


class TestEnablement:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_disabled_config_reports_not_enabled(self, repo: Repository):
        config = SubtitleFetchConfig(enabled=False, providers=["subtitlecat"])
        result = await _handler(repo, FakeHttp(), config).handle(SubtitleFetchPayload())

        assert result.success is False
        assert result.error == "字幕补全未启用"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_assrt_without_token_means_no_usable_source(self, repo: Repository):
        """勾了 assrt 却没 token: 报未启用, 而不是让任务跑到一半才失败."""
        config = SubtitleFetchConfig(enabled=True, providers=["assrt"], assrt_token=None)
        result = await _handler(repo, FakeHttp(), config).handle(SubtitleFetchPayload())

        assert result.success is False
        assert result.error is not None
        assert "没有可用的字幕来源" in result.error


class TestPlacement:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_placement_goes_through_organize_subtitles(
        self,
        repo: Repository,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        subtitle_config: SubtitleFetchConfig,
        subtitlecat_pages: dict[str, str],
    ):
        """落位必须调用 ``organize.subtitles.place_subtitles``: 模板与命名只在那里实现."""
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root, number="AD-1")
        recorded: list[dict[str, object]] = []
        real = module.place_subtitles

        async def spy(sources, **kwargs):
            recorded.append({"sources": [p.name for p in sources], **kwargs})
            return await real(sources, **kwargs)

        monkeypatch.setattr(module, "place_subtitles", spy)
        result = await _handler(repo, FakeHttp(texts=subtitlecat_pages), subtitle_config).handle(
            SubtitleFetchPayload(media_file_id=media.id)
        )

        assert result.success is True
        assert len(recorded) == 1
        assert recorded[0]["sources"] == ["AD-1.zh.srt"]
        assert recorded[0]["video_source"] == root / "AD-1.mp4"
        assert (root / "AD-1.zh.srt").exists()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_existing_target_is_not_overwritten(
        self,
        repo: Repository,
        tmp_path: Path,
        subtitle_config: SubtitleFetchConfig,
        subtitlecat_pages: dict[str, str],
    ):
        """同名目标已存在时按失败计, 不覆盖: 用户的字幕比来源的更权威."""
        root = tmp_path / "lib"
        # 库只声明 .ass, 于是同名的 .srt 不会被"已有字幕"判定命中, 会一路走到落位冲突.
        _, media = await seed_media(repo, root, number="AD-1", subtitle_extensions=[".ass"])
        (root / "AD-1.zh.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nmine\n")

        result = await _handler(repo, FakeHttp(texts=subtitlecat_pages), subtitle_config).handle(
            SubtitleFetchPayload(media_file_id=media.id)
        )

        assert result.success is True
        assert result.result is not None
        assert result.result.fetched == 0
        assert result.result.failed == 1
        assert "mine" in (root / "AD-1.zh.srt").read_text()


class TestFailures:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_source_without_result_counts_as_missing(
        self,
        repo: Repository,
        tmp_path: Path,
        subtitle_config: SubtitleFetchConfig,
    ):
        """来源没有这份字幕是缺口, 不是任务失败: 任务仍然成功, 计数进 ``missing``."""
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root, number="AD-9")
        web = FakeHttp(texts={"https://www.subtitlecat.com/index.php?search=AD-9": "<html></html>"})

        assert media.id is not None
        result = await _handler(repo, web, subtitle_config).handle(SubtitleFetchPayload(media_file_id=media.id))

        assert result.success is True
        assert result.result is not None
        assert result.result.missing == 1
        assert result.result.failed == 0
        assert not (root / "AD-9.zh.srt").exists()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_transient_source_failure_counts_as_failed(
        self,
        repo: Repository,
        tmp_path: Path,
        subtitle_config: SubtitleFetchConfig,
    ):
        """限流与断连只说明这一轮没取到, 不说明来源没有: 记 ``failed``, 下一轮还能补上."""
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root, number="AD-1")
        url = "https://www.subtitlecat.com/index.php?search=AD-1"
        web = FakeHttp(texts={url: "<html></html>"}, transient_failures={url: 5})
        config = subtitle_config.model_copy(update={"max_attempts": 1})

        assert media.id is not None
        result = await _handler(repo, web, config).handle(SubtitleFetchPayload(media_file_id=media.id))

        assert result.success is True
        assert result.result is not None
        assert (result.result.missing, result.result.failed) == (0, 1)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_transient_failure_is_retried_up_to_max_attempts(
        self,
        repo: Repository,
        tmp_path: Path,
        subtitle_config: SubtitleFetchConfig,
        subtitlecat_pages: dict[str, str],
    ):
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root, number="AD-1")
        web = FakeHttp(
            texts=subtitlecat_pages,
            transient_failures={"https://www.subtitlecat.com/index.php?search=AD-1": 1},
        )
        config = subtitle_config.model_copy(update={"max_attempts": 2})

        result = await _handler(repo, web, config).handle(SubtitleFetchPayload(media_file_id=media.id))

        assert result.success is True
        assert result.result is not None
        assert result.result.fetched == 1
        assert web.calls.count("https://www.subtitlecat.com/index.php?search=AD-1") == 2

    @pytest.mark.asyncio(loop_scope="function")
    async def test_media_without_metadata_is_failed(
        self,
        repo: Repository,
        tmp_path: Path,
        subtitle_config: SubtitleFetchConfig,
        subtitlecat_pages: dict[str, str],
    ):
        """没有元数据就没法按模板落位: 记为失败而不是硬塞一个路径."""
        root = tmp_path / "lib"
        _, media = await seed_media(repo, root, number="AD-1", with_metadata=False)

        result = await _handler(repo, FakeHttp(texts=subtitlecat_pages), subtitle_config).handle(
            SubtitleFetchPayload(media_file_id=media.id)
        )

        assert result.success is True
        assert result.result is not None
        assert result.result.failed == 1
