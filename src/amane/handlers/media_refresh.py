"""让 Emby / Jellyfin 重新扫描媒体库.

媒体服务器只提供整库刷新: 没有按路径或按库的入参, 也没有返回内容, 因此任务结果只有目标地址与
服务器类型. 连接失败与认证失败都记进结果, 不抛出: 抛出只会让任务进入重试, 而缺配置与密钥无效
两种情形重试同样失败.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..media_server import MediaServerClient, MediaServerError
from .models import MediaRefreshPayload, MediaRefreshResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..config import HotSettings, MediaServerConfig
    from ..media_server import MediaRefreshClientLike


class MediaRefreshHandler(TaskHandler[MediaRefreshPayload, MediaRefreshResult]):
    def __init__(
        self,
        config: HotSettings,
        client_factory: Callable[[MediaServerConfig], MediaRefreshClientLike] = MediaServerClient,
    ) -> None:
        super().__init__(payload_t=MediaRefreshPayload, result_t=MediaRefreshResult)
        self._config = config
        self._client_factory = client_factory

    async def handle(self, payload: MediaRefreshPayload) -> TaskResult[MediaRefreshResult]:
        cfg = self._config.media_server
        if not cfg.enabled or cfg.url is None:
            return TaskResult(success=False, error="媒体服务器未配置: 需要 media_server.url 与 media_server.api_key")

        result = MediaRefreshResult(kind=cfg.kind, url=cfg.url)
        try:
            async with self._client_factory(cfg) as client:
                await client.refresh_library()
        except MediaServerError as error:
            return TaskResult(success=False, result=result, error=str(error))
        return TaskResult(success=True, result=result)
