"""启动期环境变量接口: 读、写、占位符、校验、注释保留."""

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


def env_path_of(app: FastAPI) -> Path:
    return env_file_path(data_dir_of(app))


async def test_empty_file_lists_managed_keys(client: AsyncClient, app: FastAPI) -> None:
    """没有文件也算正常: 页面照常拿到受管键清单与每项的属性."""
    response = await client.get(ENV_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is False
    assert body["entries"] == []
    assert body["path"] == str(env_path_of(app))
    by_key = {item["key"]: item for item in body["keys"]}
    assert by_key["AMANE_TOKEN"]["editable"] is True
    assert by_key["AMANE_TOKEN"]["secret"] is True
    assert by_key["AMANE_SAFE_DIRS"]["editable"] is False
    assert "安全目录" in by_key["AMANE_SAFE_DIRS"]["description"]
    assert "AMANE_DATA_DIR" in by_key


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

    path = env_path_of(app)
    assert "AMANE_TOKEN=s3cret" in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_empty_secret_is_not_reported_as_set(client: AsyncClient, app: FastAPI) -> None:
    """空敏感值回显空串而不是占位符 —— 否则页面会误显示「已设置」."""
    path = env_path_of(app)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('AMANE_TOKEN=""\n', encoding="utf-8")

    body = (await client.get(ENV_URL)).json()

    entry = next(item for item in body["entries"] if item["key"] == "AMANE_TOKEN")
    assert entry["value"] == ""
    assert entry["secret"] is True


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
    text = env_path_of(app).read_text(encoding="utf-8")
    assert "AMANE_TOKEN=real-token" in text
    assert PLACEHOLDER not in text


async def test_dropping_a_key_deletes_it(client: AsyncClient, app: FastAPI) -> None:
    """整份替换: 列表里没有的键会被删除, 回到默认值."""
    await client.put(
        ENV_URL,
        json={
            "entries": [{"key": "AMANE_TEST_LOG", "value": "1"}, {"key": "AMANE_UPDATE_URL", "value": "https://a.test"}]
        },
    )

    response = await client.put(ENV_URL, json={"entries": [{"key": "AMANE_UPDATE_URL", "value": "https://a.test"}]})

    assert response.status_code == 200
    text = env_path_of(app).read_text(encoding="utf-8")
    assert "AMANE_TEST_LOG" not in text


async def test_empty_value_deletes_the_key(client: AsyncClient, app: FastAPI) -> None:
    """空串表示「删除这一行」: 直接写 ``AMANE_TEST_LOG=""`` 会让重启后解析 bool 失败."""
    await client.put(ENV_URL, json={"entries": [{"key": "AMANE_TEST_LOG", "value": "1"}]})

    response = await client.put(ENV_URL, json={"entries": [{"key": "AMANE_TEST_LOG", "value": ""}]})

    assert response.status_code == 200
    assert "AMANE_TEST_LOG" not in env_path_of(app).read_text(encoding="utf-8")


async def test_comments_and_foreign_keys_survive_a_save(client: AsyncClient, app: FastAPI) -> None:
    """整份覆写不能抹掉运维写的注释与第三方键."""
    path = env_path_of(app)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# custom note\nHF_ENDPOINT=https://hf-mirror.com\nAMANE_TEST_LOG=0\n",
        encoding="utf-8",
    )

    response = await client.put(ENV_URL, json={"entries": [{"key": "AMANE_TEST_LOG", "value": "1"}]})

    assert response.status_code == 200
    text = path.read_text(encoding="utf-8")
    assert "# custom note" in text
    assert "HF_ENDPOINT=https://hf-mirror.com" in text
    assert "AMANE_TEST_LOG=1" in text
    assert [entry["key"] for entry in response.json()["entries"]] == ["AMANE_TEST_LOG"]


@pytest.mark.parametrize(
    ("key", "value", "detail"),
    [
        pytest.param("AMANE_NOT_A_FIELD", "x", "不支持的环境变量", id="unknown-key"),
        pytest.param("PATH", "/usr/bin", "不支持的环境变量", id="foreign-key"),
        pytest.param("AMANE_UPDATE_URL", "a\nb", "不能包含换行", id="newline"),
        pytest.param("AMANE_UPDATE_URL", "x" * 5000, "过长", id="too-long"),
        pytest.param("AMANE_SAFE_DIRS", "/", "不能在页面修改", id="readonly-key"),
        pytest.param("AMANE_SUPERVISED", "1", "不能在页面修改", id="readonly-flag"),
    ],
)
async def test_invalid_entries_are_rejected(client: AsyncClient, key: str, value: str, detail: str) -> None:
    """写坏环境变量的后果是下次起不来, 所以非法的键与值一律 422."""
    response = await client.put(ENV_URL, json={"entries": [{"key": key, "value": value}]})

    assert response.status_code == 422
    assert detail in response.json()["detail"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("AMANE_TRUSTED_ORIGINS", "foo", id="origin-without-scheme"),
        pytest.param("AMANE_CORS_ORIGINS", "not-a-url", id="cors-without-scheme"),
        pytest.param("AMANE_TEST_LOG", "maybe", id="bad-bool"),
    ],
)
async def test_bad_values_are_rejected_before_writing(client: AsyncClient, app: FastAPI, key: str, value: str) -> None:
    """值先按 ``ColdSettings`` 预校验: 磁盘上一个字节都不能改."""
    response = await client.put(ENV_URL, json={"entries": [{"key": key, "value": value}]})

    assert response.status_code == 422
    assert key in response.json()["detail"]
    assert not env_path_of(app).exists()


async def test_good_values_are_accepted_and_load(client: AsyncClient, app: FastAPI) -> None:
    """合法值能落盘, 并且用这份文件实例化 ``ColdSettings`` 成功."""
    response = await client.put(
        ENV_URL,
        json={
            "entries": [
                {"key": "AMANE_TRUSTED_ORIGINS", "value": "https://a.example.com:8443"},
                {"key": "AMANE_TEST_LOG", "value": "1"},
            ]
        },
    )

    assert response.status_code == 200
    text = env_path_of(app).read_text(encoding="utf-8")
    assert "AMANE_TRUSTED_ORIGINS=https://a.example.com:8443" in text
    assert "AMANE_TEST_LOG=1" in text


async def test_environment_shadowed_keys_are_flagged(client: AsyncClient) -> None:
    """进程环境变量里已有的键会被标出来 —— 启动时它优先于 ``.env``."""
    body = (await client.get(ENV_URL)).json()

    by_key = {item["key"]: item for item in body["keys"]}
    assert by_key["AMANE_DATA_DIR"]["overridden_by_env"] is True
    assert by_key["AMANE_UPDATE_URL"]["overridden_by_env"] is False
