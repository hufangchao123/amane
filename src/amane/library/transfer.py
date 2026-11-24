"""刮削后的文件转移.

一个番号的视频与它的附属文件一起搬走: 字幕、NFO、封面都按视频的文件名或番号命名, 归属按番号
判定, 解析不出番号的名字才退回前缀. 同目录里同一番号可能并存两个版本 (普通版与中字版),
附属文件只跟着自己那个版本走.

目的目录由调用方传入, 本模块不读配置: 成功与失败两个目的目录的语义在 handler 与库配置里.
"""

from __future__ import annotations

import errno
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from ..parsing import parse_file_info
from ..utils.path import is_descendant, resolved_path
from ..utils.threads import in_thread
from .rules import MEDIA_EXTENSIONS

if TYPE_CHECKING:
    from ..db.models import Library
    from ..parsing.file_info import FileInfo

# 不带字幕配置也会被搬走的附属文件: 元数据与图片.
EXTRA_SIDECAR_SUFFIXES: frozenset[str] = frozenset({".nfo", ".jpg", ".jpeg", ".png", ".webp"})

PARTIAL_SUFFIX = ".amane-partial"
"""占位文件的后缀: 目的目录里 0 字节的真实文件, 命名成功但内容还没到.

不用媒体后缀, 也不与下载器的临时后缀相同 (``library.rules.PARTIAL_SUFFIXES``): 占位落在扫描根
里, 与正片同后缀会让扫描把它当成品.
"""


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
    """前缀判定: 名字以视频 stem (或番号) 开头, 且紧随其后的是分隔符.

    只在文件名解析不出番号时兜底: 只看「前缀 + 分隔符」而不看长度, 因此 `ABC-123-1080p.mp4` 的
    附属文件既可以是 `ABC-123-1080p.srt` (与 amane 默认模板一致) 也可以是 `ABC-123.nfo`.
    """
    if not name.startswith(stem):
        return False
    rest = name[len(stem) :]
    return rest[:1] in {".", "-", "_"}


def _belongs(name: str, video: FileInfo, prefixes: set[str]) -> bool:
    """附属文件是否属于这个视频.

    归属按番号判定: 同目录里 `ABC-123.mp4` 与中字版 `ABC-123-C.mp4` 解析出同一个番号, 前缀会把
    中字版的字幕算到普通版头上, 搬走后中字版就没有字幕了. 番号相同还要比中字标记与分集 —— 两者
    都决定「视频的哪一份」, 不比同样会搬错.
    """
    sub = parse_file_info(text=name)
    if sub.number is None:
        return any(_matches_stem(name, prefix) for prefix in prefixes)
    if video.number is None or sub.number.casefold() != video.number.casefold():
        return False
    if sub.has_subtitle_in_name != video.has_subtitle_in_name:
        return False
    return _cd_belongs(video.cd, sub.cd)


def _cd_belongs(video_cd: int | None, sub_cd: int | None) -> bool:
    """分集口径与 `src/amane/organize/subtitles.py::_cd_belongs` 一致: 单片也算 cd=1 那一集的附属."""
    if sub_cd == video_cd:
        return True
    return video_cd == 1 and sub_cd is None


def _prefixes(video: Path, info: FileInfo) -> set[str]:
    """兜底用的前缀: 视频 stem 与解析出的番号."""
    prefixes = {video.stem}
    if info.number:
        prefixes.add(info.number)
    return prefixes


def _partial_path(target: Path) -> Path:
    return target.with_name(f"{target.name}{PARTIAL_SUFFIX}")


def _free_target(directory: Path, name: str) -> Path | None:
    """在目的目录里原子占一个名字, 已占用就加序号, 绝不覆盖已有文件.

    「先检查存在再搬」在并发下会失效: 两个库共用同一个成品目录时, 各自持自己的库锁, 都能通过
    存在性检查拿到同一个目标, 后搬的那次会把先到的那份直接覆盖掉. 这里用 ``O_CREAT|O_EXCL``
    先把占位文件写成, 抢不到就顺延序号; 占位用不进入媒体的后缀.
    """
    stem, suffix = Path(name).stem, Path(name).suffix
    for index in range(1, 1000):
        candidate = directory / (name if index == 1 else f"{stem} ({index}){suffix}")
        if candidate.exists():
            continue
        try:
            os.close(os.open(_partial_path(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            continue
        except OSError:
            return None
        return candidate
    return None


def _move_one(source: Path, directory: Path, name: str, *, attempts: int = 3) -> tuple[Path | None, int]:
    """搬一份文件, 返回落定路径与字节数; 失败返回 ``(None, 0)``.

    占位成功之后还要复核目标名没被别人写成真文件: 占位只能保证两个 amane 之间不撞,
    挡不住目的目录里的外部写入.
    """
    for _ in range(attempts):
        target = _free_target(directory, name)
        if target is None:
            return None, 0
        if target.exists():
            _partial_path(target).unlink(missing_ok=True)
            name = f"{target.stem} (2){target.suffix}"
            continue
        try:
            size = source.stat().st_size
            _place(source, target)
        except OSError:
            # 占位文件没被成品替换掉: 删掉, 否则目的目录里留下一个 0 字节的占位残片.
            _partial_path(target).unlink(missing_ok=True)
            return None, 0
        return target, size
    return None, 0


def _sidecar_name(name: str, video_stem: str, new_stem: str) -> str:
    """把附属文件名的视频前缀换成落定后的视频主名."""
    if video_stem and name.startswith(video_stem):
        return f"{new_stem}{name[len(video_stem) :]}"
    return name


def _place(source: Path, target: Path) -> None:
    """把源文件写到已占位的目标名上.

    同一文件系统内 ``replace`` 是原子的; 跨文件系统时回退为复制到同目录的临时名再替换 ——
    ``shutil.move`` 在 EXDEV 下直接复制到目标路径, 中途失败 (空间不足 / 网络断) 会在成品目录里
    留下半截的同名文件, 它看起来已经是一份完整的成品. 与 `src/amane/organize/file.py::_move`
    同一口径.

    成品落到目标名之后占位就撤掉: 它只负责在并发里占住名字, 留着会在扫描根里留下 0 字节文件.
    撤在写完之后, 占位期间名字一直是占着的.
    """
    try:
        source.replace(target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temporary)
            temporary.replace(target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        source.unlink(missing_ok=True)
    _partial_path(target).unlink(missing_ok=True)


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
    info = parse_file_info(path=video)
    prefixes = _prefixes(video, info)
    base = directory or video.parent
    siblings: list[Path] = []
    for entry in base.iterdir():
        if not entry.is_file() or entry == video:
            continue
        if entry.suffix.lower() not in allowed:
            continue
        if entry.suffix.lower() in MEDIA_EXTENSIONS:
            continue
        if _belongs(entry.name, info, prefixes):
            siblings.append(entry)
    return sorted(siblings, key=lambda path: path.name)


def _prune_source_dir(directory: Path, keep_dir: Path | None, stop_dirs: Sequence[Path] = ()) -> bool:
    """自下而上删空目录, 停在最近的授权边界之内; 返回是否删掉了至少一层.

    只删一层是不够的: 输入目录常按 `/in/2026/11/ABC-123/` 分层, 删掉最末一层后上面两层永远留着.
    配置的输入/成品/失败目录本身不删: 它们由使用者维护 (下载器要往里写), 消失后下载器可能报错.

    边界按真实路径比, 且每一步都要求当前目录仍严格处于边界之内: 输入目录经符号链接配置时
    (``/mnt/in -> /vol1/real_in``), 索引里的路径是真实路径, 字面比较永远不相等, 剪枝会一路删到
    边界之上. 一个边界都匹配不上时直接不剪 —— 源目录不在我们管的目录里, 没有理由动它.
    """
    boundary = _innermost_boundary(directory, keep_dir, stop_dirs)
    if boundary is None:
        return False
    current = resolved_path(directory)
    if not is_descendant(current, boundary):
        return False
    removed = False
    while current != boundary:
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


def _innermost_boundary(directory: Path, keep_dir: Path | None, stop_dirs: Sequence[Path]) -> Path | None:
    """目录所在的最内层授权边界 (真实路径); 都不包含它时返回 None."""
    target = resolved_path(directory)
    candidates = [resolved_path(path) for path in (keep_dir, *stop_dirs) if path is not None]
    inside = [boundary for boundary in candidates if is_descendant(target, boundary)]
    if not inside:
        return None
    return max(inside, key=lambda boundary: len(str(boundary)))


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
    stop_dirs: Sequence[Path] = (),
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
    if not sidecars_only:
        target, size = _move_one(video, destination, video.name)
        if target is None:
            failed.append(video.name)
        else:
            moved += 1
            written += size
            new_video = target
    # 附属文件跟着视频的新名字走: 视频被加了序号 (`ABC (2).mkv`) 时字幕若自己再抢一次名额,
    # 会得到 `ABC-1080p (2).srt` 之类与视频对不上的名字, 播放器与巡检都不再认它是对应的字幕.
    for source in sidecars:
        name = _sidecar_name(source.name, video.stem, new_video.stem)
        target, size = _move_one(source, destination, name)
        if target is None:
            failed.append(source.name)
            continue
        moved += 1
        written += size
    source_dir = sidecar_dir or video.parent
    vacuumed = sidecars_only or new_video != video
    removable = not keep_source_dirs and vacuumed and (keep_dir is None or not _same_path(source_dir, keep_dir))
    removed = _prune_source_dir(source_dir, keep_dir, stop_dirs) if removable else False
    return TransferOutcome(
        destination=destination,
        video_path=new_video,
        files=moved,
        bytes=written,
        failed=failed,
        source_dir_removed=removed,
    )
