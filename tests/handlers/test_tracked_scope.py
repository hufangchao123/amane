"""刮削后转移让记录落在库根之外: 整理、刷新、洗版三处都要把那些位置当成库内.

三个可选目录 (输入/成功/失败) 都算本库; 洗版只比较成品 (库根与成功目录).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from amane.config import HotSettings
from amane.db.models import MediaFileStatus
from amane.handlers import (
    OrganizeHandler,
    OrganizePayload,
    RefreshHandler,
    RefreshPayload,
    ScanMode,
)

if TYPE_CHECKING:
    from amane.db.repository import Repository
    from amane.media.resource_store import ResourceStore


async def _library_with_moved_file(repo: Repository, tmp_path: Path, *, ok_dir: bool) -> tuple[int, Path]:
    """库根只有成品目录, 一条记录指向库根之外的落地目录."""
    lib_root, done = tmp_path / "lib", tmp_path / "done"
    lib_root.mkdir()
    media_path = done / "ABC-123" / "ABC-123-1080p.mp4"
    media_path.parent.mkdir(parents=True)
    media_path.write_bytes(b"x" * 64)
    lib = await repo.create_library(
        name="t",
        path=str(lib_root),
        scrape_transfer=True,
        scrape_ok_dir=str(done) if ok_dir else None,
        scrape_fail_dir=None if ok_dir else str(done),
    )
    assert lib.id is not None
    await repo.create_media_file(lib.id, path=str(media_path), number="ABC-123", status=MediaFileStatus.SCRAPED)
    return lib.id, media_path


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("ok_dir", [True, False])
async def test_organize_keeps_records_moved_out_of_root(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, ok_dir: bool
) -> None:
    """整理前的剪枝不能删掉落在成功/失败目录的记录."""
    library_id, media_path = await _library_with_moved_file(repo, tmp_path, ok_dir=ok_dir)

    handler = OrganizeHandler(repo, HotSettings(), resource_store)
    payload = OrganizePayload(library_id=library_id)
    await payload.resolve(repo)
    result = await handler.handle(payload)

    assert result.success is True
    kept = list(await repo.list_media_files(library_id=library_id, limit=None))
    assert [row.path for row in kept] == [str(media_path)]


@pytest.mark.asyncio(loop_scope="function")
async def test_refresh_remove_keeps_records_in_landing_dirs(repo: Repository, tmp_path: Path) -> None:
    """整库刷新 (add+remove) 不能把后转移目的目录里的记录当成已消失."""
    library_id, media_path = await _library_with_moved_file(repo, tmp_path, ok_dir=True)

    handler = RefreshHandler(repo=repo)
    # 不传 path 会从进程 CWD 起扫, 用例里必须钉住库根.
    result = await handler.handle(
        RefreshPayload(library_id=library_id, path=str(tmp_path / "lib"), scan={ScanMode.add, ScanMode.remove})
    )

    assert result.success is True
    assert result.result is not None
    assert result.result.removed == 0
    kept = await repo.list_media_files(library_id=library_id, limit=None)
    assert [row.path for row in kept] == [str(media_path)]


@pytest.mark.asyncio(loop_scope="function")
async def test_refresh_remove_keeps_unreadable_rows(
    repo: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """权限 / IO 错误不等于文件消失: 只在确认 ENOENT 时删行, 读不到的留到下一趟."""
    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    target = lib_root / "ABC-123.mp4"
    target.write_bytes(b"x" * 64)
    lib = await repo.create_library(name="t", path=str(lib_root))
    assert lib.id is not None
    await repo.create_media_file(lib.id, path=str(target), number="ABC-123")

    real_stat = Path.stat

    def deny(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "ABC-123.mp4":
            raise PermissionError(13, "Permission denied", self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", deny)

    handler = RefreshHandler(repo=repo)
    result = await handler.handle(RefreshPayload(library_id=lib.id, path=str(lib_root), scan={ScanMode.remove}))

    assert result.success is True
    assert result.result is not None
    assert result.result.removed == 0
    assert result.result.unreadable == 1
    assert await repo.list_media_files(library_id=lib.id, limit=None)


@pytest.mark.asyncio(loop_scope="function")
async def test_organize_prune_keeps_unreadable_rows(
    repo: Repository, resource_store: ResourceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整理前的剪枝同样只信 ENOENT, 读不到的行留到下一趟."""
    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    target = lib_root / "ABC-123.mp4"
    target.write_bytes(b"x" * 64)
    lib = await repo.create_library(name="t", path=str(lib_root))
    assert lib.id is not None
    await repo.create_media_file(lib.id, path=str(target), number="ABC-123", status=MediaFileStatus.SCRAPED)

    real_stat = Path.stat

    def deny(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "ABC-123.mp4":
            raise PermissionError(13, "Permission denied", self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", deny)

    handler = OrganizeHandler(repo, HotSettings(), resource_store)
    payload = OrganizePayload(library_id=lib.id)
    await payload.resolve(repo)
    result = await handler.handle(payload)

    assert result.success is True
    assert result.result is not None
    assert result.result.unreadable == 1
    assert await repo.list_media_files(library_id=lib.id, limit=None)
