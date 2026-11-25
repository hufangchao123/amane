"""``.env`` 读写: 与 dotenv 的转义口径一致, 写盘只动受管行."""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

import pytest
from dotenv import dotenv_values

from amane.config.envfile import (
    PLACEHOLDER,
    editable_keys,
    env_file_path,
    is_secret_key,
    managed_keys,
    merge_placeholders,
    overridden_by_env,
    read_env_file,
    validate_entries,
    validate_values,
    write_env_file,
)

if TYPE_CHECKING:
    from pathlib import Path

TRICKY_VALUES = [
    pytest.param('p@ss"wo\\rd', id="quotes-and-backslash"),
    pytest.param("/media/媒体 库#1", id="space-and-hash"),
    pytest.param("a=b", id="equals"),
    pytest.param("'single'", id="single-quotes"),
    pytest.param("https://example.test/x?y=1&z=2", id="url"),
]


def test_managed_keys_cover_cold_settings() -> None:
    """受管清单来自 ``ColdSettings`` 的字段, 只读项与可编辑项不重叠."""
    managed = set(managed_keys())
    editable = set(editable_keys())

    assert "AMANE_TEST_LOG" in managed
    assert "AMANE_SAFE_DIRS" in managed
    assert editable < managed
    assert "AMANE_SAFE_DIRS" not in editable
    assert "AMANE_TOKEN" in editable


def test_secret_keys_are_the_credential_ones() -> None:
    assert is_secret_key("AMANE_TOKEN")
    assert is_secret_key("AMANE_IMAGE_TOKEN")
    assert not is_secret_key("AMANE_TEST_LOG")


@pytest.mark.parametrize("value", TRICKY_VALUES)
def test_round_trip_matches_dotenv(tmp_path: Path, value: str) -> None:
    """写出去的值必须能被 dotenv 原样解析回来 —— 页面上看到的就是启动时的值."""
    path = env_file_path(tmp_path)

    write_env_file(path, {"AMANE_UPDATE_URL": value})

    assert read_env_file(path) == {"AMANE_UPDATE_URL": value}
    assert dotenv_values(path)["AMANE_UPDATE_URL"] == value


def test_repeated_saves_do_not_double_escape(tmp_path: Path) -> None:
    """反复保存同一个含引号的值, 结果不能层层翻倍."""
    path = env_file_path(tmp_path)
    value = 'say "hi" \\ bye'

    for _ in range(3):
        write_env_file(path, {"AMANE_UPDATE_URL": value})

    assert read_env_file(path) == {"AMANE_UPDATE_URL": value}


def test_write_keeps_comments_and_foreign_keys(tmp_path: Path) -> None:
    """运维手写的注释与第三方键 (``HF_ENDPOINT``) 不在受管范围内, 保存时不能动."""
    path = env_file_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# 手工注释\nHF_ENDPOINT=https://hf-mirror.com\nAMANE_TEST_LOG=0\nAMANE_UPDATE_URL=https://old.test\n",
        encoding="utf-8",
    )

    write_env_file(path, {"AMANE_TEST_LOG": "1"})

    text = path.read_text(encoding="utf-8")
    assert "# 手工注释" in text
    assert "HF_ENDPOINT=https://hf-mirror.com" in text
    assert "AMANE_TEST_LOG=1" in text
    assert "AMANE_UPDATE_URL" not in text
    managed = {key: value for key, value in read_env_file(path).items() if key.startswith("AMANE_")}
    assert managed == {"AMANE_TEST_LOG": "1"}


def test_write_is_private_and_leaves_no_temporaries(tmp_path: Path) -> None:
    """文件是 600, 且不留下明文临时文件."""
    path = env_file_path(tmp_path)

    write_env_file(path, {"AMANE_TOKEN": "s3cret"})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [item.name for item in path.parent.iterdir() if item.name.endswith(".tmp")] == []


def test_empty_values_are_dropped_before_writing(tmp_path: Path) -> None:
    """空串表示删除: 合并占位符时就把空值摘掉, 免得落地成 ``AMANE_TEST_LOG=""``."""
    path = env_file_path(tmp_path)
    write_env_file(path, {"AMANE_TEST_LOG": "1", "AMANE_TOKEN": "t"})

    cleaned = validate_entries({"AMANE_TEST_LOG": "", "AMANE_TOKEN": "t"})
    write_env_file(path, merge_placeholders(cleaned, read_env_file(path)))

    assert read_env_file(path) == {"AMANE_TOKEN": "t"}


@pytest.mark.parametrize(
    ("key", "value", "detail"),
    [
        pytest.param("AMANE_NOT_A_FIELD", "x", "不支持的环境变量", id="unknown"),
        pytest.param("HF_ENDPOINT", "x", "不支持的环境变量", id="foreign"),
        pytest.param("AMANE_SAFE_DIRS", "/", "不能在页面修改", id="readonly"),
        pytest.param("AMANE_SUPERVISED", "1", "不能在页面修改", id="readonly-flag"),
        pytest.param("AMANE_UPDATE_URL", "a\nb", "不能包含换行", id="newline"),
        pytest.param("AMANE_UPDATE_URL", "x" * 5000, "过长", id="too-long"),
    ],
)
def test_invalid_entries_are_rejected(key: str, value: str, detail: str) -> None:
    with pytest.raises(ValueError, match=detail):
        validate_entries({key: value})


@pytest.mark.parametrize(
    ("values", "detail"),
    [
        pytest.param({"AMANE_TRUSTED_ORIGINS": "foo"}, "AMANE_TRUSTED_ORIGINS", id="origin"),
        pytest.param({"AMANE_CORS_ORIGINS": "bar"}, "AMANE_CORS_ORIGINS", id="cors"),
        pytest.param({"AMANE_TEST_LOG": "maybe"}, "AMANE_TEST_LOG", id="bool"),
    ],
)
def test_values_are_validated_by_cold_settings(values: dict[str, str], detail: str) -> None:
    """预校验用的是 ``ColdSettings`` 自己, 所以「能存进去」等于「重启不会炸」."""
    with pytest.raises(ValueError, match=detail):
        validate_values(values)


def test_valid_values_pass() -> None:
    validate_values(
        {
            "AMANE_TEST_LOG": "0",
            "AMANE_TRUSTED_ORIGINS": "https://a.example.com:8443",
            "AMANE_UPDATE_URL": "https://example.test/latest",
            "AMANE_TOKEN": "s3cret",
        }
    )


def test_merge_placeholders_keeps_stored_value() -> None:
    """``***`` 表示保持原值; 空串表示删除; 其余按提交值覆盖."""
    cleaned = validate_entries(
        {"AMANE_TOKEN": PLACEHOLDER, "AMANE_TEST_LOG": "", "AMANE_UPDATE_URL": "https://new.test"}
    )
    merged = merge_placeholders(
        cleaned,
        {"AMANE_TOKEN": "real", "AMANE_TEST_LOG": "1", "AMANE_UPDATE_URL": "https://old.test"},
    )

    assert merged == {"AMANE_TOKEN": "real", "AMANE_UPDATE_URL": "https://new.test"}


def test_overridden_by_env_reads_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """容器环境变量优先于 ``.env``, 页面据此把对应行标成只读."""
    monkeypatch.setenv("AMANE_UPDATE_URL", "https://from-env.test")

    assert overridden_by_env("AMANE_UPDATE_URL")
    monkeypatch.delenv("AMANE_UPDATE_URL")

    assert not overridden_by_env("AMANE_UPDATE_URL")


def test_env_path_lives_in_the_data_dir(tmp_path: Path) -> None:
    assert env_file_path(tmp_path) == tmp_path / ".env"
    assert os.fspath(env_file_path(tmp_path)).endswith(".env")
