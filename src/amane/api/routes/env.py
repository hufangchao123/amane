"""启动期环境变量 (``.env``) 的读写.

与 ``/api/config`` 分工明确: 那里是运行期配置 (``config.toml``, 热生效); 这里是启动期配置
(``AMANE_*`` 环境变量, 改完需重启进程). 两者都不会把敏感值回显给页面。
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException

from ...config.envfile import (
    PLACEHOLDER,
    editable_keys,
    env_file_path,
    is_secret_key,
    key_docs,
    merge_placeholders,
    read_env_file,
    validate_entries,
    write_env_file,
)
from ..deps import ConfigDep
from ..models.env import EnvEntry, EnvFilePatch, EnvFileResponse

logger = structlog.get_logger("amane.env")

router = APIRouter(prefix="/env", tags=["env"])


@router.get("", response_model=EnvFileResponse)
async def get_env(config: ConfigDep) -> EnvFileResponse:
    """当前环境变量文件; 敏感值只回显占位符, 页面据此判断「已设置」."""
    path = env_file_path(config.cold.data_dir)
    values = read_env_file(path)
    docs = key_docs()
    entries = [
        EnvEntry(
            key=key,
            value=PLACEHOLDER if is_secret_key(key) else value,
            secret=is_secret_key(key),
            description=docs.get(key, ""),
        )
        for key, value in sorted(values.items())
    ]
    return EnvFileResponse(
        path=str(path),
        exists=path.is_file(),
        entries=entries,
        editable_keys=list(editable_keys()),
    )


@router.put("", response_model=EnvFileResponse)
async def put_env(patch: EnvFilePatch, config: ConfigDep) -> EnvFileResponse:
    """整份替换环境变量文件; 占位符 ``***`` 表示保持原值.

    写盘失败 500, 键或值不合法 422 —— 环境变量写错的后果是「进程起不来」, 宁可拒绝。
    """
    path = env_file_path(config.cold.data_dir)
    submitted: dict[str, str] = {}
    for entry in patch.entries:
        key = entry.key.strip()
        if not key:
            continue
        submitted[key] = entry.value
    try:
        cleaned = validate_entries(submitted)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    merged = merge_placeholders(cleaned, read_env_file(path))
    try:
        write_env_file(path, merged)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入环境变量文件失败: {exc}") from None
    logger.info("env file written", path=str(path), keys=sorted(merged))
    return await get_env(config)
