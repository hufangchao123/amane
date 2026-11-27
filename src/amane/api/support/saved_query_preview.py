"""预设卡片的配图: 跑一次预设, 拿第一条结果的图.

预设卡原先只有名字与描述, 一串「丝袜」「4K」的名字放在一起很难分辨; 取第一条结果的海报/头像
当缩略图, 一眼能看出这条预设大概选中了什么.

代价是列表接口要真的执行一遍 SQL, 因此两条约束: 只给 ``metadata`` / ``actor`` 两类做 (``data``
预设的结果是行数据, 与图无关), 且整批共用一个很短的预算 —— 超时就整批不带图, 页面照常可用.
"""

from __future__ import annotations

import time

import structlog

from ...agent import AgentService
from ...agent.executor import QueryExecutor
from ...db.models import SavedQuery, SavedQueryEntity
from ...db.repository import Repository
from ..models.saved_queries import SavedQueryPreview

__all__ = ["PREVIEW_BUDGET_SECONDS", "PREVIEW_SQL_TIMEOUT_MS", "build_previews"]

logger = structlog.get_logger()

PREVIEW_SQL_TIMEOUT_MS = 2_000
"""单条预设最多跑这么久; 预设本身是 id 查询, 正常都在毫秒级."""

PREVIEW_BUDGET_SECONDS = 3.0
"""整批预算: 超过就不再给剩下的预设配图, 免得一条坏预设拖慢整个列表."""


async def build_previews(
    repo: Repository,
    service: AgentService,
    queries: list[SavedQuery],
) -> dict[int, SavedQueryPreview]:
    """按预设 id 给出缩略图; 算不出来的不放进结果, 由前端显示占位图."""
    executor = service.executor
    deadline = time.monotonic() + PREVIEW_BUDGET_SECONDS
    previews: dict[int, SavedQueryPreview] = {}
    for query in queries:
        if query.id is None or query.entity is SavedQueryEntity.DATA:
            continue
        if time.monotonic() > deadline:
            logger.info("saved query preview budget exhausted", done=len(previews))
            break
        preview = await _preview_of(repo, executor, query)
        if preview is not None:
            previews[query.id] = preview
    return previews


async def _preview_of(repo: Repository, executor: QueryExecutor, query: SavedQuery) -> SavedQueryPreview | None:
    try:
        cached = await executor.ensure_cached(query, timeout_ms=PREVIEW_SQL_TIMEOUT_MS)
    except Exception as exc:  # 预设 SQL 坏掉不该让列表 500
        logger.info("saved query preview failed", query_id=query.id, error=str(exc))
        return None
    entity_id = _first_id(cached.rows)
    if entity_id is None:
        return None
    if query.entity is SavedQueryEntity.METADATA:
        metadata = await repo.get_metadata(entity_id)
        if metadata is None:
            return None
        return SavedQueryPreview(
            entity_id=entity_id,
            title=metadata.number or metadata.title or str(entity_id),
            image_url=_first_url(metadata.poster_url, metadata.poster_urls, metadata.thumb_url, metadata.thumb_urls),
        )
    actor = await repo.get_actor(entity_id)
    if actor is None:
        return None
    return SavedQueryPreview(entity_id=actor.id or entity_id, title=actor.name, image_url=_first_url(actor.image_urls))


def _first_id(rows: list[list[object]]) -> int | None:
    if not rows or not rows[0]:
        return None
    value = rows[0][0]
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _first_url(*candidates: object) -> str | None:
    """按顺序取第一个非空 URL: 单值字段在前, 列表字段在后."""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
        if isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, str) and item.strip():
                    return item
    return None
