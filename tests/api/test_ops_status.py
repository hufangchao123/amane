"""媒体运维只读端点的集成测试: 五个新路由都走全局鉴权, 默认配置下报未启用."""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from httpx2 import AsyncClient


STATUS_CASES = [
    pytest.param("transfer/status", {"enabled", "pending", "moved", "skipped", "failed"}, id="transfer"),
    pytest.param("subtitles/status", {"enabled", "missing", "queued"}, id="subtitles"),
    pytest.param("wash/status", {"enabled", "groups", "trashed", "restorable_bytes"}, id="wash"),
    pytest.param("nfo/status", {"actors", "actors_with_image", "libraries"}, id="nfo"),
    pytest.param("notify/status", {"enabled", "kind", "has_url", "records"}, id="notify"),
]


class TestOpsStatusHttp:
    @pytest.mark.asyncio(loop_scope="function")
    @pytest.mark.parametrize(("path", "keys"), STATUS_CASES)
    async def test_status_shape(self, client: AsyncClient, path: str, keys: set[str]) -> None:
        resp = await client.get(path)
        assert resp.status_code == 200
        body = resp.json()
        assert keys <= set(body)

    @pytest.mark.asyncio(loop_scope="function")
    @pytest.mark.parametrize(("path", "keys"), STATUS_CASES)
    async def test_disabled_by_default(self, client: AsyncClient, path: str, keys: set[str]) -> None:
        """默认配置下四个开关都是关的; 体检与备份不设 enabled 判据时也不得报故障."""
        body = (await client.get(path)).json()
        if "enabled" in body:
            assert body["enabled"] is False
        if "failed" in body:
            assert body["failed"] == 0


class TestOpsConfig:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_sections_roundtrip(self, client: AsyncClient) -> None:
        """四个 section 进配置 schema, 且能写回后再读出来."""
        schema = (await client.get("config/schema")).json()["properties"]
        for section in ("transfer", "subtitles", "wash"):
            assert section in schema

        patched = await client.patch("config", json={"transfer": {"enabled": True, "max_moves_per_run": 3}})
        assert patched.status_code == 200
        body = (await client.get("config")).json()
        assert body["transfer"]["enabled"] is True
        assert body["transfer"]["max_moves_per_run"] == 3

    @pytest.mark.asyncio(loop_scope="function")
    async def test_invalid_move_limit_rejected(self, client: AsyncClient) -> None:
        """越界值必须被拒: 单轮上限为 0 等于永远不搬."""
        resp = await client.patch("config", json={"transfer": {"max_moves_per_run": 0}})
        assert resp.status_code == 422


SUBMIT_CASES = [
    pytest.param({"type": "transfer_115"}, "transfer_115", id="transfer"),
    pytest.param({"type": "subtitle_fetch", "library_id": 1}, "subtitle_fetch", id="subtitles"),
    pytest.param({"type": "subtitle_audit"}, "subtitle_audit", id="subtitle-audit"),
    pytest.param({"type": "wash_scan", "kind": "local"}, "wash_scan", id="wash-scan"),
    pytest.param({"type": "wash_apply", "group_keys": ["SSIS-1"]}, "wash_apply", id="wash-apply"),
    pytest.param({"type": "nfo_patch", "kind": "directors"}, "nfo_patch", id="nfo-patch"),
    pytest.param({"type": "notify", "title": "测试", "body": "正文"}, "notify", id="notify"),
    pytest.param({"type": "avatar_check", "limit": 5}, "avatar_check", id="avatar-check"),
]


class TestOpsSubmit:
    @pytest.mark.asyncio(loop_scope="function")
    @pytest.mark.parametrize(("body", "task_type"), SUBMIT_CASES)
    async def test_submit_accepted(self, client: AsyncClient, body: dict, task_type: str) -> None:
        """运维任务都能入队; 缺省字段由 payload 默认值补齐."""
        resp = await client.post("tasks", json=body)
        assert resp.status_code == 202
        assert resp.json()["type"] == task_type

    @pytest.mark.asyncio(loop_scope="function")
    async def test_wash_kind_rejected(self, client: AsyncClient) -> None:
        """kind 只认三个字面量, 其它值 422."""
        assert (await client.post("tasks", json={"type": "wash_scan", "kind": "both"})).status_code == 422
