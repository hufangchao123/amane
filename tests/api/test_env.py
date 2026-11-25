"""启动期环境变量接口: 读、写、占位符、校验."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

import pytest

from amane.config.envfile import PLACEHOLDER, env_file_path

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from httpx2 import AsyncClient

pytestmark = pytest.mark.asyncio(loop_scope="function")

ENV_URL = "env"
"""客户端基址已含 ``/api`` 前缀, 这里只写相对段."""


def data_dir_of(app: FastAPI) -> Path:
    """运行实例的数据目录; 环境变量文件就落在它下面."""
    return app.state.runtime.config.cold.data_dir


async def test_empty_file_reports_editable_keys(client: AsyncClient, app: FastAPI) -> None:
    """没有文件也算正常: 页面照常列出可编辑的键, 不抛 404."""
    response = await client.get(ENV_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is False
    assert body["entries"] == []
    assert "AMANE_DATA_DIR" in body["editable_keys"]
    assert body["path"] == str(env_file_path(data_dir_of(app)))


async def test_put_round_trip_hides_secrets(client: AsyncClient, app: FastAPI) -> None:
    """写入后能读回; token 这类键只回占位符, 磁盘上是明文."""
    response = await client.put(
        ENV_URL,
        json={"entries": [{"key": "AMANE_UPDATE_URL", "value": "https://example.test/latest"}]},
    )

    assert response.status_code == 200
    assert [entry["key"] for entry in response.json()["entries"]] == ["AMANE_UPDATE_URL"]

    response = await client.put(
        ENV_URL,
        json={
            "entries": [
                {"key": "AMANE_UPDATE_URL", "value": "https://example.test/latest"},
                {"key": "AMANE_TOKEN", "value": "s3cret"},
            ]
        },
    )

    body = response.json()
    assert response.status_code == 200
    by_key = {entry["key"]: entry for entry in body["entries"]}
    assert by_key["AMANE_TOKEN"]["value"] == PLACEHOLDER
    assert by_key["AMANE_TOKEN"]["secret"] is True
    assert by_key["AMANE_UPDATE_URL"]["value"] == "https://example.test/latest"

    path = env_file_path(data_dir_of(app))
    assert "AMANE_TOKEN=s3cret" in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_placeholder_keeps_the_stored_value(client: AsyncClient, app: FastAPI) -> None:
    """页面回传 ``***`` 表示保持原值, 不能把占位符写进文件."""
    await client.put(ENV_URL, json={"entries": [{"key": "AMANE_TOKEN", "value": "real-token"}]})

    response = await client.put(
        ENV_URL,
        json={
            "entries": [
                {"key": "AMANE_TOKEN", "value": PLACEHOLDER},
                {"key": "AMANE_UPDATE_URL", "value": "https://example.test/x"},
            ]
        },
    )

    assert response.status_code == 200
    text = env_file_path(data_dir_of(app)).read_text(encoding="utf-8")
    assert "AMANE_TOKEN=real-token" in text
    assert PLACEHOLDER not in text


async def test_dropping_a_key_deletes_it(client: AsyncClient, app: FastAPI) -> None:
    """整份替换: 列表里没有的键会被删除, 回到默认值."""
    await client.put(
        ENV_URL,
        json={"entries": [{"key": "AMANE_TOKEN", "value": "t"}, {"key": "AMANE_LOG_DIR", "value": "/logs"}]},
    )

    response = await client.put(ENV_URL, json={"entries": [{"key": "AMANE_TOKEN", "value": PLACEHOLDER}]})

    assert response.status_code == 200
    text = env_file_path(data_dir_of(app)).read_text(encoding="utf-8")
    assert "AMANE_LOG_DIR" not in text


@pytest.mark.parametrize(
    ("key", "value", "detail"),
    [
        pytest.param("AMANE_NOT_A_FIELD", "x", "不支持的环境变量", id="unknown-key"),
        pytest.param("PATH", "/usr/bin", "不支持的环境变量", id="foreign-key"),
        pytest.param("AMANE_SAFE_DIRS", "a\nAMANE_TOKEN=evil", "不能包含换行", id="newline"),
        pytest.param("AMANE_UPDATE_URL", "x" * 5000, "过长", id="too-long"),
    ],
)
async def test_invalid_entries_are_rejected(client: AsyncClient, key: str, value: str, detail: str) -> None:
    """写坏环境变量的后果是下次起不来, 所以非法的键与值一律 422."""
    response = await client.put(ENV_URL, json={"entries": [{"key": key, "value": value}]})

    assert response.status_code == 422
    assert detail in response.json()["detail"]


async def test_values_with_spaces_are_quoted(client: AsyncClient, app: FastAPI) -> None:
    """值的引号规则: 有空格或井号时加引号, 读回来还是原值."""
    value = "/media/媒体 库#1"
    response = await client.put(ENV_URL, json={"entries": [{"key": "AMANE_SAFE_DIRS", "value": value}]})

    assert response.status_code == 200
    assert response.json()["entries"][0]["value"] == value
    text = env_file_path(data_dir_of(app)).read_text(encoding="utf-8")
    assert f'"{value}"' in text
