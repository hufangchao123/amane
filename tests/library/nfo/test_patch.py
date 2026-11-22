"""NFO 修补的单元测试: 全部在临时目录里的文本文件上进行, 不碰数据库."""

import os
from pathlib import Path

import pytest

from amane.library.nfo.patch import count_directors, fill_actor_thumbs, strip_directors
from amane.library.rules import WASH_TRASH_DIRNAME

pytestmark = pytest.mark.asyncio(loop_scope="function")

NFO_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<movie>
  <title>ABC-123 标题</title>
  <actor>
    <name>演员甲</name>
    <type>Actor</type>
  </actor>
  <actor>
    <name>演员乙</name>
    <thumb>https://example.test/old.jpg</thumb>
    <type>Actor</type>
  </actor>
  <director>片商</director>
</movie>
"""


def _write_nfo(root: Path, name: str = "ABC-123.nfo", text: str = NFO_TEMPLATE) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


async def test_dry_run_counts_without_writing(tmp_path: Path) -> None:
    """预演只报数: 文件内容一字不改."""
    root = tmp_path / "media"
    path = _write_nfo(root)
    outcome = await fill_actor_thumbs([str(root)], {"演员甲": "https://example.test/a.jpg"}, dry_run=True)
    assert (outcome.files, outcome.changed, outcome.filled) == (1, 1, 1)
    assert path.read_text(encoding="utf-8") == NFO_TEMPLATE


async def test_fill_writes_thumb_and_is_idempotent(tmp_path: Path) -> None:
    """真跑补上缺的缩略图; 再跑一次不应产生变更."""
    root = tmp_path / "media"
    path = _write_nfo(root)
    images = {"演员甲": "https://example.test/a.jpg"}
    first = await fill_actor_thumbs([str(root)], images, dry_run=False)
    assert (first.changed, first.filled) == (1, 1)
    text = path.read_text(encoding="utf-8")
    assert "<thumb>https://example.test/a.jpg</thumb>" in text
    assert text.count("<thumb>") == 2

    second = await fill_actor_thumbs([str(root)], images, dry_run=False)
    assert (second.changed, second.filled) == (0, 0)


async def test_actor_without_image_is_counted(tmp_path: Path) -> None:
    """库内没有头像的演员只计数, 不写半截标签."""
    root = tmp_path / "media"
    path = _write_nfo(root)
    outcome = await fill_actor_thumbs([str(root)], {}, dry_run=False)
    assert (outcome.changed, outcome.filled, outcome.missing_image) == (0, 0, 1)
    assert path.read_text(encoding="utf-8") == NFO_TEMPLATE


async def test_url_ampersand_is_escaped(tmp_path: Path) -> None:
    """地址里的 ``&`` 必须转义, 否则 NFO 不是合法 XML."""
    root = tmp_path / "media"
    path = _write_nfo(root)
    await fill_actor_thumbs([str(root)], {"演员甲": "https://example.test/a.jpg?x=1&y=2"}, dry_run=False)
    assert "?x=1&amp;y=2" in path.read_text(encoding="utf-8")


async def test_trash_directories_are_skipped(tmp_path: Path) -> None:
    """回收站里的 NFO 不参与修补: 那些文件等待清理."""
    root = tmp_path / "media"
    live = _write_nfo(root)
    trashed = _write_nfo(root / WASH_TRASH_DIRNAME / "local")
    outcome = await fill_actor_thumbs([str(root)], {"演员甲": "https://example.test/a.jpg"}, dry_run=False)
    assert outcome.files == 1
    assert "<thumb>https://example.test/a.jpg</thumb>" in live.read_text(encoding="utf-8")
    assert "https://example.test/a.jpg" not in trashed.read_text(encoding="utf-8")


async def test_strip_directors_removes_lines(tmp_path: Path) -> None:
    """清导演: 整行删除, 其它元素保持原样."""
    root = tmp_path / "media"
    path = _write_nfo(root)
    outcome = await strip_directors([str(root)], dry_run=False)
    assert (outcome.changed, outcome.removed) == (1, 1)
    text = path.read_text(encoding="utf-8")
    assert "<director>" not in text
    assert "<title>" in text

    again = await strip_directors([str(root)], dry_run=False)
    assert (again.changed, again.removed) == (0, 0)


async def test_count_directors(tmp_path: Path) -> None:
    """状态端点用的计数与实际条数一致."""
    root = tmp_path / "media"
    _write_nfo(root)
    _write_nfo(root, "DEF-456.nfo", NFO_TEMPLATE.replace("<director>片商</director>", ""))
    assert count_directors([str(root)]) == 1


async def test_unreadable_files_are_counted(tmp_path: Path) -> None:
    """读不出来的 NFO 计失败, 不中断整轮修补."""
    root = tmp_path / "media"
    broken = root / "BAD-001.nfo"
    root.mkdir(parents=True)
    broken.write_bytes(b"\xff\xfe\x00bad")
    (root / "GOOD-002.nfo").write_text(NFO_TEMPLATE, encoding="utf-8")
    os.utime(broken, None)
    outcome = await strip_directors([str(root)], dry_run=True)
    assert (outcome.files, outcome.failed, outcome.changed) == (2, 1, 1)
