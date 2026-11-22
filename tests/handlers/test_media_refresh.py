"""MEDIA_REFRESH handler: 未配置直接失败, 正常路径把目标地址写进结果, 服务器错误记进结果."""

from __future__ import annotations

from typing import Literal, Self

import pytest

from amane.config import HotSettings, MediaServerConfig
from amane.handlers import MediaRefreshHandler, MediaRefreshPayload
from amane.media_server import MediaServerError


class _FakeClient:
    def __init__(self, *, error: str | None = None) -> None:
        self.error = error
        self.refresh_calls = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def refresh_library(self) -> None:
        self.refresh_calls += 1
        if self.error is not None:
            raise MediaServerError(self.error)


_KINDS: tuple[Literal["emby", "jellyfin"], ...] = ("emby", "jellyfin")


def _hot(kind: Literal["emby", "jellyfin"] = "emby") -> HotSettings:
    """只给刷新用到的项: 服务器类型、地址与密钥."""
    return HotSettings(media_server=MediaServerConfig(url="http://media.local:8096", api_key="key", kind=kind))


@pytest.mark.asyncio(loop_scope="function")
async def test_unconfigured_server_fails_without_calling_client() -> None:
    client = _FakeClient()
    handler = MediaRefreshHandler(HotSettings(), client_factory=lambda _: client)

    result = await handler.handle(MediaRefreshPayload())

    assert not result.success
    assert result.error is not None and "media_server" in result.error
    assert client.refresh_calls == 0


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.parametrize("kind", _KINDS)
async def test_refresh_reports_target(kind: Literal["emby", "jellyfin"]) -> None:
    client = _FakeClient()
    handler = MediaRefreshHandler(_hot(kind=kind), client_factory=lambda _: client)

    result = await handler.handle(MediaRefreshPayload())

    assert result.success
    assert result.result is not None
    assert result.result.kind == kind
    assert result.result.url == "http://media.local:8096"
    assert client.refresh_calls == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_server_error_keeps_target_in_result() -> None:
    client = _FakeClient(error="认证失败 (HTTP 401): media_server.api_key 无效")
    handler = MediaRefreshHandler(_hot(), client_factory=lambda _: client)

    result = await handler.handle(MediaRefreshPayload())

    assert not result.success
    assert result.result is not None and result.result.url == "http://media.local:8096"
    assert result.error is not None and "401" in result.error
    assert client.refresh_calls == 1
