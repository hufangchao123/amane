"""字幕巡检: 只看文件名判配不上, 缺外挂字幕取库内标记, 两者分开计数."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from amane.handlers import SubtitleAuditHandler, SubtitleAuditPayload
from amane.subtitles.audit import scan_library

if TYPE_CHECKING:
    from amane.db.repository import Repository

SUFFIXES = [".srt", ".ass"]


def _write(path: Path, content: str = "x") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path.as_posix()


@pytest.mark.asyncio(loop_scope="function")
async def test_scan_matches_subtitle_starting_with_video_stem(tmp_path: Path) -> None:
    video = _write(tmp_path / "ABC-123" / "ABC-123-1080p.mp4")
    _write(tmp_path / "ABC-123" / "ABC-123-1080p.zh.srt")

    scan = await scan_library([video], SUFFIXES)

    assert (scan.videos, scan.subtitles) == (1, 1)
    assert scan.findings == []


@pytest.mark.asyncio(loop_scope="function")
async def test_scan_reports_subtitle_without_matching_video(tmp_path: Path) -> None:
    video = _write(tmp_path / "ABC-123" / "ABC-123-1080p.mp4")
    _write(tmp_path / "ABC-123" / "ABC-123-720p.zh.srt")

    scan = await scan_library([video], SUFFIXES)

    assert [finding.path for finding in scan.findings] == [(tmp_path / "ABC-123" / "ABC-123-720p.zh.srt").as_posix()]
    assert "同目录没有同名视频" in scan.findings[0].detail
    assert "ABC-123" in scan.findings[0].detail


@pytest.mark.asyncio(loop_scope="function")
async def test_scan_ignores_extensions_outside_library_setting(tmp_path: Path) -> None:
    video = _write(tmp_path / "ABC-123" / "ABC-123-1080p.mp4")
    _write(tmp_path / "ABC-123" / "ABC-123-1080p.idx")

    scan = await scan_library([video], SUFFIXES)

    assert (scan.subtitles, scan.findings) == (0, [])


@pytest.mark.asyncio(loop_scope="function")
async def test_scan_without_files_or_suffixes_counts_nothing(tmp_path: Path) -> None:
    assert (await scan_library([], SUFFIXES)).findings == []
    video = _write(tmp_path / "ABC-123" / "ABC-123.mp4")
    _write(tmp_path / "ABC-123" / "ABC-123.zh.srt")
    assert (await scan_library([video], [])).subtitles == 0


@pytest.mark.asyncio(loop_scope="function")
async def test_handler_counts_missing_and_orphans(repo: Repository, tmp_path: Path) -> None:
    root = tmp_path / "lib"
    library = await repo.create_library(name="t", path=str(root))
    assert library.id is not None
    video = _write(root / "ABC-123" / "ABC-123-1080p.mp4")
    _write(root / "ABC-123" / "ABC-123-720p.zh.srt")
    await repo.create_media_file(library.id, path=video)

    result = await SubtitleAuditHandler(repo).handle(SubtitleAuditPayload())

    assert result.success
    assert result.result is not None
    assert (result.result.libraries, result.result.videos, result.result.subtitles) == (1, 1, 1)
    assert (result.result.missing, result.result.orphans) == (1, 1)
    assert result.result.samples == [(root / "ABC-123" / "ABC-123-720p.zh.srt").as_posix()]


@pytest.mark.asyncio(loop_scope="function")
async def test_handler_rejects_unknown_library(repo: Repository) -> None:
    result = await SubtitleAuditHandler(repo).handle(SubtitleAuditPayload(library_id=4242))

    assert not result.success
    assert result.error == "媒体库 4242 不存在"
