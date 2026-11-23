"""库内文件转移: 同前缀附属文件的收集与搬动, 连同空目录回收与库根保留."""

from pathlib import Path

from amane.library.transfer import collect_sidecars, transfer_scrape_result


def _touch(path: Path, size: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_collect_sidecars_takes_prefix_siblings_only(tmp_path: Path) -> None:
    """同前缀的字幕、NFO、图片都收; 不带前缀的文件与同前缀的另一份视频不收."""
    video = _touch(tmp_path / "ABC-123" / "ABC-123-1080p.mp4")
    siblings = [
        _touch(video.parent / "ABC-123-1080p.srt"),
        _touch(video.parent / "ABC-123.nfo"),
        _touch(video.parent / "ABC-123-thumb.jpg"),
        _touch(video.parent / "ABC-123-720p.ass"),
    ]
    _touch(video.parent / "ABC-123-720p.mp4")
    _touch(video.parent / "XYZ-999.srt")
    _touch(video.parent / "ABC-1230.srt")

    collected = collect_sidecars(video, [".srt", ".ass"])

    assert collected == sorted(siblings, key=lambda path: path.name)


def test_transfer_moves_video_and_sidecars(tmp_path: Path) -> None:
    """视频与附属文件进目的目录, 原目录空了就删掉."""
    source = tmp_path / "in" / "ABC-123"
    video = _touch(source / "ABC-123-1080p.mp4", size=10)
    subtitle = _touch(source / "ABC-123-1080p.srt", size=5)
    destination = tmp_path / "done"

    outcome = transfer_scrape_result.sync(video, destination, [".srt"])

    assert outcome.failed == []
    assert (outcome.files, outcome.bytes) == (2, 15)
    assert outcome.video_path == destination / video.name
    assert outcome.video_path.read_bytes()
    assert (destination / subtitle.name).exists()
    assert not video.exists()
    assert not source.exists()
    assert outcome.source_dir_removed


def test_transfer_keeps_configured_dir_itself(tmp_path: Path) -> None:
    """输入/成品/失败目录本身不删: 下载器还要往里写, 目录消失会让它报错.

    子目录仍按 `keep_source_dirs` 关闭时的口径删掉.
    """
    input_dir = tmp_path / "downloads" / "PT"
    source = input_dir / "ABC-123"
    video = _touch(source / "ABC-123-1080p.mp4")

    outcome = transfer_scrape_result.sync(
        video, tmp_path / "done", [".srt"], keep_dir=tmp_path / "media", stop_dirs=(input_dir,)
    )

    assert outcome.video_path == tmp_path / "done" / video.name
    assert input_dir.is_dir()
    assert not source.exists()
    assert outcome.source_dir_removed


def test_transfer_keeps_library_root(tmp_path: Path) -> None:
    """``keep_dir`` 是库根: 即使搬空也保留, 否则后续扫描失去目标."""
    library_root = tmp_path / "media"
    video = _touch(library_root / "ABC-123.mp4")

    outcome = transfer_scrape_result.sync(video, tmp_path / "done", [".srt"], keep_dir=library_root)

    assert outcome.video_path == tmp_path / "done" / video.name
    assert library_root.is_dir()


def test_transfer_renames_on_collision(tmp_path: Path) -> None:
    """目的目录已有同名文件时不覆盖, 加序号."""
    source = tmp_path / "in"
    video = _touch(source / "ABC-123.mp4", size=3)
    destination = tmp_path / "done"
    _touch(destination / "ABC-123.mp4", size=99)

    outcome = transfer_scrape_result.sync(video, destination, [".srt"])

    assert outcome.video_path == destination / "ABC-123 (2).mp4"
    assert (destination / "ABC-123.mp4").stat().st_size == 99
    assert outcome.video_path.stat().st_size == 3
