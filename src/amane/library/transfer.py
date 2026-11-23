"""刮削后的文件转移.

一个番号的视频与它同前缀的附属文件一起搬走: 字幕、NFO、封面都按 `<视频 stem>.<后缀>` 或
`<视频 stem>-<名>.<后缀>` 命名, 因此按前缀收集; 同前缀的另一份视频 (不同清晰度) 不在此列.

目的目录由调用方传入, 本模块不读配置: 成功与失败两个目的目录的语义在 handler 与库配置里.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..parsing import parse_file_info
from ..utils.threads import in_thread
from .rules import MEDIA_EXTENSIONS

if TYPE_CHECKING:
    from ..db.models import Library

# 不带字幕配置也会被搬走的附属文件: 元数据与图片.
EXTRA_SIDECAR_SUFFIXES: frozenset[str] = frozenset({".nfo", ".jpg", ".jpeg", ".png", ".webp"})


def scrape_transfer_destination(library: Library, outcome: str) -> str | None:
    """刮削后转移的目的目录; 返回 None 表示这个结果不搬.

    成功侧: 配了成功目录就搬过去, 没配但有输入目录则搬进库根, 两者皆无说明文件已经在库根.
    失败侧: 只认失败目录, 没配就留在原处便于重试.
    """
    if outcome == "ok":
        if library.scrape_ok_dir:
            return library.scrape_ok_dir
        return library.path if library.scrape_input_dir else None
    return library.scrape_fail_dir or None


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


def _free_target(directory: Path, name: str) -> Path | None:
    """在目的目录里原子占一个名字, 已存在就加序号, 绝不覆盖已有文件.

    「先看存在再搬」在并发下会失效: 两个库共用同一个成品目录时, 各自持自己的库锁, 都能通过
    存在性检查拿到同一个目标, 后搬的那次会把先到的那份直接覆盖掉. 这里用 ``O_CREAT|O_EXCL``
    先把名字占住, 抢不到就顺延序号.
    """
    stem, suffix = Path(name).stem, Path(name).suffix
    for index in range(1, 1000):
        candidate = directory / (name if index == 1 else f"{stem} ({index}){suffix}")
        try:
            os.close(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            continue
        except OSError:
            return None
        return candidate
    return None


def collect_sidecars(
    video: Path,
    subtitle_suffixes: Sequence[str],
    *,
    directory: Path | None = None,
) -> list[Path]:
    """视频同目录下需要一起搬走的附属文件; 视频本身不在内.

    ``directory`` 用于重试: 视频已在目的目录, 而字幕/NFO/封面还留在原目录里.
    """
    allowed = {suffix.lower() for suffix in subtitle_suffixes} | EXTRA_SIDECAR_SUFFIXES
    prefixes = _prefixes(video)
    base = directory or video.parent
    siblings: list[Path] = []
    for entry in base.iterdir():
        if not entry.is_file() or entry == video:
            continue
        if entry.suffix.lower() not in allowed:
            continue
        if entry.suffix.lower() in MEDIA_EXTENSIONS:
            continue
        if any(_matches_stem(entry.name, prefix) for prefix in prefixes):
            siblings.append(entry)
    return sorted(siblings, key=lambda path: path.name)


def _prune_source_dir(directory: Path, keep_dir: Path | None) -> bool:
    """自下而上删空目录, 停在 ``keep_dir`` (库根) 之前; 返回是否删掉了至少一层.

    只删一层是不够的: 输入目录常按 `/in/2026/11/ABC-123/` 分层, 删掉最末一层后上面两层永远留着.
    """
    removed = False
    current = directory
    while True:
        if keep_dir is not None and _same_path(current, keep_dir):
            break
        try:
            current.rmdir()
        except OSError:
            break
        removed = True
        parent = current.parent
        if parent == current:
            break
        current = parent
    return removed


def _same_path(left: Path, right: Path) -> bool:
    """字面比较两个路径, 不解析符号链接 (与 ``path_is_under`` 的口径一致)."""
    return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


@in_thread
def transfer_scrape_result(
    video: Path,
    destination: Path,
    subtitle_suffixes: Sequence[str],
    *,
    keep_dir: Path | None = None,
    keep_source_dirs: bool = False,
    sidecars_only: bool = False,
    sidecar_dir: Path | None = None,
) -> TransferOutcome:
    """把视频与附属文件搬到 ``destination``, 返回新路径与统计.

    单个文件搬不动只记进 ``failed``, 其余继续; 视频本身失败时 ``video_path`` 仍是原位,
    调用方据此判断要不要改库里的路径. 搬完若原目录空了则逐层删到 ``keep_dir`` 为止, 除非
    ``keep_source_dirs`` 要求保留; ``keep_dir`` (库根) 即使空了也保留, 删掉库根会让后续扫描失去目标.

    ``sidecars_only`` 用于重试: 视频上一轮已经搬过去了, 这一轮只补搬当时失败的字幕/NFO/封面.
    """
    destination.mkdir(parents=True, exist_ok=True)
    moved = 0
    written = 0
    failed: list[str] = []
    new_video = video
    sidecars = collect_sidecars(video, subtitle_suffixes, directory=sidecar_dir)
    sources = sidecars if sidecars_only else [video, *sidecars]
    for source in sources:
        target = _free_target(destination, source.name)
        if target is None:
            failed.append(source.name)
            continue
        try:
            size = source.stat().st_size
            shutil.move(str(source), str(target))
        except OSError:
            # 占位文件没被搬移覆盖掉: 删掉, 否则目的目录里留下一个 0 字节的影子文件.
            target.unlink(missing_ok=True)
            failed.append(source.name)
            continue
        moved += 1
        written += size
        if source == video:
            new_video = target
    source_dir = sidecar_dir or video.parent
    vacuumed = sidecars_only or new_video != video
    removable = not keep_source_dirs and vacuumed and (keep_dir is None or not _same_path(source_dir, keep_dir))
    removed = _prune_source_dir(source_dir, keep_dir) if removable else False
    return TransferOutcome(
        destination=destination,
        video_path=new_video,
        files=moved,
        bytes=written,
        failed=failed,
        source_dir_removed=removed,
    )
