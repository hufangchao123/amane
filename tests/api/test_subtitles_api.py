"""/subtitles 与字幕任务的接口约束."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx2 import AsyncClient


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
        assert by_library.json()["payload"] == {"media_file_id": None, "library_id": 1}
