"""插件接口的配置脱敏与占位符回写.

插件配置由插件自己声明, Amane 只能按字段名判据脱敏; 前端回写占位符时按当前值还原.
"""

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from amane.api.routes import API_PREFIX
from amane.config import HotSettings
from amane.observability.models import REDACTION_PLACEHOLDER
from tests.api.conftest import make_app
from tests.plugins.test_plugin_system import plugin_source, write_plugin

if TYPE_CHECKING:
    from pathlib import Path

PLUGIN_ID = "acme.fake"
PLUGIN_TOKEN = "plugin-api-token-secret"
PLUGIN_ENDPOINT = "https://plugin.example.test"

#: 带密钥字段的插件配置模型: 用例插件比默认夹具多一个 ``api_token``.
_SECRET_CONFIG_MODEL = '''    endpoint: str = "https://plugin.example.test"
    api_token: str = ""'''


def _write_plugin_with_token(data_dir: Path) -> None:
    source = plugin_source(PLUGIN_ID)
    assert '    endpoint: str = "https://plugin.example.test"' in source
    write_plugin(
        data_dir,
        PLUGIN_ID,
        body=source.replace('    endpoint: str = "https://plugin.example.test"', _SECRET_CONFIG_MODEL),
    )


@pytest.fixture
def plugin_app(tmp_path: Path) -> FastAPI:
    data_dir = tmp_path / "data"
    _write_plugin_with_token(data_dir)
    hot = HotSettings.model_validate(
        {
            "plugins": {
                PLUGIN_ID: {"config": {"endpoint": PLUGIN_ENDPOINT, "api_token": PLUGIN_TOKEN}},
            }
        }
    )
    return make_app(hot, data_dir, tmp_path / "logs", tmp_path / "files")


@pytest_asyncio.fixture
async def plugin_client(plugin_app: FastAPI) -> AsyncIterator[AsyncClient]:
    ctx = plugin_app.router.lifespan_context(plugin_app)
    await ctx.__aenter__()
    transport = ASGITransport(app=plugin_app)
    async with AsyncClient(transport=transport, base_url=f"http://test{API_PREFIX}/") as client:
        yield client
    await ctx.__aexit__(None, None, None)


class TestPluginConfigSecrets:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_list_and_get_hide_secrets(self, plugin_client: AsyncClient):
        listed = await plugin_client.get("plugins")
        assert listed.status_code == 200
        assert PLUGIN_TOKEN not in listed.text
        config = listed.json()["items"][0]["config"]["config"]
        assert config["api_token"] == REDACTION_PLACEHOLDER
        assert config["endpoint"] == PLUGIN_ENDPOINT

        single = await plugin_client.get(f"plugins/{PLUGIN_ID}")
        assert single.status_code == 200
        assert PLUGIN_TOKEN not in single.text
        assert single.json()["config"]["config"]["api_token"] == REDACTION_PLACEHOLDER

    @pytest.mark.asyncio(loop_scope="function")
    async def test_patch_write_back_keeps_secret(self, plugin_client: AsyncClient, plugin_app: FastAPI):
        """前端把拿到的插件配置原样回写: 占位符不能覆盖已存的密钥."""
        current = (await plugin_client.get(f"plugins/{PLUGIN_ID}")).json()["config"]
        resp = await plugin_client.patch(f"plugins/{PLUGIN_ID}", json=current)
        assert resp.status_code == 200, resp.text
        assert PLUGIN_TOKEN not in resp.text
        assert plugin_app.state.runtime.config.hot.plugins[PLUGIN_ID].config["api_token"] == PLUGIN_TOKEN

    @pytest.mark.asyncio(loop_scope="function")
    async def test_patch_accepts_plaintext(self, plugin_client: AsyncClient, plugin_app: FastAPI):
        resp = await plugin_client.patch(
            f"plugins/{PLUGIN_ID}", json={"config": {"endpoint": PLUGIN_ENDPOINT, "api_token": "new-token"}}
        )
        assert resp.status_code == 200, resp.text
        assert "new-token" not in resp.text
        assert plugin_app.state.runtime.config.hot.plugins[PLUGIN_ID].config["api_token"] == "new-token"
