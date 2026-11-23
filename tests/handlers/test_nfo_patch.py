"""NFO_PATCH handler: 两类修补共用载荷, 演员头像取自库内 ``Actor.image_urls``."""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from amane.config import HotSettings
from amane.db.repo_types import WriteMode
from amane.handlers import NfoPatchHandler, NfoPatchPayload
from amane.handlers._common import LibraryTaskLocks

if TYPE_CHECKING:
    from amane.db.repository import Repository

NFO = """<?xml version="1.0" encoding="UTF-8"?>
<movie>
  <title>ABC-123 标题</title>
  <actor>
    <name>演员甲</name>
    <type>Actor</type>
  </actor>
  <director>片商</director>
</movie>
"""


async def _prepare(repo: Repository, root: Path, *, image: str | None = "https://example.test/a.jpg") -> int:
    """建库, 放一个 NFO, 并按需建一个带头像的演员."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "ABC-123.nfo").write_text(NFO, encoding="utf-8")
    library = await repo.create_library(name="日韩", path=str(root))
    assert library.id is not None
    await repo.upsert_metadata(number="ABC-123", actors=["演员甲"])
    actors = await repo.get_actors_by_names(["演员甲"])
    assert actors and actors[0].id is not None
    await repo.update_actor(actors[0].id, image_urls=[image] if image else [], mode=WriteMode.MANUAL)
    return library.id


@pytest.mark.asyncio(loop_scope="function")
async def test_unknown_library_fails(repo: Repository) -> None:
    """未知库直接失败, 不扫盘."""
    result = await NfoPatchHandler(repo, HotSettings(), LibraryTaskLocks()).handle(NfoPatchPayload(library_id=404))
    assert not result.success
    assert result.error == "媒体库 404 不存在"


@pytest.mark.asyncio(loop_scope="function")
async def test_thumbs_dry_run_then_apply(repo: Repository, tmp_path: Path) -> None:
    """预演报数不写盘; 关掉 ``dry_run`` 后缩略图进文件."""
    root = tmp_path / "media"
    library_id = await _prepare(repo, root)
    handler = NfoPatchHandler(repo, HotSettings(), LibraryTaskLocks())

    preview = await handler.handle(NfoPatchPayload(library_id=library_id, dry_run=True))
    assert preview.success and preview.result is not None
    assert (preview.result.changed, preview.result.filled, preview.result.dry_run) == (1, 1, True)
    assert "<thumb>" not in (root / "ABC-123.nfo").read_text(encoding="utf-8")

    applied = await handler.handle(NfoPatchPayload(library_id=library_id, dry_run=False))
    assert applied.success and applied.result is not None
    assert (applied.result.changed, applied.result.filled) == (1, 1)
    assert "https://example.test/a.jpg" in (root / "ABC-123.nfo").read_text(encoding="utf-8")


@pytest.mark.asyncio(loop_scope="function")
async def test_actor_without_image_is_reported(repo: Repository, tmp_path: Path) -> None:
    """库内演员没头像时计入 ``missing_image``, 文件不变."""
    root = tmp_path / "media"
    library_id = await _prepare(repo, root, image=None)
    result = await NfoPatchHandler(repo, HotSettings(), LibraryTaskLocks()).handle(
        NfoPatchPayload(library_id=library_id, dry_run=False)
    )
    assert result.success and result.result is not None
    assert (result.result.changed, result.result.missing_image) == (0, 1)


@pytest.mark.asyncio(loop_scope="function")
async def test_directors_are_removed(repo: Repository, tmp_path: Path) -> None:
    """``kind=directors`` 只碰导演行, 不补缩略图."""
    root = tmp_path / "media"
    library_id = await _prepare(repo, root)
    result = await NfoPatchHandler(repo, HotSettings(), LibraryTaskLocks()).handle(
        NfoPatchPayload(kind="directors", library_id=library_id, dry_run=False)
    )
    assert result.success and result.result is not None
    assert (result.result.removed, result.result.filled) == (1, 0)
    text = (root / "ABC-123.nfo").read_text(encoding="utf-8")
    assert "<director>" not in text and "<thumb>" not in text


@pytest.mark.asyncio(loop_scope="function")
async def test_missing_library_path_counts_as_failure(repo: Repository, tmp_path: Path) -> None:
    """库目录不存在时计失败一次, 任务不成功但结果仍带计数."""
    root = tmp_path / "gone"
    library_id = await _prepare(repo, root)
    for child in root.iterdir():
        child.unlink()
    root.rmdir()
    result = await NfoPatchHandler(repo, HotSettings(), LibraryTaskLocks()).handle(
        NfoPatchPayload(library_id=library_id, dry_run=False)
    )
    assert not result.success
    assert result.result is not None
    assert result.result.failed == 1
