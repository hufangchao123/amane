"""对已有的 NFO 做两类就地修补: 给演员块补 ``<thumb>``, 或删掉整行 ``<director>``.

修补只改磁盘上的 NFO 文本, 不写数据库, 也不改库内索引; 因此对手工搬动过的目录同样适用.
写入用同目录临时文件加 ``os.replace``, 中途失败不会留下半截 NFO.
两类修补都幂等: 演员块已有 ``<thumb>`` 的跳过, 没有 ``<director>`` 的文件不计变更.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ...utils.threads import in_thread
from ..rules import is_in_trash

NFO_SUFFIX = ".nfo"
MAX_SAMPLES = 10

_ACTOR_BLOCK = re.compile(r"<actor>(.*?)</actor>", re.DOTALL)
_ACTOR_THUMB = re.compile(r"<thumb[\s/>]")
_ACTOR_NAME = re.compile(r"<name>(.*?)</name>", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_DIRECTOR_LINE = re.compile(r"[ \t]*<director>.*?</director>[ \t]*\r?\n?", re.DOTALL)


@dataclass
class NfoPatchOutcome:
    """一次修补的计数. ``files`` 是扫到的 NFO 数, ``changed`` 是真正改写的文件数."""

    files: int = 0
    changed: int = 0
    filled: int = 0
    missing_image: int = 0
    removed: int = 0
    failed: int = 0
    samples: list[str] = field(default_factory=list)

    def merge(self, other: NfoPatchOutcome) -> None:
        self.files += other.files
        self.changed += other.changed
        self.filled += other.filled
        self.missing_image += other.missing_image
        self.removed += other.removed
        self.failed += other.failed
        for sample in other.samples:
            if len(self.samples) < MAX_SAMPLES:
                self.samples.append(sample)


@in_thread
def fill_actor_thumbs(paths: Sequence[str], images: dict[str, str], *, dry_run: bool) -> NfoPatchOutcome:
    """按演员名给演员块插入 ``<thumb>``; 名字查不到图片时只计数 ``missing_image``."""
    outcome = NfoPatchOutcome()
    for nfo_path in _iter_nfo(paths):
        outcome.files += 1
        try:
            text = nfo_path.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            outcome.failed += 1
            continue
        patched, filled, missing = _fill_actor_thumbs(text, images)
        outcome.filled += filled
        outcome.missing_image += missing
        if filled == 0:
            continue
        outcome.changed += 1
        if len(outcome.samples) < MAX_SAMPLES:
            outcome.samples.append(nfo_path.name)
        if not dry_run and not _write_atomically(nfo_path, patched):
            outcome.failed += 1
    return outcome


@in_thread
def strip_directors(paths: Sequence[str], *, dry_run: bool) -> NfoPatchOutcome:
    """删掉 ``<director>`` 行; 该标签只有一个值, 整行删除不影响其它元素."""
    outcome = NfoPatchOutcome()
    for nfo_path in _iter_nfo(paths):
        outcome.files += 1
        try:
            text = nfo_path.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            outcome.failed += 1
            continue
        patched, removed = _DIRECTOR_LINE.subn("", text)
        if removed == 0:
            continue
        outcome.removed += removed
        outcome.changed += 1
        if len(outcome.samples) < MAX_SAMPLES:
            outcome.samples.append(nfo_path.name)
        if not dry_run and not _write_atomically(nfo_path, patched):
            outcome.failed += 1
    return outcome


def count_directors(paths: Sequence[str]) -> int:
    """只数 ``<director>`` 的条数, 供状态端点报出待清理规模."""
    total = 0
    for nfo_path in _iter_nfo(paths):
        try:
            total += len(_DIRECTOR_LINE.findall(nfo_path.read_text(encoding="utf-8")))
        except OSError, UnicodeDecodeError:
            continue
    return total


def _fill_actor_thumbs(text: str, images: dict[str, str]) -> tuple[str, int, int]:
    filled = 0
    missing = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal filled, missing
        block = match.group(1)
        if _ACTOR_THUMB.search(block):
            return match.group(0)
        name_match = _ACTOR_NAME.search(block)
        if name_match is None:
            return match.group(0)
        name = _TAG.sub("", name_match.group(1)).strip()
        url = images.get(name, "")
        if not url:
            missing += 1
            return match.group(0)
        filled += 1
        thumb = f"    <thumb>{_escape_url(url)}</thumb>\n"
        return f"<actor>{block.rstrip()}\n{thumb}  </actor>"

    return _ACTOR_BLOCK.sub(_replace, text), filled, missing


def _escape_url(url: str) -> str:
    return url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _iter_nfo(paths: Iterable[str]) -> Iterable[Path]:
    for raw in paths:
        root = Path(raw)
        if not root.is_dir():
            continue
        for current, directories, files in os.walk(root):
            directories[:] = [name for name in directories if not is_in_trash(Path(current) / name)]
            for name in files:
                if name.endswith(NFO_SUFFIX):
                    yield Path(current) / name


def _write_atomically(nfo_path: Path, text: str) -> bool:
    temporary = nfo_path.with_name(nfo_path.name + ".patch-tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
    except OSError:
        return False
    try:
        temporary.replace(nfo_path)
    except OSError:
        temporary.unlink(missing_ok=True)
        return False
    return True
