"""启动期环境变量 (``.env``) 的读写.

与 ``/api/config`` 分工明确: 那里是运行期配置 (``config.toml``, 热生效); 这里是启动期配置
(``AMANE_*`` 环境变量, 改完需重启进程). 两者都不会把敏感值回显给页面。

写盘前一律按 :class:`amane.config.manager.ColdSettings` 预校验: 环境变量写错的后果是
「进程起不来」, 而重启之后 Web 界面也打不开, 没有第二次机会。
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, HTTPException

from ...config.envfile import (
    PLACEHOLDER,
    editable_keys,
    env_file_path,
    is_secret_key,
    key_docs,
    managed_keys,
    merge_placeholders,
    overridden_by_env,
    read_env_file,
    validate_entries,
    validate_values,
    write_env_file,
)
from ..deps import ConfigDep, RuntimeDep
from ..models.env import EnvEntry, EnvFilePatch, EnvFileResponse, EnvKeyMeta

logger = structlog.get_logger("amane.env")

router = APIRouter(prefix="/env", tags=["env"])

_WRITE_LOCK = asyncio.Lock()
"""同一进程内串行化写盘: 两个页面同时保存不能交错覆盖."""


def _keys_meta() -> list[EnvKeyMeta]:
    docs = key_docs()
    editable = set(editable_keys())
    return [
        EnvKeyMeta(
            key=key,
            secret=is_secret_key(key),
            editable=key in editable,
            overridden_by_env=overridden_by_env(key),
            description=docs.get(key, ""),
        )
        for key in managed_keys()
    ]


async def _read_state(config: ConfigDep, runtime: RuntimeDep) -> EnvFileResponse:
    """读取文件状态; 磁盘 IO 走线程, 不阻塞事件循环."""
    path = env_file_path(config.cold.data_dir)
    values = await asyncio.to_thread(read_env_file, path)
    docs = key_docs()
    editable = set(editable_keys())
    managed = set(managed_keys())
    entries = [
        EnvEntry(
            key=key,
            value=PLACEHOLDER if (is_secret_key(key) and value) else value,
            secret=is_secret_key(key),
            editable=key in editable,
            overridden_by_env=overridden_by_env(key),
            description=docs.get(key, ""),
        )
        for key, value in sorted(values.items())
        if key in managed
    ]
    return EnvFileResponse(
        path=str(path),
        exists=await asyncio.to_thread(path.is_file),
        entries=entries,
        keys=_keys_meta(),
        supervised=runtime.config.cold.supervised,
    )


@router.get("", response_model=EnvFileResponse)
async def get_env(config: ConfigDep, runtime: RuntimeDep) -> EnvFileResponse:
    """当前环境变量文件; 敏感值只回显占位符, 页面据此判断「已设置」."""
    return await _read_state(config, runtime)


@router.put("", response_model=EnvFileResponse)
async def put_env(patch: EnvFilePatch, config: ConfigDep, runtime: RuntimeDep) -> EnvFileResponse:
    """整份替换受管键; 占位符 ``***`` 表示保持原值, 空串表示删除该键.

    键或值不合法一律 422: 值先按 ``ColdSettings`` 预校验, 拒绝写入会让进程起不来的取值。
    原文件里的注释、空行与第三方键 (如 ``HF_ENDPOINT``) 保持不动。

    被进程环境变量接管的键 (``overridden_by_env``) 允许写盘但不会生效 —— 启动时环境变量优先,
    页面据此把这些行标成只读, 免得用户以为改完没保存.
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

    async with _WRITE_LOCK:
        current = await asyncio.to_thread(read_env_file, path)
        merged = merge_placeholders(cleaned, current)
        shadowed = sorted(key for key in merged if overridden_by_env(key))
        try:
            validate_values(merged)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        try:
            await asyncio.to_thread(write_env_file, path, merged)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"写入环境变量文件失败: {exc}") from None

    logger.info("env file written", path=str(path), keys=sorted(merged), shadowed=shadowed)
    return await _read_state(config, runtime)
