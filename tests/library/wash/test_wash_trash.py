"""回收站的落点计算与三种文件操作: 搬入、还原、真删.

这里只用 ``tmp_path`` 造目录, 不碰数据库; 谁该被搬是 ``model`` 的事, 谁被记进库是 ``apply`` 的事.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amane.db.models import WashRecordKind
from amane.library.wash.trash import (
    TRASH_DIRNAME,
    is_trash_path,
    move_into_trash,
    purge_trash_paths,
    restore_from_trash,
    restore_target,
    trash_root,
    trash_target,
)


@pytest.fixture
def library_root(tmp_path: Path) -> Path:
    root = tmp_path / "media" / "lib"
    root.mkdir(parents=True)
    return root


def make_file(path: Path, *, size: int = 16) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


LAYOUT_CASES = [
    pytest.param("/media/lib", "/media/.amane_wash/lib", id="sibling-of-root"),
    pytest.param("/media/lib/", "/media/.amane_wash/lib", id="trailing-slash"),
]


@pytest.mark.parametrize(("root", "expected"), LAYOUT_CASES)
def test_trash_root(root: str, expected: str) -> None:
    """回收站与库根同级: 同盘 rename, 且不写历史遗留的 ``.amane_trash``."""
    assert trash_root(root) == Path(expected)


def test_trash_target_mirrors_relative_path(library_root: Path) -> None:
    target = trash_target(library_root, kind=WashRecordKind.LOCAL, relative=Path("2024/ABC-123/ABC-123.mkv"))
    assert target == library_root.parent / TRASH_DIRNAME / "lib" / "local" / "2024/ABC-123/ABC-123.mkv"


TRASH_PATH_CASES = [
    pytest.param("/media/.amane_wash/lib/local/a.mkv", True, id="inside"),
    pytest.param("/media/lib/a.mkv", False, id="library-file"),
    pytest.param("/media/.amane_trash/a.mkv", False, id="legacy-trash"),
]


@pytest.mark.parametrize(("path", "expected"), TRASH_PATH_CASES)
def test_is_trash_path(path: str, expected: bool) -> None:
    """``.amane_trash`` 不算洗版回收站 —— 两个目录语义不同, 不能混着判."""
    assert is_trash_path(path) is expected


def test_restore_target_strips_trash_prefix(library_root: Path) -> None:
    removed = library_root.parent / TRASH_DIRNAME / "lib" / "strm" / "x/ABC-123.strm"
    assert (
        restore_target(removed, library_root=library_root, kind=WashRecordKind.STRM) == library_root / "x/ABC-123.strm"
    )


def test_restore_target_rejects_wrong_kind(library_root: Path) -> None:
    """记录与当前库根对不上时返回 None: 库被改名或被移走后不能瞎猜原位."""
    base = library_root.parent / TRASH_DIRNAME / "lib"
    assert restore_target(base / "strm" / "a.mkv", library_root=library_root, kind=WashRecordKind.LOCAL) is None
    assert restore_target(base / "local", library_root=library_root, kind=WashRecordKind.LOCAL) is None
    assert restore_target(library_root / "a.mkv", library_root=library_root, kind=WashRecordKind.LOCAL) is None


@pytest.mark.asyncio(loop_scope="function")
async def test_move_keeps_tree_and_counts_bytes(library_root: Path) -> None:
    """搬移保留库内相对路径, 腾出的字节按源文件大小记."""
    source = make_file(library_root / "2024" / "ABC-123" / "ABC-123-1080p.mp4", size=1234)

    outcome = await move_into_trash(source, library_root=library_root, kind=WashRecordKind.LOCAL)

    assert (outcome.status, outcome.size) == ("moved", 1234)
    assert (
        outcome.target
        == library_root.parent / TRASH_DIRNAME / "lib" / "local" / "2024" / "ABC-123" / "ABC-123-1080p.mp4"
    )
    assert not source.exists()
    assert outcome.target is not None and outcome.target.read_bytes() == b"x" * 1234


@pytest.mark.asyncio(loop_scope="function")
async def test_move_never_overwrites_occupied_slot(library_root: Path) -> None:
    """落点已有文件时停下: 覆盖等于静默丢掉回收站里的那一份."""
    source = make_file(library_root / "a" / "ABC-123.mkv", size=8)
    occupied = trash_target(library_root, kind=WashRecordKind.LOCAL, relative=Path("a/ABC-123.mkv"))
    make_file(occupied, size=99)

    outcome = await move_into_trash(source, library_root=library_root, kind=WashRecordKind.LOCAL)

    assert outcome.status == "occupied"
    assert source.exists()
    assert occupied.read_bytes() == b"x" * 99


@pytest.mark.asyncio(loop_scope="function")
async def test_move_reports_missing_source(library_root: Path) -> None:
    """源已经不在盘上 (例如上一次已经搬过) 时如实返回 missing, 交给调用方计入 skipped."""
    outcome = await move_into_trash(library_root / "gone.mkv", library_root=library_root, kind=WashRecordKind.LOCAL)
    assert outcome.status == "missing"


@pytest.mark.asyncio(loop_scope="function")
async def test_move_refuses_source_outside_root(tmp_path: Path, library_root: Path) -> None:
    """库根之外的文件不归洗版管, 搬走就是越界. Note: 只拒绝, 不删除."""
    outside = make_file(tmp_path / "other" / "ABC-123.mkv")

    outcome = await move_into_trash(outside, library_root=library_root, kind=WashRecordKind.LOCAL)

    assert outcome.status == "failed"
    assert outcome.error is not None and "outside library root" in outcome.error
    assert outside.exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_move_refuses_directory(library_root: Path) -> None:
    """目录里可能还塞着字幕与封面, 整目录搬走等于不可控."""
    directory = library_root / "a-dir"
    directory.mkdir()

    outcome = await move_into_trash(directory, library_root=library_root, kind=WashRecordKind.LOCAL)

    assert outcome.status == "failed"
    assert outcome.error is not None and "source is a directory" in outcome.error
    assert directory.is_dir()


@pytest.mark.asyncio(loop_scope="function")
async def test_move_refuses_file_already_in_trash(library_root: Path) -> None:
    """回收站内的文件不能再搬一次, 否则每跑一次任务就多套一层目录."""
    inside = make_file(library_root.parent / TRASH_DIRNAME / "lib" / "local" / "a.mkv")

    outcome = await move_into_trash(inside, library_root=library_root, kind=WashRecordKind.LOCAL)

    assert outcome.status == "failed"
    assert inside.exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_restore_returns_file_to_original_path(library_root: Path) -> None:
    """还原回原位, 并把搬空的多层目录收掉."""
    source = make_file(library_root / "2024" / "ABC-123" / "ABC-123.mkv", size=32)
    moved = await move_into_trash(source, library_root=library_root, kind=WashRecordKind.LOCAL)
    assert moved.target is not None

    outcome = await restore_from_trash(moved.target, library_root=library_root, kind=WashRecordKind.LOCAL)

    assert (outcome.status, outcome.size) == ("moved", 32)
    assert outcome.target == source
    assert source.read_bytes() == b"x" * 32
    assert not moved.target.exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_restore_never_overwrites_existing_file(library_root: Path) -> None:
    """原位又冒出一份时停下, 让人决定留哪个."""
    source = make_file(library_root / "a" / "ABC-123.mkv", size=8)
    moved = await move_into_trash(source, library_root=library_root, kind=WashRecordKind.LOCAL)
    assert moved.target is not None
    make_file(source, size=3)

    outcome = await restore_from_trash(moved.target, library_root=library_root, kind=WashRecordKind.LOCAL)

    assert outcome.status == "occupied"
    assert source.read_bytes() == b"x" * 3
    assert moved.target.exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_restore_refuses_non_trash_path(library_root: Path) -> None:
    """只有回收站里的文件能被还原; 否则等于给了任意写库内路径的入口."""
    source = make_file(library_root / "a.mkv")

    outcome = await restore_from_trash(source, library_root=library_root, kind=WashRecordKind.LOCAL)

    assert outcome.status == "failed"
    assert outcome.error is not None and "not a wash trash path" in outcome.error
    assert source.exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_restore_reports_missing(library_root: Path) -> None:
    """记录指向的文件已不在回收站时如实返回 missing (可能已被人手工还原)."""
    removed = library_root.parent / TRASH_DIRNAME / "lib" / "local" / "a.mkv"

    outcome = await restore_from_trash(removed, library_root=library_root, kind=WashRecordKind.LOCAL)

    assert outcome.status == "missing"


@pytest.mark.asyncio(loop_scope="function")
async def test_purge_deletes_only_given_paths_and_prunes_dirs(library_root: Path) -> None:
    """真删只发生在给定路径上, 并自底向上收掉空目录 —— 但保留 ``.amane_wash`` 根."""
    kept = make_file(library_root.parent / TRASH_DIRNAME / "lib" / "local" / "keep.mkv")
    expired = make_file(library_root.parent / TRASH_DIRNAME / "lib" / "local" / "2024" / "ABC-123" / "a.mkv", size=64)

    report = await purge_trash_paths([expired])

    assert (report.purged, report.freed_bytes, report.failed) == (1, 64, 0)
    assert not expired.exists()
    assert not (library_root.parent / TRASH_DIRNAME / "lib" / "local" / "2024").exists()
    assert kept.exists()
    assert (library_root.parent / TRASH_DIRNAME).is_dir()


@pytest.mark.asyncio(loop_scope="function")
async def test_purge_counts_missing(library_root: Path) -> None:
    """文件已经不在时计 missing, 不算失败."""
    report = await purge_trash_paths([library_root.parent / TRASH_DIRNAME / "lib" / "local" / "gone.mkv"])
    assert (report.purged, report.missing, report.failed) == (0, 1, 0)


@pytest.mark.asyncio(loop_scope="function")
async def test_purge_refuses_library_file(library_root: Path) -> None:
    """库内文件绝不能被 purge 删: 这条防线挡住「记录被写坏 → 删了正片」."""
    library_file = make_file(library_root / "ABC-123.mkv", size=8)

    report = await purge_trash_paths([library_file])

    assert (report.purged, report.failed) == (0, 1)
    assert library_file.read_bytes() == b"x" * 8
