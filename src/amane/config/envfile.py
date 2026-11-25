"""环境变量文件 (``.env``) 的读写.

只承接启动期配置: 文件里写的是 ``AMANE_*`` 环境变量, 与 :class:`amane.config.manager.ColdSettings`
的字段一一对应. 运行期配置在 ``config.toml``, 由设置接口改; 这两处边界不要混。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from .manager import ColdSettings

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = structlog.get_logger()

ENV_PREFIX = "AMANE_"

_KEY_RE = re.compile(rf"^{ENV_PREFIX}[A-Z0-9_]*$")

_SECRET_PARTS = ("TOKEN", "SECRET", "PASSWORD", "KEY")

_LINE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")

MAX_VALUE_LENGTH = 4096

PLACEHOLDER = "***"
"""已配置的敏感值在读取接口里的替身; 提交这个值表示保持原值."""


def env_file_path(data_dir: Path) -> Path:
    """环境变量文件的位置: ``<data_dir>/.env``.

    ``ColdSettings`` 的 ``env_file`` 里第三条就是这个路径 (启动时按 ``AMANE_DATA_DIR`` 解析),
    编辑接口与加载器必须指向同一份文件, 否则改了不生效。
    """
    return data_dir / ".env"


def editable_keys() -> tuple[str, ...]:
    """允许在设置页编辑的键: ``ColdSettings`` 的每个字段一条."""
    return tuple(f"{ENV_PREFIX}{name.upper()}" for name in ColdSettings.model_fields)


def key_docs() -> dict[str, str]:
    """键的说明, 取自字段描述的首行 (给前端的提示文字)."""
    docs: dict[str, str] = {}
    for name, field in ColdSettings.model_fields.items():
        text = (field.description or "").strip().split("\n", 1)[0]
        docs[f"{ENV_PREFIX}{name.upper()}"] = text
    return docs


def is_secret_key(key: str) -> bool:
    """按键名判断是否敏感 (值不落前端, 读取时替换成占位符)."""
    upper = key.upper()
    return any(part in upper for part in _SECRET_PARTS)


def read_env_file(path: Path) -> dict[str, str]:
    """读成键值对; 忽略注释与空行, 重复键以最后一次为准."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("env file unreadable", path=str(path), error=str(exc))
        return {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _LINE_RE.match(stripped)
        if match is None:
            continue
        values[match.group("key")] = _unquote(match.group("value").strip())
    return values


def render_env_file(values: Mapping[str, str]) -> str:
    """按键名排序渲染; 每行一条, 值为空也保留 (空值表示使用默认)."""
    lines = ["# amane 启动配置; 由设置页写入, 改动需重启生效"]
    lines.extend(f"{key}={_quote(values[key])}" for key in sorted(values))
    return "\n".join(lines) + "\n"


def write_env_file(path: Path, values: Mapping[str, str]) -> None:
    """原子写入 (临时文件 + 替换), 权限 0600 —— 文件里可能有 token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(render_env_file(values), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    path.chmod(0o600)


def validate_entries(entries: Mapping[str, str]) -> dict[str, str]:
    """校验一份待写入的完整配置; 非法键或值直接抛 ValueError (路由转 422)."""
    allowed = set(editable_keys())
    unknown = sorted(set(entries) - allowed)
    if unknown:
        raise ValueError(f"不支持的环境变量: {', '.join(unknown)}")
    cleaned: dict[str, str] = {}
    for key, value in entries.items():
        if _KEY_RE.fullmatch(key) is None:
            raise ValueError(f"环境变量名不合法: {key}")
        if "\n" in value or "\r" in value or "\0" in value:
            raise ValueError(f"{key} 的值不能包含换行或空字符")
        if len(value) > MAX_VALUE_LENGTH:
            raise ValueError(f"{key} 的值过长 (上限 {MAX_VALUE_LENGTH} 字符)")
        cleaned[key] = value
    return cleaned


def merge_placeholders(submitted: Mapping[str, str], current: Mapping[str, str]) -> dict[str, str]:
    """提交里的占位符表示「保持原值」: 原文件里有就沿用, 没有就丢弃该键."""
    merged: dict[str, str] = {}
    for key, value in submitted.items():
        if value == PLACEHOLDER:
            if key in current:
                merged[key] = current[key]
            continue
        merged[key] = value
    return merged


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _quote(value: str) -> str:
    """只在必要时加引号, 保持文件可读."""
    if value == "" or any(char in value for char in " \t#'\""):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value
