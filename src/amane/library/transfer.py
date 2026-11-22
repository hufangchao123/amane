"""刮削后的文件转移.

一个番号的视频与它同前缀的附属文件一起搬走: 字幕、NFO、封面都按 `<视频 stem>.<后缀>` 或
`<视频 stem>-<名>.<后缀>` 命名, 因此按前缀收集; 同前缀的另一份视频 (不同清晰度) 不在此列.

目的目录由调用方传入, 本模块不读配置: 成功与失败两个目的目录的语义在 handler 与库配置里.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..parsing import parse_file_info
from ..utils.threads import in_thread
from .rules import MEDIA_EXTENSIONS

# 不带字幕配置也会被搬走的附属文件: 元数据与图片.
EXTRA_SIDECAR_SUFFIXES: frozenset[str] = frozenset({".nfo", ".jpg", ".jpeg", ".png", ".webp"})


@dataclass(frozen=True)
class TransferOutcome:
    """一次转移的结果; ``video_path`` 是视频在新位置上的路径, 未搬动时等于原位."""

    destination: Path
    video_path: Path
    files: int = 0
    bytes: int = 0
    failed: list[str] = field(default_factory=list)
    source_dir_removed: bool = False


def _matches_stem(name: str, stem: str) -> bool:
    """同前缀判定: 名字以视频 stem (或番号) 开头, 且紧随其后的是分隔符.

    只看「前缀 + 分隔符」而不看长度, 因此 `ABC-123-1080p.mp4` 的附属文件既可以是
    `ABC-123-1080p.srt` (与 amane 默认模板一致) 也可以是 `ABC-123.nfo` (整理前的常见命名);
    是否搬走由后缀决定, 同前缀的另一份视频靠后缀排除.
    """
    if not name.startswith(stem):
        return False
    rest = name[len(stem) :]
    return rest[:1] in {".", "-", "_"}


def _prefixes(video: Path) -> set[str]:
    """可作为附属文件前缀的名字: 视频 stem 与解析出的番号."""
    prefixes = {video.stem}
    number = parse_file_info(path=video).number
    if number:
        prefixes.add(number)
    return prefixes


def _free_target(directory: Path, name: str) -> Path:
    """目的目录里已存在同名文件时加序号, 不覆盖已有文件."""
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    index = 2
    while (candidate := directory / f"{stem} ({index}){suffix}").exists():
        index += 1
    return candidate


def collect_sidecars(video: Path, subtitle_suffixes: Sequence[str]) -> list[Path]:
    """视频同目录下需要一起搬走的附属文件; 视频本身不在内."""
    allowed = {suffix.lower() for suffix in subtitle_suffixes} | EXTRA_SIDECAR_SUFFIXES
    prefixes = _prefixes(video)
    siblings: list[Path] = []
    for entry in video.parent.iterdir():
        if not entry.is_file() or entry == video:
            continue
        if entry.suffix.lower() not in allowed:
            continue
        if entry.suffix.lower() in MEDIA_EXTENSIONS:
            continue
        if any(_matches_stem(entry.name, prefix) for prefix in prefixes):
            siblings.append(entry)
    return sorted(siblings, key=lambda path: path.name)


def _remove_empty_dir(directory: Path) -> bool:
    try:
        directory.rmdir()
    except OSError:
        return False
    return True


@in_thread
def transfer_scrape_result(
    video: Path, destination: Path, subtitle_suffixes: Sequence[str], *, keep_dir: Path | None = None
) -> TransferOutcome:
    """把视频与附属文件搬到 ``destination``, 返回新路径与统计.

    单个文件搬不动只记进 ``failed``, 其余继续; 视频本身失败时 ``video_path`` 仍是原位,
    调用方据此判断要不要改库里的路径. 搬完若原目录空了则删掉这一层; ``keep_dir``
    (库根) 即使空了也保留, 删掉库根会让后续扫描失去目标.
    """
    destination.mkdir(parents=True, exist_ok=True)
    moved = 0
    written = 0
    failed: list[str] = []
    new_video = video
    for source in [video, *collect_sidecars(video, subtitle_suffixes)]:
        target = _free_target(destination, source.name)
        try:
            size = source.stat().st_size
            shutil.move(str(source), str(target))
        except OSError:
            failed.append(source.name)
            continue
        moved += 1
        written += size
        if source == video:
            new_video = target
    removable = new_video != video and (keep_dir is None or video.parent != keep_dir)
    removed = _remove_empty_dir(video.parent) if removable else False
    return TransferOutcome(
        destination=destination,
        video_path=new_video,
        files=moved,
        bytes=written,
        failed=failed,
        source_dir_removed=removed,
    )
