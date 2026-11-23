"""SCRAPE_TRANSFER handler: 成功搬进库根, 失败搬进失败目录, 未开启时只记说明, 库内记录跟着走."""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from amane.handlers import ScrapeTransferHandler, ScrapeTransferPayload
from amane.handlers._common import LibraryTaskLocks

if TYPE_CHECKING:
    from amane.db.repository import Repository


async def _library(
    repo: Repository,
    root: Path,
    *,
    pending: Path | None = None,
    ok_dir: Path | None = None,
    fail_dir: Path | None = None,
    transfer: bool = True,
) -> int:
    root.mkdir(parents=True, exist_ok=True)
    library = await repo.create_library(
        name="日韩",
        path=str(root),
        scrape_transfer=transfer,
        scrape_input_dir=str(pending) if pending is not None else None,
        scrape_ok_dir=str(ok_dir) if ok_dir is not None else None,
        scrape_fail_dir=str(fail_dir) if fail_dir is not None else None,
    )
    assert library.id is not None
    return library.id


async def _media(repo: Repository, library_id: int, video: Path) -> int:
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    media = await repo.create_media_file(library_id, path=str(video))
    assert media.id is not None
    return media.id


@pytest.mark.asyncio(loop_scope="function")
async def test_success_moves_into_library_root_and_updates_path(repo: Repository, tmp_path: Path) -> None:
    """刮削成功: 视频与字幕离开输入目录进库根, 库内记录改指新路径."""
    root, pending = tmp_path / "done", tmp_path / "in"
    library_id = await _library(repo, root, pending=pending, fail_dir=None)
    video = pending / "ABC-123" / "ABC-123-1080p.mp4"
    media_id = await _media(repo, library_id, video)
    (video.parent / "ABC-123-1080p.srt").write_text("srt", encoding="utf-8")

    result = await ScrapeTransferHandler(repo, LibraryTaskLocks()).handle(
        ScrapeTransferPayload(media_file_id=media_id, outcome="ok")
    )

    assert result.success, result.error
    assert result.result is not None
    assert (result.result.files, result.result.failed, result.result.destination) == (2, 0, str(root))
    moved = root / video.name
    assert moved.exists()
    assert (root / "ABC-123-1080p.srt").exists()
    assert not video.parent.exists()
    media = await repo.get_media_file(media_id)
    assert media is not None
    assert media.path == str(moved)


@pytest.mark.asyncio(loop_scope="function")
async def test_failure_moves_into_fail_dir(repo: Repository, tmp_path: Path) -> None:
    """刮削失败: 进失败目录, 记录同样跟着走."""
    root, pending, fail_dir = tmp_path / "done", tmp_path / "in", tmp_path / "failed"
    library_id = await _library(repo, root, pending=pending, fail_dir=fail_dir)
    video = pending / "ABC-123.mp4"
    media_id = await _media(repo, library_id, video)

    result = await ScrapeTransferHandler(repo, LibraryTaskLocks()).handle(
        ScrapeTransferPayload(media_file_id=media_id, outcome="failed")
    )

    assert result.success, result.error
    assert (fail_dir / video.name).exists()
    media = await repo.get_media_file(media_id)
    assert media is not None
    assert media.path == str(fail_dir / video.name)


@pytest.mark.asyncio(loop_scope="function")
async def test_switch_off_reports_note_without_moving(repo: Repository, tmp_path: Path) -> None:
    """未开启时成功返回并说明原因, 文件留在原处."""
    root = tmp_path / "done"
    library_id = await _library(repo, root, transfer=False)
    video = root / "ABC-123.mp4"
    media_id = await _media(repo, library_id, video)

    result = await ScrapeTransferHandler(repo, LibraryTaskLocks()).handle(
        ScrapeTransferPayload(media_file_id=media_id, outcome="ok")
    )

    assert result.success
    assert result.result is not None
    assert result.result.note == "该媒体库未开启刮削后转移"
    assert video.exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_failure_without_fail_dir_stays_in_input(repo: Repository, tmp_path: Path) -> None:
    """没配失败目录时失败不搬, 文件留在输入目录便于重试."""
    root, pending = tmp_path / "done", tmp_path / "in"
    library_id = await _library(repo, root, pending=pending, fail_dir=None)
    video = pending / "ABC-123.mp4"
    media_id = await _media(repo, library_id, video)

    result = await ScrapeTransferHandler(repo, LibraryTaskLocks()).handle(
        ScrapeTransferPayload(media_file_id=media_id, outcome="failed")
    )

    assert result.success
    assert result.result is not None
    assert result.result.note == "文件已在原处, 无需转移"
    assert video.exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_missing_source_fails(repo: Repository, tmp_path: Path) -> None:
    """源文件已不在时判失败, 不动库内路径."""
    root, pending = tmp_path / "done", tmp_path / "in"
    library_id = await _library(repo, root, pending=pending, fail_dir=None)
    video = pending / "ABC-123.mp4"
    media_id = await _media(repo, library_id, video)
    video.unlink()

    result = await ScrapeTransferHandler(repo, LibraryTaskLocks()).handle(
        ScrapeTransferPayload(media_file_id=media_id, outcome="ok")
    )

    assert not result.success
    assert result.error is not None
    assert "源文件已不在" in result.error


@pytest.mark.asyncio(loop_scope="function")
async def test_success_moves_from_library_root_into_ok_dir(repo: Repository, tmp_path: Path) -> None:
    """库根就是待整理目录: 只配成功与失败目录, 通过后离开库根."""
    root, done, failed = tmp_path / "pending", tmp_path / "done", tmp_path / "failed"
    library_id = await _library(repo, root, ok_dir=done, fail_dir=failed)
    video = root / "ABC-123" / "ABC-123-1080p.mp4"
    media_id = await _media(repo, library_id, video)

    result = await ScrapeTransferHandler(repo, LibraryTaskLocks()).handle(
        ScrapeTransferPayload(media_file_id=media_id, outcome="ok")
    )

    assert result.success, result.error
    assert result.result is not None
    assert result.result.destination == str(done)
    assert (done / video.name).exists()
    media = await repo.get_media_file(media_id)
    assert media is not None
    assert media.path == str(done / video.name)


@pytest.mark.asyncio(loop_scope="function")
async def test_success_without_ok_dir_leaves_file_in_place(repo: Repository, tmp_path: Path) -> None:
    """没有输入目录也没有成功目录: 文件本来就在库根, 只记一句说明, 不搬也不生成副本."""
    root, failed = tmp_path / "media", tmp_path / "failed"
    library_id = await _library(repo, root, fail_dir=failed)
    video = root / "ABC-123" / "ABC-123-1080p.mp4"
    media_id = await _media(repo, library_id, video)

    result = await ScrapeTransferHandler(repo, LibraryTaskLocks()).handle(
        ScrapeTransferPayload(media_file_id=media_id, outcome="ok")
    )

    assert result.success
    assert result.result is not None
    assert result.result.files == 0
    assert result.result.note == "文件已在原处, 无需转移"
    assert video.exists()
    assert not (video.parent / "ABC-123-1080p (2).mp4").exists()
