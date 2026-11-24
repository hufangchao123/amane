from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import AfterValidator

from ..utils.path import path_is_under
from ..utils.threads import in_thread

if TYPE_CHECKING:
    from re import Pattern

MEDIA_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".mkv",
        ".avi",
        ".wmv",
        ".flv",
        ".mov",
        ".ts",
        ".iso",
        ".strm",
    }
)

# 与默认 trailer 模板文件名 `{link_dir}/trailer.mp4` 对齐; 空串表示不跳过.
DEFAULT_TRAILER_PATTERN = "(?i)trailer"

# 空列表关闭字幕发现. 不纳入 MEDIA_EXTENSIONS (字幕不是正片).
DEFAULT_SUBTITLE_EXTENSIONS: tuple[str, ...] = (".srt", ".ass", ".ssa", ".vtt", ".sub")

_SUBTITLE_EXT_RE = re.compile(r"^\.[a-z0-9]+$")

# 固定保留名. 路径任一深度含此目录名则不入库.
TRASH_DIRNAME = ".amane_trash"

WASH_TRASH_DIRNAME = ".amane_wash"
"""洗版回收站. 常态落在库根的父目录, 库建在父目录层面时会被扫描到, 因此与 ``TRASH_DIRNAME`` 同等忽略."""

_IGNORED_DIRNAMES = frozenset({TRASH_DIRNAME, WASH_TRASH_DIRNAME})

# .strm 在扫描扩展名里 (当正片入口), 但是路径指针不是视频字节; 大小过滤不把它当视频字节.
_POINTER_EXTENSIONS = frozenset({".strm"})

PARTIAL_SUFFIXES = frozenset({".!qb", ".part", ".crdownload", ".download", ".aria2"})
"""下载器写到一半的文件用这些后缀收尾 (qBittorrent 默认 ``.!qB``).

按整条扩展名比: ``ABC-123.mkv.!qB`` 的 ``suffix`` 是 ``.!qB``, 与视频扩展名不同, 因此既不
会入库也不会触发监控事件; 下载完成后该后缀被去掉, 那一刻才会被当成正片处理.
"""


def is_partial_download(path: Path) -> bool:
    """下载器临时文件: 扩展名是 ``.!qB`` / ``.part`` 之类."""
    return path.suffix.casefold() in PARTIAL_SUFFIXES


def scope_roots(
    *,
    path: str,
    input_dir: str | None = None,
    ok_dir: str | None = None,
    fail_dir: str | None = None,
) -> list[Path]:
    """媒体库跟踪的全部目录: 库根与三个可选目录, 跳过空项.

    刮削后转移让 ``MediaFile.path`` 可以落在库根之外; 判断一条记录是否仍属于这个库时,
    必须按这组目录判定, 不能只比库根.
    """
    extras = (input_dir, ok_dir, fail_dir)
    return [Path(path), *(Path(extra) for extra in extras if extra)]


def landing_roots(
    *, input_dir: str | None = None, ok_dir: str | None = None, fail_dir: str | None = None
) -> list[Path]:
    """刮削后转移的三个落点目录 (不含库根).

    这些目录里的记录仍属于本库, 但目录可能整块读不到 (网络盘未就绪); 判「文件消失」时要排除,
    否则会把在册记录当孤儿删掉.
    """
    return [Path(extra) for extra in (input_dir, ok_dir, fail_dir) if extra]


def publish_roots(*, path: str, ok_dir: str | None = None) -> list[Path]:
    """成品所在目录: 库根与成功目录.

    整理与洗版只认这两处 —— 输入目录里是待整理文件, 失败目录里是待复制的废片,
    都不该参与「留最优」的比较.
    """
    roots = [Path(path)]
    if ok_dir:
        roots.append(Path(ok_dir))
    return roots


def is_under_any(path: str | Path, roots: Sequence[Path]) -> bool:
    """路径是否为任一目录自身或子孙 (字面比较, 不解析符号链接)."""
    return any(path_is_under(path, root) for root in roots)


def compile_skip_patterns(patterns: Sequence[str | None] | None) -> list[Pattern[str]] | None:
    """逐条编译, 不能用 `|` 拼接: 用户书写的全局旗标 (如 `(?i)ads`) 出现在拼接中间会导致整体编译失败.
    空列表/全是空串返回 None (不跳过). 非法模式跳过该项.
    """
    compiled: list[Pattern[str]] = []
    for p in patterns or []:
        if not p or not p.strip():
            continue
        try:
            compiled.append(re.compile(p))
        except re.error:
            continue
    return compiled or None


def validate_regex_pattern(pattern: str, field: str) -> str:
    """校验用户输入的正则; 空串合法 (关闭)."""
    if pattern.strip():
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid {field}: {exc}") from exc
    return pattern


def validate_trailer_pattern(pattern: str) -> str:
    return validate_regex_pattern(pattern, "trailer_pattern")


def validate_blacklist_pattern(pattern: str) -> str:
    return validate_regex_pattern(pattern, "blacklist_pattern")


def validate_subtitle_extension(value: str) -> str:
    """规范化字幕扩展名: 小写、补前导点. 空串/路径分隔符/非字母数字非法."""
    stripped = value.strip().lower()
    if not stripped:
        raise ValueError("subtitle extension must not be empty")
    if not stripped.startswith("."):
        stripped = f".{stripped}"
    if "/" in stripped or "\\" in stripped or _SUBTITLE_EXT_RE.fullmatch(stripped) is None:
        raise ValueError(f"invalid subtitle extension: {value!r}")
    return stripped


def normalize_subtitle_extensions(values: list[str]) -> list[str]:
    """逐项规范化并去重 (保序). 空列表合法, 表示关闭字幕发现."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        ext = validate_subtitle_extension(raw)
        if ext not in seen:
            seen.add(ext)
            out.append(ext)
    return out


def validate_min_file_size(value: int) -> int:
    """字节阈值; 0 关闭. 负数非法."""
    if value < 0:
        raise ValueError("min_file_size must be >= 0")
    return value


TrailerPattern = Annotated[str, AfterValidator(validate_trailer_pattern)]
BlacklistPattern = Annotated[str, AfterValidator(validate_blacklist_pattern)]
SubtitleExtensions = Annotated[list[str], AfterValidator(normalize_subtitle_extensions)]
MinFileSize = Annotated[int, AfterValidator(validate_min_file_size)]


def is_video_media(path: Path, media_extensions: frozenset[str] | None = None) -> bool:
    extensions = MEDIA_EXTENSIONS if media_extensions is None else media_extensions
    return path.suffix.lower() in extensions


def is_undersized_video(path: Path, min_file_size: int, media_extensions: frozenset[str] | None = None) -> bool:
    """低于阈值的视频文件.

    - min_file_size <= 0 视为关闭.
    - 只对扫描视频扩展名判定; 图片 / nfo / 字幕等后缀一律不算.
    - .strm 是路径指针, 大小无意义, 不参与过滤.
    - 符号链接跟随目标, 比目标文件字节, 不是链接节点本身.
    - stat 失败 (含悬空链接) 视为不匹配, 不能把读不到的正片当广告丢弃.
    """
    if min_file_size <= 0:
        return False
    suffix = path.suffix.lower()
    if suffix in _POINTER_EXTENSIONS:
        return False
    if not is_video_media(path, media_extensions):
        return False
    try:
        return path.stat(follow_symlinks=True).st_size < min_file_size
    except OSError:
        return False


def is_skipped_media(path: Path, pattern: str | None) -> bool:
    """只匹配文件名 (含扩展名), 不匹配目录段."""
    compiled = compile_skip_patterns([pattern])
    if compiled is None:
        return False
    return any(r.search(path.name) for r in compiled)


def is_in_trash(path: Path) -> bool:
    """路径任一深度组件为 `.amane_trash` 或 `.amane_wash` 则视为回收目录内容: 不入库、不触发监控事件."""
    return any(part in _IGNORED_DIRNAMES for part in path.parts)


@in_thread
def has_companion_subtitle(video_path: Path, subtitle_extensions: Sequence[str]) -> bool:
    """视频同目录 (不递归) 存在库配置的字幕扩展名文件.

    只认扩展名, 不看文件名与语言: 同目录其它视频共用的字幕同样算命中.
    空扩展名列表关闭发现. 目录读不到 (不存在 / 无权限) 按无字幕处理.
    """
    extensions = {ext.lower() for ext in subtitle_extensions}
    if not extensions:
        return False
    try:
        if video_path.suffix.lower() in extensions:
            return True
        parent = video_path.parent
        with os.scandir(parent) as entries:
            for entry in entries:
                if entry.is_file() and Path(entry.name).suffix.lower() in extensions:
                    return True
    except OSError:
        return False
    return False
