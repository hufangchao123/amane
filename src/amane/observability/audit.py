"""写操作留痕: 请求返回前写一行, 失败不影响响应.

留痕不参与请求结果: 写失败只记一条日志. 这类记录用于事后核对
「谁在什么时候改了什么」, 因此只记方法、路径、脱敏后的查询串、状态码与耗时, 不记请求体
与响应体 —— 请求体里可能有密钥与个人数据, 而且体积不可控.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ..utils.urls import redact_query

if TYPE_CHECKING:
    from ..db.repository import Repository

logger = structlog.get_logger("amane.audit")

#: 留痕表保留条数; 超过后按时间倒序修剪最旧的一批.
AUDIT_KEEP = 5000

#: 需要留痕的方法; 读方法与 OPTIONS 不记.
AUDITED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: 留痕不记的路径: 就绪检查、接入引导、长连接.
AUDIT_SKIP_PATHS = frozenset({"/api/health", "/api/agent-guide", "/api/ws"})


class AuditLog:
    """把一条留痕写进库; 只做这一件事, 因此没有生命周期需要管理."""

    def __init__(self, repo: Repository, *, keep: int = AUDIT_KEEP) -> None:
        self._repo = repo
        self._keep = keep
        self._since_prune = 0

    async def record(self, *, method: str, path: str, query: str, status: int, duration_ms: float, client: str) -> bool:
        """写一条留痕, 返回是否写成; 失败只记日志.

        写失败 (库忙、表缺) 不让写请求变成 500: 留痕是旁路, 用户的操作已经完成.
        """
        try:
            await self._repo.add_audit_record(
                method=method,
                path=path,
                query=redact_query(query),
                status=status,
                duration_ms=max(0, int(duration_ms)),
                client=client,
            )
        except Exception:
            logger.exception("audit write failed", path=path, method=method)
            return False
        self._since_prune += 1
        if self._since_prune >= self._keep:
            self._since_prune = 0
            try:
                await self._repo.prune_audit_records(self._keep)
            except Exception:
                logger.exception("audit prune failed")
        return True


def should_audit(method: str, path: str) -> bool:
    """写方法且落在 API 下、又不属豁免路径时才留痕."""
    return method in AUDITED_METHODS and path.startswith("/api/") and path not in AUDIT_SKIP_PATHS
