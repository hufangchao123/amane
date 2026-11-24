"""/subtitles 与字幕任务的接口约束."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx2 import AsyncClient

    from amane.db.models import MediaFile
    from amane.db.repository import Repository


class TestSubtitleTask:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_invalid_payload_is_422(self, client: AsyncClient, stop_worker: None):
        bad_media = await client.post("tasks", json={"type": "subtitle_fetch", "media_file_id": "abc"})
        assert bad_media.status_code == 422
        bad_library = await client.post("tasks", json={"type": "subtitle_fetch", "library_id": "abc"})
        assert bad_library.status_code == 422

    @pytest.mark.asyncio(loop_scope="function")
    async def test_valid_payloads_are_accepted(self, client: AsyncClient, stop_worker: None):
        """两个字段都可空; 都空表示全库按缺口挑, 因此空 payload 也必须被接受."""
        empty = await client.post("tasks", json={"type": "subtitle_fetch"})
        assert empty.status_code == 202
        by_library = await client.post("tasks", json={"type": "subtitle_fetch", "library_id": 1})
        assert by_library.status_code == 202
        assert by_library.json()["payload"] == {"media_file_id": None, "library_id": 1, "limit": 50}

        with_limit = await client.post("tasks", json={"type": "subtitle_fetch", "library_id": 1, "limit": 200})
        assert with_limit.status_code == 202
        assert with_limit.json()["payload"] == {"media_file_id": None, "library_id": 1, "limit": 200}


class TestGenerationProbe:
    """转写端点的探测接口: 只报告能不能用, 不改配置也不入任务表."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_reports_disabled_when_generation_is_off(self, client: AsyncClient):
        response = await client.post("subtitles/generation/test")

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert "未启用" in body["detail"]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_reports_missing_endpoint(self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
        await client.patch("config", json={"subtitles": {"generation": {"enabled": True, "base_url": ""}}})

        response = await client.post("subtitles/generation/test")

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert "没有配置转写端点" in body["detail"]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_silent_probe_counts_as_reachable(self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch):
        """静音没有分段: 端点答了 200 就算通, 不能报成不可用."""
        from amane.api.routes import subtitles as routes
        from amane.subtitles.whisper import WhisperError

        async def fake_transcribe(*args, **kwargs):
            raise WhisperError("转写端点没有返回分段 (response_format 需要 verbose_json)")

        monkeypatch.setattr(routes, "transcribe", fake_transcribe)
        await client.patch(
            "config",
            json={"subtitles": {"generation": {"enabled": True, "base_url": "http://whisper.local/v1"}}},
        )

        response = await client.post("subtitles/generation/test")

        body = response.json()
        assert body["ok"] is True
        assert "静音探测" in body["detail"]
        assert body["latency_ms"] >= 0


class TestUpload:
    """手动上传字幕: 落在视频旁边, 并点亮外挂字幕角标."""

    async def _seed(self, repo: Repository, root: Path) -> MediaFile:
        root.mkdir(parents=True, exist_ok=True)
        library = await repo.create_library(
            name="上传库",
            path=str(root),
            subtitle_extensions=[".srt", ".ass"],
            write_nfo=False,
        )
        video = root / "AD-1.mp4"
        video.write_bytes(b"x")
        media = await repo.create_media_file(library_id=library.id, path=str(video), number="AD-1")
        assert media is not None
        return media

    @pytest.mark.asyncio(loop_scope="function")
    async def test_upload_lands_next_to_the_video(self, client: AsyncClient, repo: Repository, tmp_path: Path):
        media = await self._seed(repo, tmp_path / "lib")

        response = await client.post(
            "subtitles/upload",
            data={"media_file_id": str(media.id), "language": "zh"},
            files={"file": ("AD-1.srt", b"1\n00:00:00,000 --> 00:00:01,000\n\xe4\xbd\xa0\xe5\xa5\xbd\n", "text/plain")},
        )

        assert response.status_code == 200
        body = response.json()
        target = Path(body["path"])
        assert target.name == "AD-1.zh.srt"
        assert target.is_file()
        assert body["bytes_written"] > 0
        refreshed = await repo.get_media_file(media.id)
        assert refreshed is not None and refreshed.has_external_subtitle is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_unknown_suffix_is_422(self, client: AsyncClient, repo: Repository, tmp_path: Path):
        media = await self._seed(repo, tmp_path / "lib")

        response = await client.post(
            "subtitles/upload",
            data={"media_file_id": str(media.id)},
            files={"file": ("AD-1.txt", b"hello", "text/plain")},
        )

        assert response.status_code == 422
        assert "字幕格式不支持" in response.json()["detail"]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_unknown_media_is_404(self, client: AsyncClient, repo: Repository, tmp_path: Path):
        await self._seed(repo, tmp_path / "lib")

        response = await client.post(
            "subtitles/upload",
            data={"media_file_id": "999999"},
            files={"file": ("AD-1.srt", b"1\n", "text/plain")},
        )

        assert response.status_code == 404

    @pytest.mark.asyncio(loop_scope="function")
    async def test_pointer_file_accepts_upload(self, client: AsyncClient, repo: Repository, tmp_path: Path):
        """.strm 没有音轨, 手动上传是唯一能补字幕的路子: 字幕落在指针文件旁边."""
        library = await repo.create_library(
            name="strm库",
            path=str(tmp_path / "strm"),
            subtitle_extensions=[".srt"],
            write_nfo=False,
        )
        (tmp_path / "strm").mkdir()
        pointer = tmp_path / "strm" / "AD-2.strm"
        pointer.write_text("http://example.com/a.mp4")
        media = await repo.create_media_file(library_id=library.id, path=str(pointer), number="AD-2")
        assert media is not None

        response = await client.post(
            "subtitles/upload",
            data={"media_file_id": str(media.id)},
            files={"file": ("AD-2.srt", b"1\n", "text/plain")},
        )

        assert response.status_code == 200
        assert Path(response.json()["path"]).name == "AD-2.zh.srt"


class TestSubtitleFiles:
    """列出 / 删除视频旁边的字幕: 替换错版字幕用."""

    async def _seed(self, repo: Repository, root: Path) -> MediaFile:
        root.mkdir(parents=True, exist_ok=True)
        library = await repo.create_library(
            name="字幕库", path=str(root), subtitle_extensions=[".srt", ".ass"], write_nfo=False
        )
        video = root / "AD-1.mp4"
        video.write_bytes(b"x")
        media = await repo.create_media_file(library_id=library.id, path=str(video), number="AD-1")
        (root / "AD-1.zh.srt").write_text("1\n")
        (root / "AD-1.机翻.srt").write_text("2\n")
        (root / "poster.jpg").write_bytes(b"jpg")
        return media

    @pytest.mark.asyncio(loop_scope="function")
    async def test_lists_only_subtitles(self, client: AsyncClient, repo: Repository, tmp_path: Path):
        media = await self._seed(repo, tmp_path / "lib")

        response = await client.get("subtitles/files", params={"media_file_id": media.id})

        assert response.status_code == 200
        body = response.json()
        names = sorted(item["name"] for item in body["files"])
        assert names == ["AD-1.zh.srt", "AD-1.机翻.srt"]
        assert body["extensions"] == [".ass", ".srt"]
        languages = {item["name"]: item["language"] for item in body["files"]}
        assert languages["AD-1.zh.srt"] == "zh"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_delete_removes_the_file_and_clears_the_badge(
        self, client: AsyncClient, repo: Repository, tmp_path: Path
    ):
        media = await self._seed(repo, tmp_path / "lib")
        await repo.update_media_file(media.id, has_external_subtitle=True)

        response = await client.post(
            "subtitles/delete",
            data={"media_file_id": str(media.id), "path": str(tmp_path / "lib" / "AD-1.zh.srt")},
        )

        assert response.status_code == 200
        remaining = [item["name"] for item in response.json()["files"]]
        assert remaining == ["AD-1.机翻.srt"]
        refreshed = await repo.get_media_file(media.id)
        assert refreshed is not None and refreshed.has_external_subtitle is True

        last = await client.post(
            "subtitles/delete",
            data={"media_file_id": str(media.id), "path": str(tmp_path / "lib" / "AD-1.机翻.srt")},
        )
        assert last.status_code == 200
        assert last.json()["files"] == []
        refreshed = await repo.get_media_file(media.id)
        assert refreshed is not None and refreshed.has_external_subtitle is False

    @pytest.mark.asyncio(loop_scope="function")
    async def test_delete_refuses_paths_outside_the_video_dir(
        self, client: AsyncClient, repo: Repository, tmp_path: Path
    ):
        media = await self._seed(repo, tmp_path / "lib")
        stray = tmp_path / "elsewhere.srt"
        stray.write_text("1\n")

        response = await client.post(
            "subtitles/delete",
            data={"media_file_id": str(media.id), "path": str(stray)},
        )

        assert response.status_code == 422
        assert "同目录" in response.json()["detail"]
        assert stray.is_file()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_upload_accepts_a_custom_name(self, client: AsyncClient, repo: Repository, tmp_path: Path):
        media = await self._seed(repo, tmp_path / "lib")

        response = await client.post(
            "subtitles/upload",
            data={"media_file_id": str(media.id), "filename": "AD-1.人工校对.srt"},
            files={"file": ("x.srt", b"1\n", "text/plain")},
        )

        assert response.status_code == 200
        assert Path(response.json()["path"]).name == "AD-1.人工校对.srt"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_upload_refuses_bad_custom_names(self, client: AsyncClient, repo: Repository, tmp_path: Path):
        media = await self._seed(repo, tmp_path / "lib")

        separator = await client.post(
            "subtitles/upload",
            data={"media_file_id": str(media.id), "filename": "../evil.srt"},
            files={"file": ("x.srt", b"1\n", "text/plain")},
        )
        mismatch = await client.post(
            "subtitles/upload",
            data={"media_file_id": str(media.id), "filename": "AD-1.srt"},
            files={"file": ("x.ass", b"1\n", "text/plain")},
        )

        assert separator.status_code == 422
        assert mismatch.status_code == 422
