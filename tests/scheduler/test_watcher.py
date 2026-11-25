"""FileWatcher 和 WatcherService 测试"""

import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileMovedEvent,
)
from watchdog.observers.polling import PollingObserver

from amane.db.repository import Repository
from amane.enums import LibraryAutomation
from amane.events import EventBus
from amane.library import LibraryScan
from amane.scheduler.service import WatcherService
from amane.scheduler.watcher import DEBOUNCE_SECONDS, FileWatcher, _Handler
from amane.utils.path import path_is_under
from tests.helpers import LOAD_TOLERANT_TIMEOUT, await_for, wait_for


class TestHandler:
    """内部 _Handler 类的测试"""

    def test_matches_media_extensions(self):
        handler = _Handler(library_id=1, root="/lib")
        assert handler._matches(Path("/tmp/video.mp4")) is True
        assert handler._matches(Path("/tmp/video.mkv")) is True
        assert handler._matches(Path("/tmp/video.avi")) is True
        assert handler._matches(Path("/tmp/video.txt")) is False
        assert handler._matches(Path("/tmp/video.py")) is False

    def test_ignores_download_in_progress(self):
        handler = _Handler(library_id=1, root="/lib")
        assert handler._matches(Path("/tmp/video.mp4.!qB")) is False
        assert handler._matches(Path("/tmp/video.mp4.part")) is False

    def test_skips_trailer_pattern(self):
        handler = _Handler(library_id=1, root="/lib", scan=LibraryScan(trailer_pattern="(?i)trailer"))
        assert handler._matches(Path("/tmp/trailer.mp4")) is False
        assert handler._matches(Path("/tmp/video.mp4")) is True
        assert handler._matches(Path("/tmp/中文预告片.mp4")) is True  # (?i)trailer 不匹配预告

    def test_skips_undersized_video(self, tmp_path: Path):
        """大小阈值只跳过真实存在的小视频; 缺失路径不因 stat 失败被挡 (删除事件仍匹配)."""
        small = tmp_path / "ad.mp4"
        small.write_bytes(b"tiny")
        large = tmp_path / "film.mp4"
        large.write_bytes(b"x" * 100)
        nfo = tmp_path / "note.nfo"
        nfo.write_bytes(b"nfo")
        handler = _Handler(library_id=1, root="/lib", scan=LibraryScan(min_file_size=50))
        assert handler._matches(small) is False
        assert handler._matches(large) is True
        assert handler._matches(nfo) is False
        missing = tmp_path / "gone.mp4"
        assert handler._matches(missing) is True

    def test_skips_custom_preview_name(self):
        handler = _Handler(library_id=1, root="/lib", scan=LibraryScan(trailer_pattern="预告"))
        assert handler._matches(Path("/tmp/中文预告片.mp4")) is False
        assert handler._matches(Path("/tmp/video.mp4")) is True

    def test_skips_any_blacklist_pattern(self):
        """多个跳过正则任一命中即忽略 (预告片 + 文件黑名单组合)."""
        handler = _Handler(library_id=1, root="/lib", scan=LibraryScan(blacklist_patterns=["广告", "(?i)ads"]))
        assert handler._matches(Path("/tmp/新片广告.mp4")) is False
        assert handler._matches(Path("/tmp/ADS_01.mkv")) is False
        assert handler._matches(Path("/tmp/video.mp4")) is True

    def test_trash_dir_always_ignored(self):
        """.amane_trash 内路径恒不匹配 (即使文件名是正常影片)."""
        handler = _Handler(library_id=1, root="/lib")
        assert handler._matches(Path("/lib/.amane_trash/video.mp4")) is False
        assert handler._matches(Path("/lib/.amane_trash/sub/ad.mp4")) is False

    def test_move_into_trash_records_src_delete(self):
        """把文件移入 .amane_trash: dest 不匹配 → 记录 src 为删除 (记录清理/回收竞态安全)."""
        handler = _Handler(library_id=1, root="/lib")
        handler.on_moved(FileMovedEvent(src_path="/lib/incoming/ad.mp4", dest_path="/lib/.amane_trash/ad.mp4"))
        assert "/lib/incoming/ad.mp4" in handler._pending_deletes
        assert handler._pending_moves == {}

    def test_move_out_of_trash_triggers_found(self):
        """从 .amane_trash 恢复文件 (移出): dest 匹配 → 记录为移动 (重新入库)."""
        handler = _Handler(library_id=1, root="/lib")
        handler.on_moved(FileMovedEvent(src_path="/lib/.amane_trash/video.mp4", dest_path="/lib/video.mp4"))
        assert "/lib/video.mp4" in handler._pending_moves

    def test_matches_custom_patterns(self):
        handler = _Handler(library_id=1, root="/lib", scan=LibraryScan(patterns=["*.mp4", "*.mkv"]))
        assert handler._matches(Path("/tmp/video.mp4")) is True
        assert handler._matches(Path("/tmp/video.mkv")) is True
        assert handler._matches(Path("/tmp/video.avi")) is False

    def test_debounce_not_ready_immediately(self):
        handler = _Handler(library_id=1, root="/lib")
        handler._handle("/tmp/video.mp4")
        # 不应立即就绪
        ready = handler.get_ready_files()
        assert ready == []

    def test_debounce_ready_after_timeout(self):
        handler = _Handler(library_id=1, root="/lib")
        handler._handle("/tmp/video.mp4")
        # 模拟时间流逝
        handler._pending["/tmp/video.mp4"] = time.time() - DEBOUNCE_SECONDS - 1
        ready = handler.get_ready_files()
        assert len(ready) == 1
        assert ready[0] == Path("/tmp/video.mp4")
        # 获取后应被清除
        assert handler.get_ready_files() == []

    def test_settle_waits_for_size_to_stop_changing(self, tmp_path: Path):
        """体积稳定够久才放行: 下载器直写库目录时, 半截文件不能被当成成品."""
        media = tmp_path / "growing.mp4"
        media.write_bytes(b"x" * 10)
        handler = _Handler(library_id=1, root=str(tmp_path), debounce_seconds=0.01, settle_seconds=0.2)

        handler.on_created(FileCreatedEvent(str(media)))
        time.sleep(0.05)
        assert handler.get_ready_files() == []  # 首次见到只记体积, 不放行

        media.write_bytes(b"x" * 2000)  # 还在写: 体积变了, 重新计时
        time.sleep(0.05)
        assert handler.get_ready_files() == []

        time.sleep(0.25)  # 体积一直没变
        assert handler.get_ready_files() == [media]

    def test_settle_disabled_releases_after_debounce(self, tmp_path: Path):
        media = tmp_path / "done.mp4"
        media.write_bytes(b"x" * 100)
        handler = _Handler(library_id=1, root=str(tmp_path), debounce_seconds=0.01, settle_seconds=0)

        handler.on_created(FileCreatedEvent(str(media)))
        time.sleep(0.05)
        assert handler.get_ready_files() == [media]

    def test_settle_releases_when_file_disappears(self, tmp_path: Path):
        """文件在等待期间被删掉: 不在这一步拦, 交给后续注册流程报错."""
        media = tmp_path / "vanishing.mp4"
        media.write_bytes(b"x" * 100)
        handler = _Handler(library_id=1, root=str(tmp_path), debounce_seconds=0.01, settle_seconds=30)

        handler.on_created(FileCreatedEvent(str(media)))
        time.sleep(0.05)
        assert handler.get_ready_files() == []
        media.unlink()
        time.sleep(0.05)
        assert handler.get_ready_files() == [media]

    def test_ignores_non_media_files(self):
        handler = _Handler(library_id=1, root="/lib")
        handler._handle("/tmp/readme.txt")
        # txt 未被添加到 pending 因为 _handle 通过 _matches 过滤
        assert handler._pending == {}
        assert handler.get_ready_files() == []

    def test_on_created_triggers_handle(self):
        handler = _Handler(library_id=1, root="/lib")

        handler.on_created(FileCreatedEvent(src_path="/tmp/video.mp4"))
        assert "/tmp/video.mp4" in handler._pending

    def test_on_created_ignores_directories(self):
        handler = _Handler(library_id=1, root="/lib")

        # DirCreatedEvent has is_directory=True
        handler.on_created(DirCreatedEvent(src_path="/tmp/somedir"))
        assert handler._pending == {}

    def test_on_moved_triggers_handle(self):
        handler = _Handler(library_id=1, root="/lib")

        handler.on_moved(FileMovedEvent(src_path="/tmp/old.mkv", dest_path="/tmp/movie.mkv"))
        assert "/tmp/movie.mkv" in handler._pending_moves

    def test_on_deleted_file_records_prefix(self):
        handler = _Handler(library_id=1, root="/lib")
        handler.on_deleted(FileDeletedEvent(src_path="/tmp/video.mp4"))
        assert "/tmp/video.mp4" in handler._pending_dir_deletes
        assert handler._pending_deletes == {}

    def test_on_deleted_non_media_file_records_prefix(self):
        handler = _Handler(library_id=1, root="/lib")
        handler.on_deleted(FileDeletedEvent(src_path="/tmp/notes.txt"))
        assert "/tmp/notes.txt" in handler._pending_dir_deletes
        assert handler._pending_deletes == {}

    def test_on_deleted_directory_records_prefix(self):
        handler = _Handler(library_id=1, root="/lib")
        handler.on_created(FileCreatedEvent(src_path="/lib/show/a.mp4"))
        handler.on_deleted(DirDeletedEvent(src_path="/lib/show"))
        assert "/lib/show" in handler._pending_dir_deletes
        assert "/lib/show/a.mp4" not in handler._pending
        assert handler._pending_deletes == {}

    def test_on_deleted_directory_keeps_sibling_prefix(self):
        handler = _Handler(library_id=1, root="/lib")
        handler.on_created(FileCreatedEvent(src_path="/lib/show2/a.mp4"))
        handler.on_deleted(DirDeletedEvent(src_path="/lib/show"))
        assert "/lib/show2/a.mp4" in handler._pending
        assert "/lib/show" in handler._pending_dir_deletes

    def test_on_deleted_directory_ignores_trash(self):
        handler = _Handler(library_id=1, root="/lib")
        handler.on_deleted(DirDeletedEvent(src_path="/lib/.amane_trash/old"))
        assert handler._pending_dir_deletes == {}

    def test_on_deleted_ignores_watch_root(self):
        """库根自身的删除事件不按前缀删除索引."""
        handler = _Handler(library_id=1, root="/lib")
        handler.on_created(FileCreatedEvent(src_path="/lib/show/a.mp4"))
        handler.on_deleted(DirDeletedEvent(src_path="/lib"))
        assert handler._pending_dir_deletes == {}
        assert "/lib/show/a.mp4" in handler._pending

    def test_on_deleted_ignores_watch_root_with_separator(self):
        """根路径写法带尾分隔符时仍算同一个根."""
        handler = _Handler(library_id=1, root="/lib/")
        handler.on_deleted(DirDeletedEvent(src_path="/lib"))
        assert handler._pending_dir_deletes == {}

    def test_on_deleted_nested_dir_skipped_if_parent_pending(self):
        handler = _Handler(library_id=1, root="/lib")
        handler.on_deleted(DirDeletedEvent(src_path="/lib/show"))
        handler.on_deleted(DirDeletedEvent(src_path="/lib/show/nested"))
        assert list(handler._pending_dir_deletes) == ["/lib/show"]

    def test_on_deleted_parent_dir_replaces_nested(self):
        handler = _Handler(library_id=1, root="/lib")
        handler.on_deleted(DirDeletedEvent(src_path="/lib/show/nested"))
        handler.on_deleted(DirDeletedEvent(src_path="/lib/show"))
        assert "/lib/show" in handler._pending_dir_deletes
        assert "/lib/show/nested" not in handler._pending_dir_deletes

    def test_on_moved_directory_ignored(self):
        handler = _Handler(library_id=1, root="/lib")
        handler.on_moved(DirMovedEvent(src_path="/lib/show", dest_path="/lib/renamed"))
        assert handler._pending_dir_deletes == {}
        assert handler._pending_moves == {}

    def test_on_deleted_directory_drops_pending_move(self):
        handler = _Handler(library_id=1, root="/lib")
        handler.on_moved(FileMovedEvent(src_path="/lib/show/old.mp4", dest_path="/lib/show/new.mp4"))
        handler.on_deleted(DirDeletedEvent(src_path="/lib/show"))
        assert handler._pending_moves == {}
        assert "/lib/show" in handler._pending_dir_deletes

    def test_on_deleted_file_event_is_prefix(self):
        handler = _Handler(library_id=1, root="/lib")
        handler.on_created(FileCreatedEvent(src_path="/lib/show/a.mp4"))
        handler.on_deleted(FileDeletedEvent(src_path="/lib/show"))
        assert "/lib/show" in handler._pending_dir_deletes
        assert "/lib/show/a.mp4" not in handler._pending

    def test_on_deleted_file_event_dotted_dir_name_is_prefix(self):
        """Windows 对目录发 FileDeletedEvent; 目录名带点仍按前缀."""
        handler = _Handler(library_id=1, root="/lib")
        handler.on_created(FileCreatedEvent(src_path="/lib/Season1.mkv/a.mp4"))
        handler.on_deleted(FileDeletedEvent(src_path="/lib/Season1.mkv"))
        assert "/lib/Season1.mkv" in handler._pending_dir_deletes
        assert "/lib/Season1.mkv/a.mp4" not in handler._pending

    def test_on_deleted_removes_from_pending_creates(self):
        """文件创建后立即删除: 从 pending 中移除, 记为前缀删除."""
        handler = _Handler(library_id=1, root="/lib")
        handler.on_created(FileCreatedEvent(src_path="/tmp/video.mp4"))
        assert "/tmp/video.mp4" in handler._pending
        handler.on_deleted(FileDeletedEvent(src_path="/tmp/video.mp4"))
        assert "/tmp/video.mp4" not in handler._pending
        assert "/tmp/video.mp4" in handler._pending_dir_deletes
        assert handler._pending_deletes == {}

    def test_get_ready_deletes_after_debounce(self):
        handler = _Handler(library_id=1, root="/lib")
        handler._pending_deletes["/tmp/video.mp4"] = time.time() - DEBOUNCE_SECONDS - 1
        ready = handler.get_ready_deletes()
        assert len(ready) == 1
        assert ready[0] == Path("/tmp/video.mp4")
        assert handler.get_ready_deletes() == []

    def test_on_moved_records_src_as_delete_target(self):
        """移动到非媒体路径: src 记录为删除"""
        handler = _Handler(library_id=1, root="/lib")
        handler.on_moved(FileMovedEvent(src_path="/tmp/video.mp4", dest_path="/tmp/video.txt"))
        assert "/tmp/video.mp4" in handler._pending_deletes
        assert handler._pending_moves == {}

    def test_get_ready_moves_after_debounce(self):
        handler = _Handler(library_id=1, root="/lib")
        handler._pending_moves["/tmp/new.mp4"] = ("/tmp/old.mp4", time.time() - DEBOUNCE_SECONDS - 1)
        ready = handler.get_ready_moves()
        assert len(ready) == 1
        assert ready[0] == (Path("/tmp/old.mp4"), Path("/tmp/new.mp4"))
        assert handler.get_ready_moves() == []


class TestFileWatcher:
    """FileWatcher 类的测试"""

    def test_watch_creates_observer(self, tmp_path: Path):
        watcher = FileWatcher(observer_timeout=0.1, on_file_found=lambda p, _lib: None)
        watcher.watch(str(tmp_path), library_id=1)
        assert watcher._observer is not None
        assert len(watcher._handlers) == 1

    def test_watch_polling_mode(self, tmp_path: Path):
        watcher = FileWatcher(observer_timeout=0.1, on_file_found=lambda p, _lib: None, use_polling=True)
        watcher.watch(str(tmp_path), library_id=1)
        assert isinstance(watcher._observer, PollingObserver)

    def test_start_stop_lifecycle(self, tmp_path: Path):
        watcher = FileWatcher(observer_timeout=0.1, on_file_found=lambda p, _lib: None)
        watcher.watch(str(tmp_path), library_id=1)
        watcher.start()
        assert watcher.is_running
        watcher.stop()
        assert not watcher.is_running

    def test_check_debounced_calls_callback(self, tmp_path: Path):
        received = []
        watcher = FileWatcher(observer_timeout=0.1, on_file_found=lambda p, _lib: received.append(p))
        watcher.watch(str(tmp_path), library_id=1)

        # 手动注入一个已过 debounce 的 pending 文件
        handler = watcher._handlers[0]
        test_file = str(tmp_path / "test.mp4")
        handler._pending[test_file] = time.time() - DEBOUNCE_SECONDS - 1

        ready = watcher.check_debounced()
        assert len(ready) == 1
        assert ready[0] == Path(test_file)
        assert len(received) == 1
        assert received[0] == Path(test_file)

    def test_check_debounced_dir_delete_callback(self, tmp_path: Path):
        deleted: list[tuple[Path, int]] = []
        watcher = FileWatcher(
            observer_timeout=0.1,
            on_file_found=lambda p, _lib: None,
            on_dir_deleted=lambda p, lib: deleted.append((p, lib)),
        )
        watcher.watch(str(tmp_path), library_id=1)
        handler = watcher._handlers[0]
        target = tmp_path / "show"
        handler._pending_dir_deletes[str(target)] = time.time() - DEBOUNCE_SECONDS - 1
        watcher.check_debounced()
        assert deleted == [(target, 1)]

    def test_multiple_watch_paths(self, tmp_path: Path):
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()

        watcher = FileWatcher(observer_timeout=0.1, on_file_found=lambda p, _lib: None)
        watcher.watch(str(dir1), library_id=1)
        watcher.watch(str(dir2), library_id=2, patterns=["*.mkv"])
        assert len(watcher._handlers) == 2

    @pytest.mark.parametrize("use_polling", [True, False])
    def test_real_file_detection(self, tmp_path: Path, use_polling: bool):
        """集成测试: 创建文件并验证 watcher 能检测到 (两种策略)"""
        received = []
        watcher = FileWatcher(
            observer_timeout=0.1, on_file_found=lambda p, _lib: received.append(p), use_polling=use_polling
        )
        watcher.watch(str(tmp_path), library_id=1)
        watcher.start()

        try:
            # 创建媒体文件
            test_file = tmp_path / "new_video.mp4"
            test_file.write_bytes(b"\x00" * 1024)

            # 文件应在 pending 中但尚未就绪 (debounce)
            handler = watcher._handlers[0]
            wait_for(lambda: str(test_file) in handler._pending)

            # 强制跳过 debounce
            handler._pending[str(test_file)] = time.time() - DEBOUNCE_SECONDS - 1
            ready = watcher.check_debounced()
            assert len(ready) == 1
            assert ready[0] == test_file
        finally:
            watcher.stop()

    def test_is_running_false_before_start(self, tmp_path: Path):
        watcher = FileWatcher(observer_timeout=0.1, on_file_found=lambda p, _lib: None)
        watcher.watch(str(tmp_path), library_id=1)
        assert not watcher.is_running

    def test_unwatch_removes_handler(self, tmp_path: Path):
        dir1 = tmp_path / "d1"
        dir2 = tmp_path / "d2"
        dir1.mkdir()
        dir2.mkdir()
        watcher = FileWatcher(observer_timeout=0.1, on_file_found=lambda p, _lib: None)
        watcher.watch(str(dir1), library_id=1)
        watcher.watch(str(dir2), library_id=2)
        assert len(watcher._handlers) == 2

        watcher.unwatch(1)
        assert [h.library_id for h in watcher._handlers] == [2]
        assert 1 not in watcher._watches

    def test_unwatch_unknown_is_noop(self, tmp_path: Path):
        watcher = FileWatcher(observer_timeout=0.1, on_file_found=lambda p, _lib: None)
        watcher.watch(str(tmp_path), library_id=1)
        watcher.unwatch(999)  # 不应抛出
        assert len(watcher._handlers) == 1

    def test_unwatch_while_running(self, tmp_path: Path):
        watcher = FileWatcher(observer_timeout=0.1, on_file_found=lambda p, _lib: None)
        watcher.watch(str(tmp_path), library_id=1)
        watcher.start()
        try:
            watcher.unwatch(1)
            assert watcher._handlers == []
        finally:
            watcher.stop()

    @pytest.mark.skipif(sys.platform != "win32", reason="原生目录句柄只在 Windows 存在")
    def test_native_handle_closed_only_by_emitter_thread(self, tmp_path: Path, monkeypatch):
        """句柄只由 emitter 线程关闭且只关一次: 停止方并发关闭会让该线程在失效句柄上崩溃."""
        from amane.scheduler import watcher_win  # 只在 Windows 可导入

        closed: list[threading.Thread] = []
        close = watcher_win.close_directory_handle

        def record(handle):
            closed.append(threading.current_thread())
            close(handle)

        monkeypatch.setattr(watcher_win, "close_directory_handle", record)
        watcher = FileWatcher(observer_timeout=0.1, on_file_found=lambda p, _lib: None)
        watcher.watch(str(tmp_path), library_id=1)
        watcher.start()
        watcher.unwatch(1)
        watcher.stop()

        assert len(closed) == 1
        assert closed[0] is not threading.main_thread()


class TestWatcherService:
    """WatcherService 编排测试

    ``repo`` 用 `tests/conftest.py` 的文件库: 去抖回调是与用例并发执行的独立任务,
    内存库 (StaticPool) 让两者的会话共用同一个连接, 会互相提交或回滚对方的事务.
    """

    @pytest.fixture
    def bus(self):
        return EventBus()

    @pytest.fixture
    def service(self, repo: Repository, bus):
        return WatcherService(repo, bus, use_polling=True, check_interval=0.05, observer_timeout=0.1)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_start_without_watch_paths(self, service):
        """无监控路径配置时 service 不应启动 watcher"""
        await service.start()
        assert not service.is_running

    @pytest.mark.asyncio(loop_scope="function")
    async def test_start_with_watch_paths(self, service, repo: Repository, tmp_path: Path):
        """存在监控路径时 service 应启动"""
        await repo.create_library(name="t", path=str(tmp_path), automation=LibraryAutomation.SCRAPE)
        await service.start()
        assert service.is_running
        await service.stop()
        assert not service.is_running

    @pytest.mark.asyncio(loop_scope="function")
    async def test_start_watches_pending_dir_of_transfer_library(
        self, service, repo: Repository, tmp_path: Path
    ) -> None:
        """开刮削后转移的库监控输入目录: 库根只收本服务搬进去的成品, 不会出现外部新文件."""
        lib_root, pending = tmp_path / "done", tmp_path / "in"
        lib_root.mkdir()
        pending.mkdir()
        await repo.create_library(
            name="t",
            path=str(lib_root),
            automation=LibraryAutomation.SCRAPE,
            scrape_transfer=True,
            scrape_input_dir=str(pending),
        )

        await service.start()
        assert service.is_running
        try:
            video = pending / "MIDV-990.mp4"
            video.write_bytes(b"\x00" * 100)

            media = await await_for(lambda: repo.get_media_file_by_path(str(video)), timeout=LOAD_TOLERANT_TIMEOUT)

            assert media is not None
        finally:
            await service.stop()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_on_file_found_broadcasts_event(self, service, repo: Repository, bus, tmp_path: Path):
        """验证文件发现时发出事件"""
        lib = await repo.create_library(name="t", path=str(tmp_path))
        assert lib.id is not None
        test_file = tmp_path / "video.mp4"
        test_file.write_bytes(b"\x00" * 100)

        await service._on_file_found(test_file, lib.id)

        media = await repo.get_media_file_by_path(str(test_file))
        assert media is not None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_on_file_found_skips_duplicate(self, service, repo: Repository, bus: EventBus, tmp_path: Path):
        """验证重复文件不会重复广播"""
        lib = await repo.create_library(name="t", path=str(tmp_path))
        assert lib.id is not None
        test_file = tmp_path / "MIDV-123.mp4"
        test_file.write_bytes(b"\x00" * 100)

        await service._on_file_found(test_file, lib.id)
        await service._on_file_found(test_file, lib.id)

        tasks = await repo.list_tasks()
        assert len(tasks) == 1

    @pytest.mark.asyncio(loop_scope="function")
    async def test_on_file_found_watch_only_does_not_scrape(self, service, repo: Repository, tmp_path: Path) -> None:
        """automation=watch 只登记, 不入队 SCRAPE."""
        lib = await repo.create_library(name="t", path=str(tmp_path), automation=LibraryAutomation.WATCH)
        assert lib.id is not None
        test_file = tmp_path / "MIDV-123.mp4"
        test_file.write_bytes(b"\x00" * 100)

        await service._on_file_found(test_file, lib.id)

        media = await repo.get_media_file_by_path(str(test_file))
        assert media is not None
        assert media.number == "MIDV-123"
        assert await repo.list_tasks() == []

    @pytest.mark.asyncio(loop_scope="function")
    async def test_debounce_loop_flushes_ready_files(self, service, repo: Repository, bus, tmp_path: Path):
        """验证 debounce 循环处理通过 debounce 窗口的文件"""
        await repo.create_library(name="t", path=str(tmp_path), automation=LibraryAutomation.SCRAPE)
        await service.start()

        # 注入一个虚假的 pending 路径 (磁盘上非真实文件, 因此
        # polling observer 不会重新检测并覆盖我们的时间戳)
        handler = service._watcher._handlers[0]
        fake_path = str(tmp_path / "phantom.mp4")
        handler._pending[fake_path] = time.time() - DEBOUNCE_SECONDS - 1

        # 去抖回调是 fire-and-forget 任务, 等它把 MediaFile 落库
        media = await await_for(lambda: repo.get_media_file_by_path(fake_path), timeout=LOAD_TOLERANT_TIMEOUT)
        assert media is not None

        await service.stop()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_stop_is_idempotent(self, service):
        """未运行时调用 stop 应是安全的"""
        await service.stop()  # 无操作, 不应抛出异常
        assert not service.is_running

    @pytest.mark.asyncio(loop_scope="function")
    async def test_start_is_idempotent(self, service, repo: Repository, tmp_path: Path):
        """调用两次 start 不应创建重复的 watcher"""
        await repo.create_library(name="t", path=str(tmp_path), automation=LibraryAutomation.SCRAPE)
        await service.start()
        await service.start()  # 第二次调用 - 应为无操作
        assert service.is_running
        await service.stop()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_on_file_deleted_removes_from_db(self, service, repo: Repository, tmp_path: Path):
        """文件删除时从 DB 移除 MediaFile"""
        test_file = tmp_path / "video.mp4"
        test_file.write_bytes(b"\x00" * 100)

        # 先注册文件
        media = await repo.create_media_file(library_id=1, path=str(test_file))
        assert media.id is not None

        # 调用删除处理器
        await service._on_file_deleted(test_file)

        # 验证已从 DB 移除
        assert await repo.get_media_file(media.id) is None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_on_file_deleted_ignores_untracked(self, service, repo: Repository, tmp_path: Path):
        """删除未追踪的文件不会报错"""
        test_file = tmp_path / "unknown.mp4"
        await service._on_file_deleted(test_file)  # 不应抛出

    @pytest.mark.asyncio(loop_scope="function")
    async def test_delete_under_removes_prefix_only(self, service, repo: Repository, tmp_path: Path):
        lib = await repo.create_library(name="t", path=str(tmp_path), automation=LibraryAutomation.WATCH)
        other = await repo.create_library(name="o", path=str(tmp_path / "other"), automation=LibraryAutomation.WATCH)
        assert lib.id is not None
        assert other.id is not None
        nested = tmp_path / "show"
        video = nested / "a.mp4"
        sibling = tmp_path / "show2" / "b.mp4"
        other_video = tmp_path / "other" / "c.mp4"
        await repo.create_media_file(library_id=lib.id, path=str(video))
        await repo.create_media_file(library_id=lib.id, path=str(sibling))
        await repo.create_media_file(library_id=other.id, path=str(other_video))
        await service._delete_under(lib.id, nested)
        assert await repo.get_media_file_by_path(str(video)) is None
        assert await repo.get_media_file_by_path(str(sibling)) is not None
        assert await repo.get_media_file_by_path(str(other_video)) is not None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_delete_under_file_path_only_self(self, service, repo: Repository, tmp_path: Path):
        """文件路径当前缀时只命中自身, 不碰到兄弟文件."""
        lib = await repo.create_library(name="t", path=str(tmp_path), automation=LibraryAutomation.WATCH)
        assert lib.id is not None
        video = tmp_path / "a.mp4"
        sibling = tmp_path / "b.mp4"
        await repo.create_media_file(library_id=lib.id, path=str(video))
        await repo.create_media_file(library_id=lib.id, path=str(sibling))
        await service._delete_under(lib.id, video)
        assert await repo.get_media_file_by_path(str(video)) is None
        assert await repo.get_media_file_by_path(str(sibling)) is not None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_dir_delete_flushes_prefixed_index(self, service, repo: Repository, tmp_path: Path):
        lib = await repo.create_library(name="t", path=str(tmp_path), automation=LibraryAutomation.WATCH)
        assert lib.id is not None
        nested = tmp_path / "show"
        video = nested / "a.mp4"
        await repo.create_media_file(library_id=lib.id, path=str(video))
        await service.start()
        handler = service._watcher._handlers[0]
        handler._pending_dir_deletes[str(nested)] = time.time() - DEBOUNCE_SECONDS - 1

        async def gone() -> bool:
            return await repo.get_media_file_by_path(str(video)) is None

        assert await await_for(gone, timeout=LOAD_TOLERANT_TIMEOUT)
        await service.stop()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_library_root_delete_keeps_index(self, service, repo, tmp_path: Path):
        """库根自身的删除事件不进入清理流程; 子目录删除仍按前缀删除."""
        lib = await repo.create_library(name="t", path=str(tmp_path), automation=LibraryAutomation.WATCH)
        assert lib.id is not None
        await service.start()
        handler = service._watcher._handlers[0]

        handler.on_deleted(DirDeletedEvent(src_path=str(tmp_path)))
        assert handler._pending_dir_deletes == {}

        handler.on_deleted(DirDeletedEvent(src_path=str(tmp_path / "show")))
        assert list(handler._pending_dir_deletes) == [str(tmp_path / "show")]
        await service.stop()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_on_file_moved_updates_path(self, service, repo: Repository, tmp_path: Path):
        """文件移动时更新 DB 中的路径"""
        src = tmp_path / "old.mp4"
        dest = tmp_path / "new.mp4"
        src.write_bytes(b"\x00" * 100)

        media = await repo.create_media_file(library_id=1, path=str(src))
        assert media.id is not None

        await service._on_file_moved(src, dest, 1)

        updated = await repo.get_media_file(media.id)
        assert updated is not None
        assert updated.path == str(dest)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_on_file_moved_untracked_creates_new(self, service, repo: Repository, tmp_path: Path):
        """源文件不在 DB 中时, 视为新文件发现"""
        src = tmp_path / "unknown.mp4"
        dest = tmp_path / "MIDV-123.mp4"
        dest.write_bytes(b"\x00" * 100)

        await service._on_file_moved(src, dest, 1)

        media = await repo.get_media_file_by_path(str(dest))
        assert media is not None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_add_library_hot(self, service, repo: Repository, tmp_path: Path):
        """热添加监控库"""
        service.add_library(str(tmp_path), library_id=1)
        assert service.is_running
        await service.stop()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_remove_library_before_start_is_noop(self, service):
        """watcher 未启动时移除库应为无操作, 不抛出."""
        service.remove_library(1)
        assert not service.is_running

    @pytest.mark.asyncio(loop_scope="function")
    async def test_remove_library_hot(self, service, tmp_path: Path):
        """热移除监控库: 对应 handler 被取消监控."""
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        service.add_library(str(d1), library_id=1)
        service.add_library(str(d2), library_id=2)
        assert {h.library_id for h in service._watcher._handlers} == {1, 2}

        service.remove_library(1)
        assert {h.library_id for h in service._watcher._handlers} == {2}
        await service.stop()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_add_library_twice_replaces(self, service, tmp_path: Path):
        """重复 add_library 同一 id 不应产生重复 handler (幂等)."""
        service.add_library(str(tmp_path), library_id=1)
        service.add_library(str(tmp_path), library_id=1)
        assert [h.library_id for h in service._watcher._handlers] == [1]
        await service.stop()


class TestFileWatcherTmpFiles:
    """集成测试: 通过 tmp_path 产生真实文件系统事件, 覆盖两种 observer 策略"""

    @pytest.mark.parametrize("use_polling", [True, False])
    def test_created_file_enters_pending(self, tmp_path: Path, use_polling: bool):
        """在监控目录中创建媒体文件会将其加入 pending"""
        received = []
        watcher = FileWatcher(
            observer_timeout=0.1, on_file_found=lambda p, _lib: received.append(p), use_polling=use_polling
        )
        watcher.watch(str(tmp_path), library_id=1)
        watcher.start()

        try:
            video = tmp_path / "movie.mkv"
            video.write_bytes(b"\x00" * 512)

            handler = watcher._handlers[0]
            wait_for(lambda: str(video) in handler._pending)
        finally:
            watcher.stop()

    @pytest.mark.parametrize("use_polling", [True, False])
    def test_non_media_file_ignored(self, tmp_path: Path, use_polling: bool):
        """非媒体文件不会被 watcher 拾取"""
        watcher = FileWatcher(observer_timeout=0.1, on_file_found=lambda p, _lib: None, use_polling=use_polling)
        watcher.watch(str(tmp_path), library_id=1)
        watcher.start()

        try:
            txt = tmp_path / "notes.txt"
            txt.write_text("hello")

            handler = watcher._handlers[0]
            wait_for(lambda: str(txt) not in handler._pending, duration=0.3, interval=0.05)
        finally:
            watcher.stop()

    @pytest.mark.parametrize("use_polling", [True, False])
    def test_subdirectory_recursive(self, tmp_path: Path, use_polling: bool):
        """递归监控能检测到子目录中的文件"""
        subdir = tmp_path / "season1"
        subdir.mkdir()

        watcher = FileWatcher(observer_timeout=0.1, on_file_found=lambda p, _lib: None, use_polling=use_polling)
        watcher.watch(str(tmp_path), library_id=1, recursive=True)
        watcher.start()

        try:
            video = subdir / "ep01.mp4"
            video.write_bytes(b"\x00" * 256)

            handler = watcher._handlers[0]
            wait_for(lambda: str(video) in handler._pending)
        finally:
            watcher.stop()

    @pytest.mark.parametrize("use_polling", [True, False])
    def test_moved_file_detected(self, tmp_path: Path, use_polling: bool):
        """将文件重命名/移动到监控目录会触发检测"""
        staging = tmp_path / "staging"
        staging.mkdir()
        watched = tmp_path / "watched"
        watched.mkdir()

        # 在监控目录外创建文件
        src = staging / "clip.mp4"
        src.write_bytes(b"\x00" * 128)

        watcher = FileWatcher(observer_timeout=0.1, on_file_found=lambda p, _lib: None, use_polling=use_polling)
        watcher.watch(str(watched), library_id=1)
        watcher.start()

        try:
            # 移入监控目录 (同文件系统, shutil.move 使用 os.rename)
            dest = watched / "clip.mp4"
            shutil.move(str(src), str(dest))

            handler = watcher._handlers[0]
            # PollingObserver 通过 on_created 检测新文件;
            # 原生 observer 通过 on_moved 检测 (同文件系统 rename)
            wait_for(lambda: str(dest) in handler._pending)
        finally:
            watcher.stop()

    @pytest.mark.parametrize("use_polling", [True, False])
    def test_moved_out_directory_records_dir_delete(self, tmp_path: Path, use_polling: bool):
        """目录移出监控根: 源侧记目录删除, 不依赖子文件逐条删除."""
        watched = tmp_path / "lib"
        outside = tmp_path / "out"
        watched.mkdir()
        outside.mkdir()
        nested = watched / "show"
        nested.mkdir()
        (nested / "a.mp4").write_bytes(b"\x00" * 64)

        watcher = FileWatcher(observer_timeout=0.1, on_file_found=lambda p, _lib: None, use_polling=use_polling)
        watcher.watch(str(watched), library_id=1)
        watcher.start()
        try:
            shutil.move(str(nested), str(outside / "show"))
            handler = watcher._handlers[0]
            wait_for(
                lambda: any(path_is_under(p, nested) and path_is_under(nested, p) for p in handler._pending_dir_deletes)
            )
        finally:
            watcher.stop()


class TestDirectoryMoveAndModify:
    """整体移动进来的目录与覆盖写: 两种事件过去都被忽略, 文件永远进不了库."""

    def _collect(self) -> tuple[list[Path], FileWatcher]:
        found: list[Path] = []

        def on_found(path: Path, _library_id: int) -> None:
            found.append(path)

        watcher = FileWatcher(on_file_found=on_found, debounce_seconds=0.05, settle_seconds=0)
        watcher.watch(str(Path("/tmp/watch-root")), library_id=1, patterns=None, skip_patterns=None)
        return found, watcher

    def test_directory_moved_in_expands_to_files(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        target = root / "ABC-123"
        target.mkdir()
        (target / "ABC-123.mp4").write_bytes(b"x" * 64)
        (target / "ABC-123.srt").write_bytes(b"s")

        found: list[Path] = []
        watcher = FileWatcher(
            on_file_found=lambda path, _id: found.append(path), debounce_seconds=0.0, settle_seconds=0
        )
        watcher.watch(str(root), library_id=1, patterns=None, skip_patterns=None)

        handler = watcher._handlers[0]
        handler.on_moved(DirMovedEvent(src_path=str(tmp_path / "staging" / "ABC-123"), dest_path=str(target)))
        watcher.check_debounced()

        assert [path.name for path in found] == ["ABC-123.mp4"]

    def test_modified_file_enters_the_queue(self, tmp_path: Path) -> None:
        """``cp -f`` 落盘只有 modified 事件, 不接它就等于这个文件从未出现."""
        root = tmp_path / "root2"
        root.mkdir()
        video = root / "ABC-124.mp4"
        video.write_bytes(b"x" * 64)

        found: list[Path] = []
        watcher = FileWatcher(
            on_file_found=lambda path, _id: found.append(path), debounce_seconds=0.0, settle_seconds=0
        )
        watcher.watch(str(root), library_id=1, patterns=None, skip_patterns=None)

        watcher._handlers[0].on_modified(FileMovedEvent(src_path=str(video), dest_path=str(video)))
        watcher.check_debounced()

        assert found == [video]

    def test_move_into_path_clears_stale_delete_event(self, tmp_path: Path) -> None:
        """先删后移入同一个目标路径: 旧的删除事件不能留在队列里, 否则会按它删掉刚入库的索引."""
        root = tmp_path / "root3"
        root.mkdir()
        video = root / "ABC-125.mp4"
        video.write_bytes(b"x" * 64)

        found: list[Path] = []
        watcher = FileWatcher(
            on_file_found=lambda path, _id: found.append(path), debounce_seconds=0.0, settle_seconds=0
        )
        watcher.watch(str(root), library_id=1, patterns=None, skip_patterns=None)
        handler = watcher._handlers[0]

        handler.on_deleted(FileDeletedEvent(src_path=str(video)))
        handler.on_moved(FileMovedEvent(src_path=str(tmp_path / "staging" / video.name), dest_path=str(video)))

        assert handler.get_ready_deletes() == []
        assert handler.get_ready_moves() != []
