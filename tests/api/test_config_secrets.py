"""配置接口的密钥脱敏与占位符回写.

响应里不出现明文密钥; 前端把占位符原样回写时按当前值还原, 不丢配置.
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

if TYPE_CHECKING:
    from pathlib import Path

SITE_COOKIE = "site-cookie-secret"
SITE_TOKEN = "site-api-token-secret"
LLM_KEY = "llm-api-key-secret"
AGENT_KEY = "agent-api-key-secret"
PROXY_URL = "http://proxy-user:proxy-pass-secret@127.0.0.1:7890"
SOLVER_URL = "http://solver-user:solver-pass-secret@127.0.0.1:8191"
NOTIFY_URL = "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage"

#: 响应里一个都不许出现的明文片段.
PLAINTEXTS = (
    SITE_COOKIE,
    SITE_TOKEN,
    LLM_KEY,
    AGENT_KEY,
    "proxy-pass-secret",
    "solver-pass-secret",
    "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
)


def _secrets_hot() -> HotSettings:
    return HotSettings.model_validate(
        {
            "scraping": {"site_config": {"javdb": {"cookie": {"session": SITE_COOKIE}, "api_token": SITE_TOKEN}}},
            "llm": {"api_key": LLM_KEY},
            "agent": {"api_key": AGENT_KEY},
            "notify": {"url": NOTIFY_URL},
            "network": {"proxy": PROXY_URL, "browser": {"solver_url": SOLVER_URL}},
        }
    )


@pytest.fixture
def secrets_app(tmp_path: Path) -> FastAPI:
    return make_app(_secrets_hot(), tmp_path / "data", tmp_path / "logs", tmp_path / "files")


@pytest_asyncio.fixture
async def secrets_client(secrets_app: FastAPI) -> AsyncIterator[AsyncClient]:
    ctx = secrets_app.router.lifespan_context(secrets_app)
    await ctx.__aenter__()
    transport = ASGITransport(app=secrets_app)
    async with AsyncClient(transport=transport, base_url=f"http://test{API_PREFIX}/") as client:
        yield client
    await ctx.__aexit__(None, None, None)


class TestConfigSecrets:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_get_hides_secrets(self, secrets_client: AsyncClient):
        resp = await secrets_client.get("config")
        assert resp.status_code == 200

        body = resp.text
        for plaintext in PLAINTEXTS:
            assert plaintext not in body, f"响应里出现明文: {plaintext}"

        data = resp.json()
        assert data["llm"]["api_key"] == REDACTION_PLACEHOLDER
        assert data["agent"]["api_key"] == REDACTION_PLACEHOLDER
        assert data["scraping"]["site_config"]["javdb"]["cookie"] == {"session": REDACTION_PLACEHOLDER}
        assert data["scraping"]["site_config"]["javdb"]["api_token"] == REDACTION_PLACEHOLDER
        assert data["notify"]["url"] == REDACTION_PLACEHOLDER
        assert data["network"]["proxy"] == "http://127.0.0.1:7890"
        assert data["network"]["browser"]["solver_url"] == "http://127.0.0.1:8191"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_get_keeps_plain_fields(self, secrets_client: AsyncClient, secrets_app: FastAPI):
        """脱敏只动密钥字段: 其余字段仍按当前配置返回, 前端不必改判断."""
        hot = secrets_app.state.runtime.config.hot
        data = (await secrets_client.get("config")).json()
        assert data["worker"]["concurrency"] == hot.worker.concurrency
        assert data["logging"]["level"] == hot.logging.level
        assert data["scraping"]["crop_poster"] == hot.scraping.crop_poster

    @pytest.mark.asyncio(loop_scope="function")
    async def test_patch_accepts_plaintext(self, secrets_client: AsyncClient, secrets_app: FastAPI):
        resp = await secrets_client.patch("config", json={"llm": {"api_key": "new-llm-key"}})
        assert resp.status_code == 200
        assert resp.json()["llm"]["api_key"] == REDACTION_PLACEHOLDER
        assert secrets_app.state.runtime.config.hot.llm.api_key == "new-llm-key", "写入方向仍接受明文"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_patch_write_back_keeps_secrets(self, secrets_client: AsyncClient, secrets_app: FastAPI):
        """把 GET 拿到的整份配置原样回写: 占位符与脱敏代理串都不能覆盖原值."""
        current = (await secrets_client.get("config")).json()
        resp = await secrets_client.patch("config", json=current)
        assert resp.status_code == 200, resp.text

        hot = secrets_app.state.runtime.config.hot
        assert hot.llm.api_key == LLM_KEY
        assert hot.agent.api_key == AGENT_KEY
        site = hot.scraping.site_config["javdb"]
        assert site.cookie == {"session": SITE_COOKIE}
        assert site.api_token == SITE_TOKEN
        assert hot.notify.url == NOTIFY_URL
        assert hot.network.proxy == PROXY_URL
        assert hot.network.browser.solver_url == SOLVER_URL

        again = (await secrets_client.get("config")).text
        for plaintext in PLAINTEXTS:
            assert plaintext not in again

    @pytest.mark.asyncio(loop_scope="function")
    async def test_patch_placeholder_without_current_value_is_dropped(self, client: AsyncClient, app: FastAPI):
        """当前没有密钥时占位符不落盘: 写进 ``***`` 会让下一次校验直接失败."""
        assert app.state.runtime.config.hot.notify.url == ""
        resp = await client.patch("config", json={"notify": {"url": REDACTION_PLACEHOLDER}})
        assert resp.status_code == 200, resp.text
        assert app.state.runtime.config.hot.notify.url == ""
