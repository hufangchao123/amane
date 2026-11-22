"""字幕巡检: 统计库里缺外挂字幕的文件, 并找出与同目录视频配不上的字幕.

只读: 不移动文件, 不写库, 不发网络. 清理配不上的字幕属于回收站运维, 这里只出清单.

「配不上」的判据取自同目录: 字幕文件名不以任何视频的 stem 开头即算配不上, 因为落位规则是
``<视频 stem>.<语言>.srt``. 判据只看文件名, 不看字幕内容与下载来源 —— 来源番号与目标番号是否
一致要靠下载历史才能判, 库内没有这份历史, 因此这里不下这个结论.
"""

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..parsing.file_info import parse_file_info
from ..utils.threads import in_thread


@dataclass(frozen=True)
class SubtitleFinding:
    """一条配不上的字幕; ``detail`` 说明判据命中的是哪一条."""

    path: str
    detail: str


@dataclass
class ScanResult:
    videos: int = 0
    subtitles: int = 0
    findings: list[SubtitleFinding] = field(default_factory=list)


@dataclass(frozen=True)
class _Directory:
    """一个目录里的视频; ``numbers`` 只用于在 detail 里写出同目录的番号."""

    stems: tuple[str, ...]
    numbers: frozenset[str]


def _directories(paths: Iterable[str]) -> dict[str, _Directory]:
    stems: dict[str, list[str]] = {}
    numbers: dict[str, set[str]] = {}
    for path in paths:
        directory = Path(path).parent.as_posix()
        stems.setdefault(directory, []).append(Path(path).stem)
        number = parse_file_info(path=path).number
        if number:
            numbers.setdefault(directory, set()).add(number.upper())
    return {
        directory: _Directory(tuple(names), frozenset(numbers.get(directory, set())))
        for directory, names in stems.items()
    }


def _subtitle_names(directory: str, suffixes: tuple[str, ...]) -> list[str]:
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    return [entry.name for entry in entries if entry.is_file() and entry.name.lower().endswith(suffixes)]


def _finding(directory: str, videos: _Directory, name: str) -> SubtitleFinding | None:
    if any(name.startswith(stem + ".") for stem in videos.stems):
        return None
    own = (parse_file_info(text=Path(name).stem).number or "").upper()
    detail = f"同目录没有同名视频 (字幕番号 {own}, 视频番号 {', '.join(sorted(videos.numbers)) or '未知'})"
    return SubtitleFinding(path=(Path(directory) / name).as_posix(), detail=detail)


@in_thread
def scan_library(paths: Sequence[str], suffixes: Sequence[str]) -> ScanResult:
    """同步扫描: 目录列举与番号解析都有磁盘与 CPU 开销, 因此整体进线程池."""
    report = ScanResult()
    if not suffixes:
        return report
    extensions = tuple(suffixes)
    for directory, videos in _directories(paths).items():
        report.videos += len(videos.stems)
        for name in _subtitle_names(directory, extensions):
            report.subtitles += 1
            finding = _finding(directory, videos, name)
            if finding is not None:
                report.findings.append(finding)
    return report
