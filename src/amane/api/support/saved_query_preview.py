"""预设卡片的配图: 跑一次预设, 拿第一条结果的图.

预设卡原先只有名字与描述, 一串「丝袜」「4K」的名字放在一起很难分辨; 取第一条结果的海报/头像
当缩略图, 一眼能看出这条预设大概选中了什么.

列表接口给每条预设补第一行的图; 预设详情页的结果表再按行补一遍 (``previews_for_rows``),
于是「预设里每个 id」都能看到对应的片子. 代价是要真的执行一遍 SQL, 因此两条约束: 只给
``metadata`` / ``actor`` 两类做 (``data`` 预设按列名猜实体, 猜不出就不给图), 且整批共用一个很短的
预算 —— 超时就整批不带图, 页面照常可用.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import structlog

from ...agent import AgentService
from ...agent.executor import QueryExecutor
from ...db.models import SavedQuery, SavedQueryEntity
from ...db.repository import Repository
from ..models.saved_queries import SavedQueryPreview

__all__ = ["PREVIEW_BUDGET_SECONDS", "PREVIEW_SQL_TIMEOUT_MS", "build_previews", "previews_for_rows"]

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
        if query.id is None:
            continue
        if time.monotonic() > deadline:
            logger.info("saved query preview budget exhausted", done=len(previews))
            break
        preview = await _preview_of(repo, executor, query)
        if preview is not None:
            previews[query.id] = preview
    return previews


async def _preview_of(repo: Repository, executor: QueryExecutor, query: SavedQuery) -> SavedQueryPreview | None:
    """列表卡片的图 = 结果第一行的图; 与详情页走同一套列名判定, 口径不会两样."""
    try:
        cached = await executor.ensure_cached(query, timeout_ms=PREVIEW_SQL_TIMEOUT_MS)
    except Exception as exc:  # 预设 SQL 坏掉不该让列表 500
        logger.info("saved query preview failed", query_id=query.id, error=str(exc))
        return None
    if not cached.rows:
        return None
    previews = await previews_for_rows(repo, query, cached.columns, cached.rows[:1])
    return previews[0] if previews else None


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


_ID_COLUMNS = ("id", "metadata_id", "actor_id")
"""结果表里可能装实体 id 的列名, 按优先级."""

_PREVIEW_ROW_LIMIT = 200
"""一次最多补多少行的图: 页面一页 100 行, 留一倍余量就好, 再多没意义."""


def _id_column(columns: Sequence[str]) -> int | None:
    lowered = [str(c).strip().lower() for c in columns]
    for name in _ID_COLUMNS:
        if name in lowered:
            return lowered.index(name)
    return None


def _entity_of(query: SavedQuery, columns: Sequence[str]) -> SavedQueryEntity | None:
    """预设有实体类型就用它; ``data`` 预设按列名猜, 猜不出就不给图."""
    if query.entity in (SavedQueryEntity.METADATA, SavedQueryEntity.ACTOR):
        return query.entity
    lowered = {str(c).strip().lower() for c in columns}
    if "number" in lowered:
        return SavedQueryEntity.METADATA
    if "name" in lowered:
        return SavedQueryEntity.ACTOR
    return None


def entity_of(query: SavedQuery, columns: Sequence[str]) -> SavedQueryEntity | None:
    """结果行装的是哪类实体; 判断不出返回 None (调用方据此不给图)."""
    return _entity_of(query, columns)


async def previews_for_rows(
    repo: Repository,
    query: SavedQuery,
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
) -> list[SavedQueryPreview | None]:
    """按结果行的实体 id 取图, 与 ``rows`` 等长; 取不到的位置是 ``None``.

    预设的结果表是任意 SQL 的产物, 只能靠列名判断 id 与实体; 判断不出来就整列不给图,
    宁可少一张图, 也不要把演员头像贴到番号上。
    """
    entity = entity_of(query, columns)
    if entity is None or not rows:
        return [None] * len(rows)
    column = _id_column(columns)
    if column is None:
        return [None] * len(rows)

    ids: list[int] = []
    for row in rows[:_PREVIEW_ROW_LIMIT]:
        value = row[column] if column < len(row) else None
        ids.append(value if isinstance(value, int) and not isinstance(value, bool) else 0)

    wanted = [i for i in ids if i > 0]
    if not wanted:
        return [None] * len(rows)

    by_id: dict[int, SavedQueryPreview] = {}
    if entity is SavedQueryEntity.METADATA:
        for item in await repo.get_metadata_by_ids(wanted):
            if item.id is None:
                continue
            by_id[item.id] = SavedQueryPreview(
                entity_id=item.id,
                title=(item.number or item.title or str(item.id)).strip(),
                image_url=_first_url(item.poster_url, *item.poster_urls, item.thumb_url, *item.thumb_urls),
            )
    else:
        for actor in await repo.get_actors(wanted):
            if actor.id is None:
                continue
            by_id[actor.id] = SavedQueryPreview(
                entity_id=actor.id,
                title=(actor.name or str(actor.id)).strip(),
                image_url=_first_url(*actor.image_urls),
            )

    return [by_id.get(i) for i in ids] + [None] * max(0, len(rows) - _PREVIEW_ROW_LIMIT)
