from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from ...config import HotSettings
from ...events import Event, EventType
from ...observability.redact import redact_hot
from ..deps import ConfigDep, PluginManagerDep, RuntimeDep

logger = structlog.get_logger()

router = APIRouter(prefix="/config", tags=["config"])


def _redacted_hot(hot: HotSettings) -> dict[str, Any]:
    """热配置的脱敏副本: 结构与 ``HotSettings`` 一致, 密钥类字段为占位符.

    响应不声明 ``response_model=HotSettings``: 占位符过不了 ``NotifyConfig.url`` 等字段的校验,
    用户也拿不到可以回写的明文.
    """
    return redact_hot(hot.model_dump(mode="json"))


@router.get("")
async def get_config(config: ConfigDep) -> dict[str, Any]:
    return _redacted_hot(config.hot)


@router.patch("")
async def update_config(req: dict[str, Any], config: ConfigDep, runtime: RuntimeDep) -> dict[str, Any]:
    """先用当前会话的插件目录校验新路由和插件配置, 再持久化."""
    changed_sections = list(req.keys())
    logger.info("config update requested", sections=changed_sections)

    try:
        preview = config.preview(req)
        if runtime.plugin_manager is not None:
            runtime.plugin_manager.validate_hot_settings(preview, require_available=False)
        config.update(req)
    except (KeyError, ValidationError, ValueError) as e:
        logger.warning("config update rejected", error=str(e), sections=changed_sections)
        raise HTTPException(status_code=422, detail=str(e)) from e

    await runtime.apply_rebuild()
    logger.info("runtime rebuilt after config update", sections=changed_sections)

    await runtime.event_bus.broadcast(Event(type=EventType.LOG, data={"type": "config.updated"}))

    return _redacted_hot(config.hot)


@router.get("/schema")
async def get_config_schema(plugin_manager: PluginManagerDep) -> dict:
    return plugin_manager.augment_config_schema(HotSettings.model_json_schema())
