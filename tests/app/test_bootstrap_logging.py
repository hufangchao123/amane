"""启动日志只保留 token 的指纹, 不出现明文.

明文 token 落在 ``data_dir`` 下的 token 文件 (0600), 日志里只需够核对「是不是同一枚」.
"""

from typing import TYPE_CHECKING

import pytest

from amane.app import bootstrap
from amane.config import HotSettings
from tests.api.conftest import make_app
from tests.helpers import LogRecorder

if TYPE_CHECKING:
    from pathlib import Path

API_TOKEN = "api-token-plaintext-secret"
IMAGE_TOKEN = "image-token-plaintext-secret"


@pytest.mark.asyncio(loop_scope="function")
async def test_startup_logs_do_not_contain_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    recorder = LogRecorder()
    monkeypatch.setattr(bootstrap, "logger", recorder)
    # 图片 token 与接口 token 各自独立; 夹具默认关闭图片校验, 这里直接给出取值.
    monkeypatch.setattr(bootstrap, "resolve_image_token", lambda *_args: IMAGE_TOKEN)

    app = make_app(HotSettings(), tmp_path / "data", tmp_path / "logs", tmp_path / "files", token=API_TOKEN)
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()
    try:
        assert app.state.runtime.api_token == API_TOKEN, "token 校验仍然生效"

        text = recorder.text()
        assert API_TOKEN not in text
        assert IMAGE_TOKEN not in text

        for event, token in (("api token auth enabled", API_TOKEN), ("image token auth enabled", IMAGE_TOKEN)):
            fields = [fields for _level, name, fields in recorder.records if name == event]
            assert len(fields) == 1, text
            assert fields[0]["token_enabled"] is True
            assert fields[0]["token_length"] == len(token)
            fingerprint = fields[0]["token_fingerprint"]
            assert isinstance(fingerprint, str) and len(fingerprint) == 8
            assert fingerprint != token
    finally:
        await ctx.__aexit__(None, None, None)
