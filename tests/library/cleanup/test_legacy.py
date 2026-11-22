"""历史回收站的统计与按保留期删除: 只看修改时间, 不碰库内索引."""

import os
import time
from pathlib import Path

import pytest

from amane.library.cleanup import legacy

pytestmark = pytest.mark.asyncio(loop_scope="function")


def _trash(root: Path) -> Path:
    """造一个回收站目录, 里面一份旧文件与一份新文件."""
    trash_dir = root / ".amane_trash"
    old = trash_dir / "subtitle" / "20260101" / "ABC-123.zh.srt"
    new = trash_dir / "dedupe" / "20261120" / "XYZ-456.mp4"
    for path in (old, new):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    stale = time.time() - 60 * 24 * 3600
    os.utime(old, (stale, stale))
    return trash_dir


async def test_scan_reports_files_and_oldest(tmp_path: Path) -> None:
    """统计给出文件数、总字节与最早的天数; 空目录报零."""
    trash_dir = _trash(tmp_path)
    stats = await legacy.scan(trash_dir)
    assert (stats.files, stats.bytes, stats.oldest_days) == (2, 2, 60)
    assert await legacy.scan(tmp_path / "缺少目录") == legacy.TrashStats(files=0, bytes=0, oldest_days=None)


async def test_purge_dry_run_keeps_files(tmp_path: Path) -> None:
    """``dry_run`` 只统计: 文件都还在, 但会给出将删除的样例."""
    trash_dir = _trash(tmp_path)
    outcome = await legacy.purge(trash_dir, 30, dry_run=True)
    assert (outcome.scanned, outcome.purged, outcome.kept, outcome.bytes_freed) == (2, 1, 1, 1)
    assert outcome.samples == ["subtitle/20260101/ABC-123.zh.srt"]
    assert (trash_dir / "subtitle" / "20260101" / "ABC-123.zh.srt").exists()


async def test_purge_removes_expired_and_prunes_dirs(tmp_path: Path) -> None:
    """超过保留期的文件被删除, 腾空的子目录一并回收, 回收站根目录保留."""
    trash_dir = _trash(tmp_path)
    outcome = await legacy.purge(trash_dir, 30, dry_run=False)
    assert (outcome.purged, outcome.kept, outcome.failed) == (1, 1, 0)
    assert not (trash_dir / "subtitle").exists()
    assert (trash_dir / "dedupe" / "20261120" / "XYZ-456.mp4").exists()
    assert trash_dir.is_dir()


@pytest.mark.parametrize("keep_days", [1, 3650])
async def test_purge_keep_days_boundary(tmp_path: Path, keep_days: int) -> None:
    """保留期取下界的 1 天与上界的 3650 天都按天数判定, 不报错."""
    trash_dir = _trash(tmp_path)
    outcome = await legacy.purge(trash_dir, keep_days, dry_run=True)
    assert outcome.purged + outcome.kept == 2


async def test_purge_missing_dir_is_noop(tmp_path: Path) -> None:
    """回收站不存在时什么都不做: 多数库没有历史内容."""
    outcome = await legacy.purge(tmp_path / "没有回收站", 30, dry_run=False)
    assert (outcome.scanned, outcome.purged, outcome.failed) == (0, 0, 0)
