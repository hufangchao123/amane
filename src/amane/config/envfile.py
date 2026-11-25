"""环境变量文件 (``.env``) 的读写.

只承接启动期配置: 文件里写的是 ``AMANE_*`` 环境变量, 与 :class:`amane.config.manager.ColdSettings`
的字段一一对应. 运行期配置在 ``config.toml``, 由设置接口改; 这两处边界不要混。

三条硬约束, 违反任何一条都会让「改完重启就起不来」:

- 写入前必须按 :class:`ColdSettings` 预校验值, 否则错值要等重启才暴露;
- 只覆写受管键所在的行, 原文件里的注释、空行与第三方键 (如 ``HF_ENDPOINT``) 原样保留;
- 部署级护栏键 (数据目录 / 日志目录 / 安全目录 / 监管开关) 不允许从页面改.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from dotenv import dotenv_values
from pydantic import ValidationError

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

EDITABLE_KEYS: tuple[str, ...] = (
    "AMANE_TEST_LOG",
    "AMANE_TOKEN",
    "AMANE_IMAGE_TOKEN",
    "AMANE_TRUSTED_ORIGINS",
    "AMANE_CORS_ORIGINS",
    "AMANE_UPDATE_URL",
)
"""页面可编辑的键: 只影响本应用的行为, 改错也不会把部署弄坏."""

READONLY_KEYS: tuple[str, ...] = (
    "AMANE_DATA_DIR",
    "AMANE_LOG_DIR",
    "AMANE_SAFE_DIRS",
    "AMANE_SUPERVISED",
)
"""页面只读展示的键: 属于部署级护栏, 只能由容器环境变量或部署脚本设置."""

READONLY_REASONS: dict[str, str] = {
    "AMANE_DATA_DIR": "数据目录只在启动时解析一次, 在文件里改会指向另一个库",
    "AMANE_LOG_DIR": "日志目录由部署决定, 与卷挂载绑定",
    "AMANE_SAFE_DIRS": "安全目录边界由部署决定, 放开会让越界读写失去约束",
    "AMANE_SUPERVISED": "进程监管契约, 由容器/服务管理器决定",
}

_STARTUP_HEADER = "# amane 启动配置; 由设置页写入, 改动需重启生效"


def env_file_path(data_dir: Path) -> Path:
    """环境变量文件的位置: ``<data_dir>/.env``.

    ``ColdSettings`` 的 ``env_file`` 里第三条就是这个路径 (启动时按 ``AMANE_DATA_DIR`` 解析),
    编辑接口与加载器必须指向同一份文件, 否则改了不生效.
    """
    return data_dir / ".env"


def editable_keys() -> tuple[str, ...]:
    """允许在设置页编辑的键."""
    return EDITABLE_KEYS


def readonly_keys() -> tuple[str, ...]:
    """只在页面展示、不允许编辑的键."""
    return READONLY_KEYS


def managed_keys() -> tuple[str, ...]:
    """本接口管辖的全部键 (受管 = 可编辑 + 只读)."""
    return (*EDITABLE_KEYS, *READONLY_KEYS)


def key_docs() -> dict[str, str]:
    """键的说明, 取自字段描述的首行 (给前端的提示文字)."""
    docs: dict[str, str] = {}
    for key in managed_keys():
        name = field_name(key)
        if name is None:
            continue
        field = ColdSettings.model_fields.get(name)
        text = (field.description or "").strip().split("\n", 1)[0] if field is not None else ""
        docs[key] = text or READONLY_REASONS.get(key, "")
    return docs


def field_name(key: str) -> str | None:
    """``AMANE_SAFE_DIRS`` -> ``safe_dirs``; 不是 ``ColdSettings`` 的字段时给 ``None``."""
    if not key.startswith(ENV_PREFIX):
        return None
    name = key[len(ENV_PREFIX) :].lower()
    return name if name in ColdSettings.model_fields else None


def is_secret_key(key: str) -> bool:
    """按键名判断是否敏感 (值不落前端, 读取时替换成占位符)."""
    upper = key.upper()
    return any(part in upper for part in _SECRET_PARTS)


def overridden_by_env(key: str) -> bool:
    """进程环境变量里有这个键 —— 启动时它的优先级高于 ``.env``, 改文件不影响运行中的进程."""
    return key in os.environ


def read_env_file(path: Path) -> dict[str, str]:
    """读成键值对; 用 dotenv 解析, 与启动时的加载器保持同一套语法."""
    if not path.is_file():
        return {}
    try:
        raw = dotenv_values(path, encoding="utf-8")
    except OSError as exc:
        logger.warning("env file unreadable", path=str(path), error=str(exc))
        return {}
    return {key: ("" if value is None else value) for key, value in raw.items() if key}


def read_env_text(path: Path) -> str:
    """原样读取文件文本; 不存在给空串."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def render_env_file(values: Mapping[str, str]) -> str:
    """渲染一份完整文件 (只在新建文件时使用, 已有文件按行更新)."""
    body = [f"{key}={_quote(values[key])}" for key in sorted(values)]
    return "\n".join([_STARTUP_HEADER, *body]) + "\n"


def update_env_text(text: str, values: Mapping[str, str]) -> str:
    """按行更新: 受管键就地替换或删除整行, 其余行 (注释 / 空行 / 第三方键) 原样保留."""
    managed = set(managed_keys())
    lines = text.splitlines()
    out: list[str] = []
    written: set[str] = set()
    for line in lines:
        match = _LINE_RE.match(line.strip())
        if match is None or match.group("key") not in managed:
            out.append(line)
            continue
        key = match.group("key")
        if key in values:
            out.append(f"{key}={_quote(values[key])}")
            written.add(key)
    out.extend(f"{key}={_quote(values[key])}" for key in values if key not in written)
    body = "\n".join(out).rstrip("\n")
    if not body:
        return ""
    return body + "\n"


def write_env_file(path: Path, values: Mapping[str, str]) -> None:
    """按行更新后原子写入, 权限 0600 —— 文件里可能有 token.

    临时文件用 ``mkstemp`` (创建即 0600 且名字唯一), 写完 ``fsync`` 再 ``os.replace``:
    并发保存不会互相覆盖, 也不会出现「明文短暂全局可读」的窗口.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_env_text(path)
    text = update_env_text(existing, values) if existing else render_env_file(values)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(0o600)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
    path.chmod(0o600)


def validate_entries(entries: Mapping[str, str]) -> dict[str, str]:
    """校验一份待写入的键值 (只看键与字符, 值语义见 :func:`validate_values`).

    空值表示「删除这一行, 回到默认值」—— 写 ``AMANE_TEST_LOG=""`` 落地后重启会解析失败.
    """
    editable = set(EDITABLE_KEYS)
    readonly = set(READONLY_KEYS)
    blocked = sorted(set(entries) & readonly)
    if blocked:
        reason = READONLY_REASONS.get(blocked[0], "属于部署级配置")
        raise ValueError(f"{blocked[0]} 不能在页面修改: {reason}")
    unknown = sorted(set(entries) - editable - readonly)
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
        if value == "":
            continue
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


def validate_values(merged: Mapping[str, str]) -> None:
    """按 :class:`ColdSettings` 预校验合并后的值; 不合法抛 ``ValueError``.

    校验用 ``model_validate`` 直接喂字段值, 不读环境变量、不改 ``os.environ``, 所以
    「进程里已有的值」不会掩盖待写入文件里的错值.
    """
    fields: dict[str, Any] = {}
    for key, value in merged.items():
        name = field_name(key)
        if name is None:
            continue
        fields[name] = value
    if not fields:
        return
    try:
        ColdSettings.model_validate(fields)
    except ValidationError as exc:
        raise ValueError(_format_errors(exc)) from None


def _format_errors(exc: ValidationError) -> str:
    """把 pydantic 的错误翻成一行给人看的文本."""
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ())) or "配置"
        key = f"{ENV_PREFIX}{location.upper()}"
        parts.append(f"{key}: {error.get('msg', '值不合法')}")
    return "; ".join(parts) or "环境变量的值不合法"


def _unquote(value: str) -> str:
    """与 :func:`_quote` 对称: 双引号内反转义 ``\\`` 与 ``"``, 单引号按字面量."""
    if len(value) >= 2 and value[0] == value[-1]:
        if value[0] == '"':
            return re.sub(r'\\(["\\])', r"\1", value[1:-1])
        if value[0] == "'":
            return value[1:-1]
    return value


def _quote(value: str) -> str:
    """只在必要时加引号, 保持文件可读; 加引号时按 :func:`_unquote` 的规则转义."""
    if value == "" or any(char in value for char in " \t#'\"\\"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value
