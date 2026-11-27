"""缺字幕清单: 哪个番号缺、为什么缺、试过几次、能不能重试.

统计只回答「还缺多少份」, 用户点开页面想知道的是「这几份为什么没有」—— 站点都没找到、
网盘直链没有本地视频所以转写跳过、还是上一轮下载直接报错。原因与失败次数都从任务表里取,
不额外落一份状态: 任务本身就是唯一的执行历史.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from ..db.models import MediaFile, MediaFileStatus, TaskStatus, TaskType
from ..db.repository import Repository
from ..parsing import parse_file_info
from .targets import GapKind, classify_gap

__all__ = ["ATTEMPT_WINDOW_DAYS", "GapReason", "GapRow", "collect_gaps"]

ATTEMPT_WINDOW_DAYS = 30
"""失败次数只数最近这么多天: 更早的尝试对「现在为什么还缺」没有参考价值."""

_ERROR_LIMIT = 300


class GapReason(StrEnum):
    """缺字幕的原因, 界面按它分组显示."""

    MULTI_DISC = "multi_disc"
    """分卷: 整片字幕与拆开的文件对不齐, 不作为补全目标."""
    POINTER = "pointer"
    """网盘直链 (``.strm``): 本地没有视频本体, 转写无法进行."""
    FETCH_FAILED = "fetch_failed"
    """上一轮下载报错."""
    FETCH_EMPTY = "fetch_empty"
    """跑过但站点都没有这份字幕."""
    NOT_ATTEMPTED = "not_attempted"
    """还没试过."""


@dataclass(frozen=True, slots=True)
class _Attempt:
    total: int = 0
    failed: int = 0
    error: str | None = None
    at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GapRow:
    """一份缺字幕的文件与它的原因."""

    media: MediaFile
    label: str
    """界面上的「番号」: 索引行的 number 多为空, 退回路径解析与文件名."""
    kind: GapKind
    reason: GapReason
    detail: str
    attempts: int
    failed: int
    last_error: str | None
    last_attempt_at: datetime | None
    retryable: bool


async def collect_gaps(repo: Repository, *, library_id: int | None = None) -> list[GapRow]:
    """列出缺字幕的文件与原因, 失败过的排在前面.

    ``library_id`` 为空时看全部库 —— 页面默认展示整个实例的缺口, 用户按库筛是二次操作.
    """
    gaps = await repo.list_media_files(
        status=[MediaFileStatus.SCRAPED],
        library_id=library_id,
        has_subtitle=False,
        limit=None,
    )
    attempts = await _attempts_by_media(repo)
    rows = [_row(media, attempts.get(media.id)) for media in gaps]
    rows.sort(key=_sort_key)
    return rows


async def _attempts_by_media(repo: Repository) -> dict[int, _Attempt]:
    since = datetime.now(UTC) - timedelta(days=ATTEMPT_WINDOW_DAYS)
    rows = await repo.list_subtitle_attempts(task_type=TaskType.SUBTITLE_FETCH, since=since)
    stats: dict[int, _Attempt] = {}
    for media_id, status, error, created_at in rows:
        previous = stats.get(media_id)
        if previous is None:
            # 查询按 id 倒序: 第一次遇到的这条就是这个文件最近一次尝试, 错误与时间取它.
            stats[media_id] = _Attempt(1, 1 if status is TaskStatus.FAILED else 0, _trim(error), created_at)
        else:
            stats[media_id] = _Attempt(
                previous.total + 1,
                previous.failed + (1 if status is TaskStatus.FAILED else 0),
                previous.error,
                previous.at,
            )
    return stats


def _label_of(media: MediaFile) -> str:
    """番号优先取索引行, 空则按路径解析, 再退回文件名.

    索引行的 ``number`` 只在新入库路径写; 早期批量导入的行是空的 (库里多数如此),
    直接读它会让界面整列显示「—」。
    """
    stored = (media.number or "").strip()
    if stored:
        return stored
    path = str(media.path or "")
    try:
        parsed = parse_file_info(path).number
    except Exception:  # 解析只用于展示, 失败不能影响列表
        parsed = None
    return (parsed or Path(path).stem or "").strip()


def _row(media: MediaFile, attempt: _Attempt | None) -> GapRow:
    kind = classify_gap(media.path)
    attempts = attempt.total if attempt is not None else 0
    failed = attempt.failed if attempt is not None else 0
    error = attempt.error if attempt is not None else None
    if kind is GapKind.MULTI_DISC:
        reason, detail = GapReason.MULTI_DISC, "分卷: 整片字幕与拆开的文件对不齐, 不作为补全目标"
    elif kind is GapKind.POINTER:
        reason, detail = GapReason.POINTER, "网盘直链 (.strm) 没有本地视频本体, 转写跳过; 站点没收录时只能手动上传"
    elif failed:
        reason, detail = GapReason.FETCH_FAILED, f"下载失败 {failed} 次: {error or '原因未记录'}"
    elif attempts:
        reason, detail = GapReason.FETCH_EMPTY, f"试过 {attempts} 次, 站点都没有这份字幕"
    else:
        reason, detail = GapReason.NOT_ATTEMPTED, "还没试过"
    return GapRow(
        media=media,
        label=_label_of(media),
        kind=kind,
        reason=reason,
        detail=detail,
        attempts=attempts,
        failed=failed,
        last_error=error,
        last_attempt_at=attempt.at if attempt is not None else None,
        retryable=kind is not GapKind.MULTI_DISC,
    )


def _sort_key(row: GapRow) -> tuple[int, int, str]:
    """失败过的排前面, 其次按尝试次数, 最后按番号; 分卷沉到最后."""
    bucket = 0 if row.reason is GapReason.FETCH_FAILED else (2 if row.kind is GapKind.MULTI_DISC else 1)
    return bucket, -row.attempts, row.media.number or row.media.path


def _trim(error: str | None) -> str | None:
    if not error:
        return None
    text = " ".join(error.split())
    return text if len(text) <= _ERROR_LIMIT else f"{text[: _ERROR_LIMIT - 1]}…"
