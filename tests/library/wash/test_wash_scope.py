"""洗版只比较成品目录里的文件.

库根的成品与成功目录里的成品都算; 输入目录 (待整理) 与失败目录 (待复制) 里的不算.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from amane.db.models import WashRecordKind
from amane.library.wash.collect import collect_entries

if TYPE_CHECKING:
    from pathlib import Path

    from .conftest import WashLibrary


@pytest.mark.asyncio(loop_scope="function")
async def test_collect_skips_pending_and_failed_dirs(wash_lib: WashLibrary, tmp_path: Path) -> None:
    """待整理与失败目录里的同番号文件不参与「留最优」比较."""
    assert wash_lib.library.id is not None
    pending, failed, done = tmp_path / "pending", tmp_path / "failed", tmp_path / "done"
    await wash_lib.repo.update_library(
        wash_lib.library.id,
        scrape_transfer=True,
        scrape_input_dir=str(pending),
        scrape_fail_dir=str(failed),
        scrape_ok_dir=str(done),
    )
    await wash_lib.add("ABC-123/ABC-123-1080p.mkv", size=8192)
    kept = await wash_lib.add("ABC-123/ABC-123-720p.mkv", path=str(done / "ABC-123" / "ABC-123-720p.mkv"))
    await wash_lib.add("ABC-123/ABC-123-480p.mkv", path=str(pending / "ABC-123" / "ABC-123-480p.mkv"))
    await wash_lib.add("ABC-123/ABC-123-360p.mkv", path=str(failed / "ABC-123" / "ABC-123-360p.mkv"))

    entries = await collect_entries(wash_lib.repo, library_id=wash_lib.library.id, kinds=[WashRecordKind.LOCAL])

    assert {entry.path for entry in entries} == {
        str(wash_lib.root / "ABC-123" / "ABC-123-1080p.mkv"),
        str(kept),
    }
