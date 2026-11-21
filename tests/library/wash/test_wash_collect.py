"""索引 + 磁盘实况 → 参与比较的条目.

重点在三条剔除规则 (文件不在盘上、番号解析不出、不是媒体扩展名) 与 STRM 的处置:
STRM 只当指针, 体积恒为 0, 绝不当成本地视频参与体积比较 (二次审计 P0-7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from amane.db.models import WashRecordKind
from amane.library.wash.collect import IndexRow, collect_entries, entries_from_rows

if TYPE_CHECKING:
    from pathlib import Path

    from .conftest import WashLibrary

LOCAL = WashRecordKind.LOCAL
STRM = WashRecordKind.STRM

CONDITION_CASES = [
    pytest.param("ABC-123-1080p.mkv", LOCAL, True, id="local-media"),
    pytest.param("ABC-123.strm", STRM, True, id="strm-pointer"),
    pytest.param("ABC-123.txt", LOCAL, False, id="non-media-extension"),
    pytest.param("-.mkv", LOCAL, False, id="no-number"),
]


@pytest.mark.parametrize(("name", "kind", "expected"), CONDITION_CASES)
@pytest.mark.asyncio(loop_scope="function")
async def test_entries_keep_only_usable_media(
    wash_lib: WashLibrary,
    name: str,
    kind: WashRecordKind,
    expected: bool,
) -> None:
    """只有「有番号 + 媒体扩展名 + 盘上还在」的文件才能进比较."""
    await wash_lib.add(f"a/{name}")

    entries = await collect_entries(wash_lib.repo, kinds=[LOCAL, STRM])

    assert [entry.kind for entry in entries] == ([kind] if expected else [])


@pytest.mark.asyncio(loop_scope="function")
async def test_strm_size_is_zero(wash_lib: WashLibrary) -> None:
    """STRM 的体积恒为 0: 拿指针文件大小当体积会把它误判成「更差的一份」."""
    await wash_lib.add("a/ABC-123.strm", size=2048)

    (entry,) = await collect_entries(wash_lib.repo, kinds=[STRM])

    assert (entry.kind, entry.size, entry.number) == (STRM, 0, "ABC-123")


@pytest.mark.asyncio(loop_scope="function")
async def test_local_size_comes_from_disk(wash_lib: WashLibrary) -> None:
    """体积以磁盘为准: 索引里的 size 是入库那时的快照, 换过文件就会失真."""
    path = await wash_lib.add("a/ABC-123-1080p.mkv", size=10)
    path.write_bytes(b"x" * 70)

    (entry,) = await collect_entries(wash_lib.repo, kinds=[LOCAL])

    assert entry.size == 70


@pytest.mark.asyncio(loop_scope="function")
async def test_missing_file_is_dropped(wash_lib: WashLibrary) -> None:
    """索引比磁盘旧是常态; 搬不走的东西不该出现在候选里."""
    path = await wash_lib.add("a/ABC-123-1080p.mkv")
    path.unlink()

    assert await collect_entries(wash_lib.repo, kinds=[LOCAL]) == []


@pytest.mark.asyncio(loop_scope="function")
async def test_vanished_strm_is_dropped(wash_lib: WashLibrary) -> None:
    """STRM 不看体积, 但指针文件本身消失后同样要剔除: 否则每次扫描都会把已洗掉的那份算回来."""
    path = await wash_lib.add("a/ABC-123.strm", size=0)
    path.unlink()

    assert await collect_entries(wash_lib.repo, kinds=[STRM]) == []


TRASH_CASES = [
    pytest.param(".amane_wash", id="wash-trash"),
    pytest.param(".amane_trash", id="legacy-trash"),
]


@pytest.mark.parametrize("dirname", TRASH_CASES)
@pytest.mark.asyncio(loop_scope="function")
async def test_trash_paths_are_skipped(wash_lib: WashLibrary, dirname: str) -> None:
    """回收站里的文件既不是候选, 也不该被再搬一次."""
    trash_root = wash_lib.root.parent / dirname
    await wash_lib.add("a/ABC-123-1080p.mkv", path=str(trash_root / "ABC-123-1080p.mkv"))

    assert await collect_entries(wash_lib.repo, kinds=[LOCAL]) == []


@pytest.mark.asyncio(loop_scope="function")
async def test_kind_filter_is_applied(wash_lib: WashLibrary) -> None:
    """``kinds`` 决定这一趟只算本地还是只算 STRM (配置里两侧可分别关掉)."""
    await wash_lib.add("a/ABC-123.mkv")
    await wash_lib.add("b/ABC-124.strm")

    assert [entry.kind for entry in await collect_entries(wash_lib.repo, kinds=[LOCAL])] == [LOCAL]
    assert [entry.kind for entry in await collect_entries(wash_lib.repo, kinds=[STRM])] == [STRM]
    assert len(await collect_entries(wash_lib.repo, kinds=[])) == 0


@pytest.mark.asyncio(loop_scope="function")
async def test_library_id_limits_scope(wash_lib: WashLibrary, tmp_path: Path) -> None:
    """限定库时只读那一个库; 别的库的同名文件不能混进来 (否则会跨库互删)."""
    await wash_lib.add("a/ABC-123.mkv")
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = await wash_lib.repo.create_library(name="other", path=str(other_root), write_nfo=False)
    assert other.id is not None
    (other_root / "ABC-123.mkv").write_bytes(b"x")
    await wash_lib.repo.create_media_file(other.id, path=str(other_root / "ABC-123.mkv"))

    entries = await collect_entries(wash_lib.repo, kinds=[LOCAL], library_id=other.id)

    assert [(entry.library_id, entry.path) for entry in entries] == [(other.id, str(other_root / "ABC-123.mkv"))]


@pytest.mark.asyncio(loop_scope="function")
async def test_unknown_library_yields_nothing(wash_lib: WashLibrary) -> None:
    """库不存在时返回空, 由调用方决定是否报错."""
    assert await collect_entries(wash_lib.repo, kinds=[LOCAL], library_id=9999) == []


@pytest.mark.asyncio(loop_scope="function")
async def test_index_number_falls_back_to_filename(wash_lib: WashLibrary) -> None:
    """索引里没有番号时用文件名解析的结果 —— 与线上主流程一致."""
    await wash_lib.add("a/ABC-123.strm")

    (entry,) = await entries_from_rows(
        [
            IndexRow(
                path=str(wash_lib.root / "a/ABC-123.strm"),
                number=None,
                size=None,
                duration=None,
                definition=None,
                mosaic=None,
            )
        ],
        library_id=1,
    )

    assert entry.number == "ABC-123"
