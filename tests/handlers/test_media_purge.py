"""``MEDIA_PURGE`` 的编排用例: 从片库条目出发删文件、删行、删条目, 回收站里的拒删."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from amane.config.manager import HotSettings
from amane.handlers.media_purge import MediaPurgeHandler
from amane.handlers.models import MediaPurgePayload

if TYPE_CHECKING:
    from pathlib import Path

    from amane.db.repository import Repository


async def _seed(repo: Repository, root: Path, *, number: str, name: str) -> tuple[object, int, Path]:
    """建一个带元数据的库 + 一份落在磁盘上的媒体文件."""
    root.mkdir(parents=True, exist_ok=True)
    library = await repo.create_library(name=f"lib-{number}", path=str(root))
    assert library.id is not None
    path = root / name
    path.write_bytes(b"x" * 1024)
    media = await repo.create_media_file(library_id=library.id, path=str(path))
    assert media.id is not None
    metadata = await repo.upsert_metadata(number=number, title="示例")
    assert metadata.id is not None
    await repo.update_media_file(media.id, metadata_id=metadata.id)
    return library, metadata.id, path


class TestMediaPurge:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_deletes_files_rows_and_entries(self, repo: Repository, tmp_path: Path):
        library, metadata_id, path = await _seed(repo, tmp_path / "p1", number="AD-1", name="AD-1.mp4")
        assert library.id is not None
        handler = MediaPurgeHandler(repo, HotSettings())
        result = await handler.handle(MediaPurgePayload(library_id=library.id, metadata_ids=[metadata_id]))

        assert result.success is True
        body = result.result
        assert body is not None
        assert (body.files_deleted, body.indexed, body.metadata_deleted) == (1, 1, 1)
        assert not path.exists()
        assert await repo.get_metadata(metadata_id) is None
        assert await repo.list_media_files(library_id=library.id, limit=None) == []

    @pytest.mark.asyncio(loop_scope="function")
    async def test_keeps_files_when_asked(self, repo: Repository, tmp_path: Path):
        library, metadata_id, path = await _seed(repo, tmp_path / "p2", number="AD-2", name="AD-2.mp4")
        assert library.id is not None
        handler = MediaPurgeHandler(repo, HotSettings())
        result = await handler.handle(
            MediaPurgePayload(library_id=library.id, metadata_ids=[metadata_id], delete_files=False)
        )

        assert result.result is not None
        assert (result.result.files_deleted, result.result.metadata_deleted) == (0, 1)
        assert path.exists()
        assert await repo.list_media_files(library_id=library.id, limit=None) == []

    @pytest.mark.asyncio(loop_scope="function")
    async def test_missing_file_counts_but_does_not_fail(self, repo: Repository, tmp_path: Path):
        library, metadata_id, path = await _seed(repo, tmp_path / "p3", number="AD-3", name="AD-3.mp4")
        assert library.id is not None
        path.unlink()
        handler = MediaPurgeHandler(repo, HotSettings())
        result = await handler.handle(MediaPurgePayload(library_id=library.id, metadata_ids=[metadata_id]))

        assert result.success is True
        assert result.result is not None
        assert (result.result.missing, result.result.indexed, result.result.metadata_deleted) == (1, 1, 1)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_refuses_to_touch_the_trash(self, repo: Repository, tmp_path: Path):
        """回收站里的东西还能还原: 拒绝删除, 记 refused 并标失败."""
        root = tmp_path / "p4"
        library, metadata_id, path = await _seed(repo, root, number="AD-4", name="AD-4.mp4")
        assert library.id is not None
        trash = root / ".amane_wash"
        trash.mkdir()
        moved = trash / "AD-4.mp4"
        path.rename(moved)
        media = await repo.list_media_files(library_id=library.id, limit=None)
        assert media and media[0].id is not None
        await repo.update_media_file(media[0].id, path=str(moved))

        handler = MediaPurgeHandler(repo, HotSettings())
        result = await handler.handle(MediaPurgePayload(library_id=library.id, metadata_ids=[metadata_id]))

        assert result.success is False
        assert result.result is not None
        assert (result.result.refused, result.result.files_deleted) == (1, 0)
        assert moved.exists()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_prunes_emptied_directories(self, repo: Repository, tmp_path: Path):
        root = tmp_path / "p5"
        nested = root / "演员" / "AD-5"
        nested.mkdir(parents=True)
        library = await repo.create_library(name="lib-ad5", path=str(root))
        assert library.id is not None
        path = nested / "AD-5.mp4"
        path.write_bytes(b"x" * 64)
        media = await repo.create_media_file(library_id=library.id, path=str(path))
        assert media.id is not None
        metadata = await repo.upsert_metadata(number="AD-5", title="示例")
        assert metadata.id is not None
        await repo.update_media_file(media.id, metadata_id=metadata.id)

        handler = MediaPurgeHandler(repo, HotSettings())
        result = await handler.handle(MediaPurgePayload(library_id=library.id, metadata_ids=[metadata.id]))

        assert result.result is not None
        assert result.result.pruned_dirs >= 2
        assert not nested.exists()
        assert root.exists()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_refuses_paths_outside_the_library_roots(self, repo: Repository, tmp_path: Path):
        """索引里的路径落在库根之外 (符号链接解析到别处、库路径被改过) 时拒删, 条目保留."""
        library, metadata_id, _ = await _seed(repo, tmp_path / "p6", number="AD-6", name="AD-6.mp4")
        assert library.id is not None
        outside = tmp_path / "outside" / "AD-6.mp4"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"x" * 16)
        media = await repo.list_media_files(library_id=library.id, limit=None)
        assert media and media[0].id is not None
        await repo.update_media_file(media[0].id, path=str(outside))

        handler = MediaPurgeHandler(repo, HotSettings())
        result = await handler.handle(MediaPurgePayload(library_id=library.id, metadata_ids=[metadata_id]))

        assert result.success is False
        assert result.result is not None
        assert (result.result.refused, result.result.files_deleted, result.result.metadata_deleted) == (1, 0, 0)
        assert outside.exists()
        assert await repo.get_metadata(metadata_id) is not None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_keeps_entry_when_another_library_references_it(self, repo: Repository, tmp_path: Path):
        """别的库还有文件引用同一条目时不删条目: 级联删除会把那个库的索引行重置成待刮削."""
        library, metadata_id, path = await _seed(repo, tmp_path / "p7", number="AD-7", name="AD-7.mp4")
        assert library.id is not None
        other = await repo.create_library(name="lib-other", path=str(tmp_path / "p7b"))
        assert other.id is not None
        twin = tmp_path / "p7b" / "AD-7.strm"
        twin.parent.mkdir(parents=True)
        twin.write_bytes(b"x" * 8)
        twin_media = await repo.create_media_file(library_id=other.id, path=str(twin))
        assert twin_media.id is not None
        await repo.update_media_file(twin_media.id, metadata_id=metadata_id)

        handler = MediaPurgeHandler(repo, HotSettings())
        result = await handler.handle(MediaPurgePayload(library_id=library.id, metadata_ids=[metadata_id]))

        assert result.success is True
        assert result.result is not None
        assert (result.result.files_deleted, result.result.metadata_deleted) == (1, 0)
        assert not path.exists()
        assert await repo.get_metadata(metadata_id) is not None
        survivor = await repo.get_media_file(twin_media.id)
        assert survivor is not None and survivor.status is not None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_keeps_source_dirs_when_asked(self, repo: Repository, tmp_path: Path):
        """``keep_source_dirs`` 打开时不剪空目录."""
        root = tmp_path / "p8"
        nested = root / "演员" / "AD-8"
        nested.mkdir(parents=True)
        library = await repo.create_library(name="lib-ad8", path=str(root), keep_source_dirs=True)
        assert library.id is not None
        path = nested / "AD-8.mp4"
        path.write_bytes(b"x" * 32)
        media = await repo.create_media_file(library_id=library.id, path=str(path))
        assert media.id is not None
        metadata = await repo.upsert_metadata(number="AD-8", title="示例")
        assert metadata.id is not None
        await repo.update_media_file(media.id, metadata_id=metadata.id)

        handler = MediaPurgeHandler(repo, HotSettings())
        result = await handler.handle(MediaPurgePayload(library_id=library.id, metadata_ids=[metadata.id]))

        assert result.result is not None
        assert result.result.pruned_dirs == 0
        assert nested.exists()
