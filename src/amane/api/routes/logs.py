"""运行日志 (``app.log``) 的尾部读取.

日志页的实时列表只存在浏览器内存里, 刷新页面就空了. 这个端点给页面一个回填历史的来源:
打开页面时先取最近若干行, 再叠加 WebSocket 推来的新事件.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query

from ...observability import TaskLogPage, read_app_log
from ..deps import ConfigDep

router = APIRouter(prefix="/logs", tags=["logs"])

LOGS_DEFAULT_LIMIT = 2000
LOGS_MAX_LIMIT = 20000


@router.get("")
async def get_logs(
    config: ConfigDep,
    limit: Annotated[
        int, Query(ge=1, le=LOGS_MAX_LIMIT, description="Max entries from the end of app.log")
    ] = LOGS_DEFAULT_LIMIT,
) -> TaskLogPage:
    """运行日志尾部 ``limit`` 行, 跨轮转文件回溯. 文件不存在时返回空页."""
    return await asyncio.to_thread(read_app_log, config.cold.log_dir, limit=limit)
