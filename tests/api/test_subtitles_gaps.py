"""``/subtitles/gaps``: 缺字幕的原因、失败次数与可重试性."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from amane.db.models import MediaFileStatus, TaskType

if TYPE_CHECKING:
    from pathlib import Path

    from httpx2 import AsyncClient

    from amane.db.models import MediaFile
    from amane.db.repository import Repository


async def _library(repo: Repository, root: Path) -> int:
    root.mkdir(parents=True, exist_ok=True)
    library = await repo.create_library(name="缺口库", path=str(root), subtitle_extensions=[".srt"], write_nfo=False)
    assert library.id is not None
    return library.id


async def _media(repo: Repository, library_id: int, root: Path, name: str, number: str) -> MediaFile:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    return await repo.create_media_file(
        library_id=library_id, path=str(target), number=number, status=MediaFileStatus.SCRAPED
    )


class TestGaps:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_each_gap_explains_its_own_reason(
        self, client: AsyncClient, repo: Repository, tmp_path: Path, stop_worker: None
    ):
        library_id = await _library(repo, tmp_path / "lib")
        root = tmp_path / "lib"
        untouched = await _media(repo, library_id, root, "AD-1.mp4", "AD-1")
        failing = await _media(repo, library_id, root, "AD-2.mp4", "AD-2")
        pointer = await _media(repo, library_id, root, "AD-3/AD-3.strm", "AD-3")
        disc = await _media(repo, library_id, root, "AD-4-CD2.mp4", "AD-4")

        failed = await repo.create_task(TaskType.SUBTITLE_FETCH, {"media_file_id": failing.id})
        assert await repo.fail_task(failed.id, "站点全部超时")

        response = await client.get("subtitles/gaps")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 3, "分卷默认不列, 它本来就不是补全目标"
        assert body["counts"] == {
            "missing": 2,
            "multi_disc": 1,
            "pointer": 1,
            "failed": 1,
            "fetch_empty": 0,
            "not_attempted": 1,
        }

        by_id = {item["media_file_id"]: item for item in body["items"]}
        assert by_id[failing.id]["reason"] == "fetch_failed"
        assert by_id[failing.id]["failed"] == 1
        assert by_id[failing.id]["last_error"] == "站点全部超时"
        assert by_id[failing.id]["retryable"] is True
        assert by_id[untouched.id]["reason"] == "not_attempted"
        assert by_id[pointer.id]["kind"] == "pointer"
        assert by_id[pointer.id]["reason"] == "pointer"
        assert "转写跳过" in by_id[pointer.id]["detail"]
        assert disc.id not in by_id
        assert body["items"][0]["media_file_id"] == failing.id, "失败过的排前面"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_attempts_are_counted_from_task_history(
        self, client: AsyncClient, repo: Repository, tmp_path: Path, stop_worker: None
    ):
        library_id = await _library(repo, tmp_path / "lib2")
        media = await _media(repo, library_id, tmp_path / "lib2", "AD-9.mp4", "AD-9")

        first = await repo.create_task(TaskType.SUBTITLE_FETCH, {"media_file_id": media.id})
        assert await repo.fail_task(first.id, "第一次: 连接被拒绝")
        second = await repo.create_task(TaskType.SUBTITLE_FETCH, {"media_file_id": media.id})
        claimed = await repo.claim_next_task()
        assert claimed is not None and claimed.id == second.id
        assert await repo.complete_task(second.id, None)
        third = await repo.create_task(TaskType.SUBTITLE_FETCH, {"media_file_id": media.id})
        assert await repo.fail_task(third.id, "第三次: 站点 504")

        body = (await client.get("subtitles/gaps")).json()
        item = next(row for row in body["items"] if row["media_file_id"] == media.id)
        assert item["attempts"] == 3
        assert item["failed"] == 2
        assert item["last_error"] == "第三次: 站点 504", "原因取最近一次尝试"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_multi_disc_can_be_included_on_demand(
        self, client: AsyncClient, repo: Repository, tmp_path: Path, stop_worker: None
    ):
        library_id = await _library(repo, tmp_path / "lib3")
        await _media(repo, library_id, tmp_path / "lib3", "AD-5-CD1.mp4", "AD-5")

        default = (await client.get("subtitles/gaps")).json()
        assert default["total"] == 0
        assert default["counts"]["multi_disc"] == 1

        included = (await client.get("subtitles/gaps", params={"include_multi_disc": True})).json()
        assert included["total"] == 1
        assert included["items"][0]["reason"] == "multi_disc"
        assert included["items"][0]["retryable"] is False

    @pytest.mark.asyncio(loop_scope="function")
    async def test_library_filter_and_unknown_library(
        self, client: AsyncClient, repo: Repository, tmp_path: Path, stop_worker: None
    ):
        first = await _library(repo, tmp_path / "lib4")
        second = await _library(repo, tmp_path / "lib5")
        await _media(repo, first, tmp_path / "lib4", "AD-6.mp4", "AD-6")
        await _media(repo, second, tmp_path / "lib5", "AD-7.mp4", "AD-7")

        only_first = (await client.get("subtitles/gaps", params={"library_id": first})).json()
        assert [item["number"] for item in only_first["items"]] == ["AD-6"]
        assert (await client.get("subtitles/gaps", params={"library_id": 999})).status_code == 404

    @pytest.mark.asyncio(loop_scope="function")
    async def test_number_is_parsed_when_the_row_has_none(
        self, client: AsyncClient, repo: Repository, tmp_path: Path, stop_worker: None
    ):
        """索引行的 number 多为空 (早期批量导入); 界面按路径解析出番号, 不能整列显示「—」."""
        library_id = await _library(repo, tmp_path / "lib")
        root = tmp_path / "lib"
        actor_dir = root / "某演员" / "SMOK-049"
        actor_dir.mkdir(parents=True)
        pointer = actor_dir / "SMOK-049-有码.strm"
        pointer.write_text("http://example/strm\n", encoding="utf-8")
        await repo.create_media_file(
            library_id=library_id, path=str(pointer), number=None, status=MediaFileStatus.SCRAPED
        )

        r = await client.get("/subtitles/gaps")
        assert r.status_code == 200
        items = r.json()["items"]
        assert [i["number"] for i in items] == ["SMOK-049"]
