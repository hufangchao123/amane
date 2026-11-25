"""删除执行单元: 边界、符号链接、挂载点、剪枝与释放空间统计."""

from __future__ import annotations

import errno
import os
import unicodedata
from typing import TYPE_CHECKING

import pytest

from amane.library import DeleteTally, ancestor_dirs, delete_target, prune_empty_dirs
from amane.library import delete as delete_module

if TYPE_CHECKING:
    from pathlib import Path


def _delete(path: Path, library_root: Path, tally: DeleteTally) -> DeleteTally:
    tally.record(delete_target.sync(path, library_root=library_root))
    return tally


class TestDeleteTarget:
    def test_delete_file(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        lib.mkdir()
        target = lib / "a.mkv"
        target.write_bytes(b"0123456789")

        tally = _delete(target, lib, DeleteTally())

        assert not target.exists()
        assert (tally.deleted, tally.changed, tally.failed) == (1, 0, 0)
        assert tally.freed_bytes == 10
        assert tally.hardlink_items == 0

    def test_missing_target_is_changed(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        lib.mkdir()

        tally = _delete(lib / "gone.mkv", lib, DeleteTally())

        assert (tally.deleted, tally.changed, tally.failed) == (0, 1, 0)
        assert tally.freed_bytes == 0

    def test_delete_directory_removes_contents(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        (lib / "work" / "sub").mkdir(parents=True)
        (lib / "work" / "a.mkv").write_bytes(b"aaaa")
        (lib / "work" / "sub" / "b.srt").write_bytes(b"bb")

        tally = _delete(lib / "work", lib, DeleteTally())

        assert not (lib / "work").exists()
        assert (tally.deleted, tally.failed) == (1, 0)
        assert tally.freed_bytes == 6

    def test_broken_symlink_removes_link_only(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        lib.mkdir()
        link = lib / "dangling.mkv"
        link.symlink_to(lib / "nowhere.mkv")

        tally = _delete(link, lib, DeleteTally())

        assert not link.exists(follow_symlinks=False)
        assert tally.deleted == 1

    def test_symlinked_directory_keeps_target(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        outside = tmp_path / "outside"
        lib.mkdir()
        outside.mkdir()
        (outside / "keep.mkv").write_bytes(b"keep")
        link = lib / "linked"
        link.symlink_to(outside, target_is_directory=True)

        tally = _delete(link, lib, DeleteTally())

        assert not link.exists(follow_symlinks=False)
        assert (outside / "keep.mkv").read_bytes() == b"keep"
        assert tally.deleted == 1
        assert tally.freed_bytes == 0

    def test_recursion_does_not_follow_symlinked_directory(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        outside = tmp_path / "outside"
        (lib / "work").mkdir(parents=True)
        outside.mkdir()
        (outside / "keep.mkv").write_bytes(b"keep")
        (lib / "work" / "linked").symlink_to(outside, target_is_directory=True)
        (lib / "work" / "a.mkv").write_bytes(b"aaaa")

        tally = _delete(lib / "work", lib, DeleteTally())

        assert not (lib / "work").exists()
        assert (outside / "keep.mkv").read_bytes() == b"keep"
        assert tally.freed_bytes == 4

    def test_target_through_directory_alias_refused(self, tmp_path: Path) -> None:
        """经目录链接落到库外的文件不是库内目标: 字面路径在库内, 真实路径在库外 (Windows 的 junction 同理)."""
        lib = tmp_path / "lib"
        outside = tmp_path / "outside"
        lib.mkdir()
        outside.mkdir()
        (outside / "secret.nfo").write_bytes(b"secret")
        (lib / "linked").symlink_to(outside, target_is_directory=True)

        tally = _delete(lib / "linked" / "secret.nfo", lib, DeleteTally())

        assert (outside / "secret.nfo").read_bytes() == b"secret"
        assert (tally.deleted, tally.failed) == (0, 1)

    def test_symlink_to_outside_file_removes_link_only(self, tmp_path: Path) -> None:
        """符号链接仍只删链接自身: 真实路径复核不适用于链接."""
        lib = tmp_path / "lib"
        outside = tmp_path / "outside"
        lib.mkdir()
        outside.mkdir()
        target = outside / "keep.mkv"
        target.write_bytes(b"keep")
        link = lib / "entry.mkv"
        link.symlink_to(target)

        tally = _delete(link, lib, DeleteTally())

        assert not link.exists(follow_symlinks=False)
        assert target.read_bytes() == b"keep"
        assert (tally.deleted, tally.failed) == (1, 0)

    def test_alias_library_root_still_deletes_inside(self, tmp_path: Path) -> None:
        """库路径自身是别名 (旧库未写入真实路径) 时, 库内目标不因两次真实路径解析而误拒."""
        real = tmp_path / "real"
        (real / "work").mkdir(parents=True)
        (real / "work" / "a.nfo").write_bytes(b"a")
        alias = tmp_path / "alias"
        alias.symlink_to(real, target_is_directory=True)

        tally = _delete(alias / "work" / "a.nfo", alias, DeleteTally())

        assert not (real / "work" / "a.nfo").exists()
        assert tally.deleted == 1

    def test_outside_library_refused(self, tmp_path: Path) -> None:
        """库根外没有例外: 链接树里的产物与同目录邻居一律拒绝."""
        lib = tmp_path / "lib"
        outside = tmp_path / "linktree"
        lib.mkdir()
        outside.mkdir()
        target = outside / "poster.jpg"
        neighbor = outside / "fanart.jpg"
        target.write_bytes(b"poster")
        neighbor.write_bytes(b"fanart")

        tally = _delete(target, lib, DeleteTally())
        tally = _delete(neighbor, lib, tally)

        assert (target.read_bytes(), neighbor.read_bytes()) == (b"poster", b"fanart")
        assert (tally.deleted, tally.failed) == (0, 2)

    def test_ok_dir_file_deleted(self, tmp_path: Path) -> None:
        """成品目录在库根之外 (刮削后转移): 那里的条目同样可删, 否则转移过的文件永远删不掉."""
        lib, done = tmp_path / "lib", tmp_path / "done"
        lib.mkdir()
        target = done / "ABC-123" / "ABC-123.mp4"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"0123456789")

        tally = DeleteTally()
        tally.record(delete_target.sync(target, library_root=lib, bases=(done,)))

        assert not target.exists()
        assert (tally.deleted, tally.failed) == (1, 0)
        assert tally.freed_bytes == 10

    def test_ok_dir_itself_refused(self, tmp_path: Path) -> None:
        """授权根本身不删: 与库根同理, 目标是里面的若干条目."""
        lib, done = tmp_path / "lib", tmp_path / "done"
        lib.mkdir()
        done.mkdir()
        (done / "ABC-123.mp4").write_bytes(b"x")

        outcome = delete_target.sync(done, library_root=lib, bases=(done,))

        assert outcome.status == "failed"
        assert "publish root" in (outcome.error or "")
        assert (done / "ABC-123.mp4").exists()

    def test_outside_extra_bases_refused(self, tmp_path: Path) -> None:
        """认下成品目录不等于放开库外: 这些根之外的路径仍然拒绝."""
        lib, done, other = tmp_path / "lib", tmp_path / "done", tmp_path / "other"
        lib.mkdir()
        done.mkdir()
        other.mkdir()
        target = other / "poster.jpg"
        target.write_bytes(b"poster")

        tally = DeleteTally()
        tally.record(delete_target.sync(target, library_root=lib, bases=(done,)))

        assert target.read_bytes() == b"poster"
        assert (tally.deleted, tally.failed) == (0, 1)

    def test_library_root_refused(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        lib.mkdir()
        (lib / "a.mkv").write_bytes(b"a")

        outcome = delete_target.sync(lib, library_root=lib)

        assert outcome.status == "failed"
        assert "library root" in (outcome.error or "")
        assert (lib / "a.mkv").exists()

    def test_trash_root_refused(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        trash = lib / ".amane_trash"
        trash.mkdir(parents=True)
        (trash / "old.mkv").write_bytes(b"old")

        tally = _delete(trash, lib, DeleteTally())

        assert (trash / "old.mkv").exists()
        assert tally.failed == 1

    def test_trash_contents_deleted(self, tmp_path: Path) -> None:
        """只拒绝回收站目录自身; 其下条目由回收站面板处置."""
        lib = tmp_path / "lib"
        trash = lib / ".amane_trash"
        trash.mkdir(parents=True)
        old = trash / "old.mkv"
        old.write_bytes(b"old")

        tally = _delete(old, lib, DeleteTally())

        assert not old.exists()
        assert trash.is_dir()
        assert tally.deleted == 1

    def test_nfd_name_deleted_via_nfc_path(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        lib.mkdir()
        name = unicodedata.normalize("NFD", "がくえん.mkv")
        on_disk = lib / name
        on_disk.write_bytes(b"x")

        tally = _delete(lib / unicodedata.normalize("NFC", name), lib, DeleteTally())

        assert not on_disk.exists()
        assert tally.deleted == 1

    def test_nested_mount_boundary_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        lib = tmp_path / "lib"
        (lib / "work" / "mounted").mkdir(parents=True)
        (lib / "work" / "a.mkv").write_bytes(b"aaaa")
        (lib / "work" / "mounted" / "outside.mkv").write_bytes(b"outside")
        mounted_ino = (lib / "work" / "mounted").stat().st_ino
        monkeypatch.setattr(delete_module, "_crosses_boundary", lambda st, *, dev: st.st_ino == mounted_ino)

        tally = _delete(lib / "work", lib, DeleteTally())

        assert (lib / "work" / "mounted" / "outside.mkv").read_bytes() == b"outside"
        assert (tally.deleted, tally.failed) == (0, 1)

    def test_mount_point_target_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        lib = tmp_path / "lib"
        (lib / "mounted").mkdir(parents=True)
        (lib / "mounted" / "a.mkv").write_bytes(b"aaaa")
        monkeypatch.setattr(delete_module, "_crosses_boundary", lambda st, *, dev: True)

        tally = _delete(lib / "mounted", lib, DeleteTally())

        assert (lib / "mounted" / "a.mkv").exists()
        assert (tally.deleted, tally.failed) == (0, 1)


class TestDeleteTally:
    def test_freed_bytes_requires_all_hardlinks(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        lib.mkdir()
        first = lib / "a.mkv"
        second = lib / "a(1).mkv"
        first.write_bytes(b"0123456789")
        os.link(first, second)

        partial = _delete(first, lib, DeleteTally())
        assert partial.freed_bytes == 0
        assert partial.hardlink_items == 1

        both = _delete(first, lib, _delete(second, lib, DeleteTally()))
        assert both.hardlink_items == 0
        assert both.freed_bytes == 10

    def test_failed_target_keeps_removed_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """目录删除中途失败: 已删文件仍计入统计, 目标记失败."""
        lib = tmp_path / "lib"
        (lib / "work").mkdir(parents=True)
        (lib / "work" / "a.mkv").write_bytes(b"aaaa")
        (lib / "work" / "b.mkv").write_bytes(b"bb")
        calls = 0
        real_rmdir = os.rmdir

        def flaky_rmdir(path: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError(errno.EBUSY, "busy")
            real_rmdir(path)

        monkeypatch.setattr(os, "rmdir", flaky_rmdir)

        tally = _delete(lib / "work", lib, DeleteTally())

        assert (tally.deleted, tally.failed) == (0, 1)
        assert tally.freed_bytes == 6


class TestAncestorDirs:
    @pytest.mark.parametrize(
        ("relative", "expected"),
        [
            ("incoming/a.mkv", ["incoming"]),
            ("incoming/deep/a.mkv", ["incoming/deep", "incoming"]),
            ("a.mkv", []),
            (".amane_trash/sub/a.mkv", [".amane_trash/sub"]),
            ("../outside/a.mkv", []),
        ],
    )
    def test_ancestors(self, tmp_path: Path, relative: str, expected: list[str]) -> None:
        lib = tmp_path / "lib"
        lib.mkdir()

        got = ancestor_dirs(lib / relative, library_root=lib)

        assert [path.relative_to(lib).as_posix() for path in got] == expected

    def test_library_root_has_no_ancestors(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        lib.mkdir()

        assert ancestor_dirs(lib, library_root=lib) == []

    def test_extra_bases_are_prunable(self, tmp_path: Path) -> None:
        """库根之外的授权根 (刮削输入目录) 之下同样成链, 但不含根本身."""
        lib = tmp_path / "lib"
        lib.mkdir()
        incoming = tmp_path / "in"
        (incoming / "a" / "b").mkdir(parents=True)

        got = ancestor_dirs(incoming / "a" / "b" / "x.mkv", library_root=lib, bases=(incoming,))

        assert [path.relative_to(incoming).as_posix() for path in got] == ["a/b", "a"]

    def test_extra_base_itself_is_not_pruned(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        lib.mkdir()
        incoming = tmp_path / "in"
        incoming.mkdir()

        assert ancestor_dirs(incoming, library_root=lib, bases=(incoming,)) == []


class TestPruneEmptyDirs:
    def test_removes_chain_bottom_up(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        deep = lib / "incoming" / "deep"
        deep.mkdir(parents=True)
        candidates = ancestor_dirs(lib / "incoming" / "deep" / "a.mkv", library_root=lib)

        result = prune_empty_dirs.sync(candidates, library_root=lib)

        assert result.removed == 2
        assert not (lib / "incoming").exists()

    def test_extra_bases_are_pruned_up_to_the_root(self, tmp_path: Path) -> None:
        """输入目录空掉后整条链删到根本身, 根留着等下一批下载进来."""
        lib = tmp_path / "lib"
        lib.mkdir()
        incoming = tmp_path / "in"
        deep = incoming / "a" / "b"
        deep.mkdir(parents=True)
        candidates = ancestor_dirs(deep / "x.mkv", library_root=lib, bases=(incoming,))

        result = prune_empty_dirs.sync(candidates, library_root=lib, bases=(incoming,))

        assert result.removed == 2
        assert incoming.exists()
        assert not (incoming / "a").exists()

    def test_non_empty_directory_kept(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        (lib / "incoming").mkdir(parents=True)
        (lib / "incoming" / "keep.mkv").write_bytes(b"keep")

        result = prune_empty_dirs.sync([lib / "incoming"], library_root=lib)

        assert result.removed == 0
        assert result.failed == 0
        assert (lib / "incoming").is_dir()

    def test_only_passed_directories_removed(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        (lib / "incoming" / "deep").mkdir(parents=True)

        result = prune_empty_dirs.sync([lib / "incoming" / "deep"], library_root=lib)

        assert result.removed == 1
        assert (lib / "incoming").is_dir()

    def test_root_and_trash_kept(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        trash = lib / ".amane_trash"
        trash.mkdir(parents=True)

        result = prune_empty_dirs.sync([lib, trash], library_root=lib)

        assert result.removed == 0
        assert lib.is_dir()
        assert trash.is_dir()

    def test_missing_directory_ignored(self, tmp_path: Path) -> None:
        lib = tmp_path / "lib"
        lib.mkdir()

        result = prune_empty_dirs.sync([lib / "gone"], library_root=lib)

        assert (result.removed, result.failed) == (0, 0)

    def test_other_errors_counted_as_failed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        lib = tmp_path / "lib"
        (lib / "incoming").mkdir(parents=True)

        def denied(path: str) -> None:
            raise OSError(errno.EACCES, "denied")

        monkeypatch.setattr(os, "rmdir", denied)

        result = prune_empty_dirs.sync([lib / "incoming"], library_root=lib)

        assert (result.removed, result.failed) == (0, 1)
        assert (lib / "incoming").is_dir()


class TestTrashSubtreeGuard:
    def test_recursive_delete_refuses_trash_subtree(self, tmp_path: Path) -> None:
        """目录删除不深入 .amane_trash: 回收站不能被子目录的整目录删除顺手清掉."""
        lib = tmp_path / "lib"
        work = lib / "work"
        trash = work / ".amane_trash"
        trash.mkdir(parents=True)
        (trash / "old.mkv").write_bytes(b"old")
        (work / "a.mkv").write_bytes(b"aaaa")

        tally = _delete(work, lib, DeleteTally())

        assert (tally.deleted, tally.failed) == (0, 1)
        assert (trash / "old.mkv").read_bytes() == b"old"
        assert work.is_dir()


class TestWashTrashBoundary:
    """洗版回收站挂在库根父目录, 直指它内部的文件也要拒删."""

    def test_wash_trash_outside_library_root_is_refused(self, tmp_path) -> None:
        library_root = tmp_path / "library"
        library_root.mkdir()
        wash_dir = tmp_path / ".amane_wash" / "library" / "in" / "ABC-123"
        wash_dir.mkdir(parents=True)
        backup = wash_dir / "ABC-123-1080p.mkv"
        backup.write_bytes(b"video")

        outcome = delete_target.sync(backup, library_root=library_root)

        assert outcome.status == "failed"
        assert outcome.error is not None and "wash trash" in outcome.error
        assert backup.exists()

    def test_wash_trash_directory_itself_is_refused(self, tmp_path) -> None:
        library_root = tmp_path / "library"
        library_root.mkdir()
        wash_dir = tmp_path / ".amane_wash" / "library"
        wash_dir.mkdir(parents=True)

        outcome = delete_target.sync(wash_dir, library_root=library_root)

        assert outcome.status == "failed"
        assert outcome.error is not None and "wash trash" in outcome.error
        assert wash_dir.is_dir()
