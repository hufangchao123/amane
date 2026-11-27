from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class SubtitleStatusResponse(BaseModel):
    enabled: bool
    missing: int
    """库内成片没有外挂字幕的份数 (不含分卷)."""
    multi_disc: int = 0
    """分卷里没有外挂字幕的份数: 整片字幕与拆开的文件对不齐, 不算缺口."""
    pointer: int = 0
    """指针文件 (``.strm``) 没有外挂字幕的份数: 本地没有音轨, 转写跳过, 只能手动上传."""
    queued: int


class SubtitleGenerationTestResponse(BaseModel):
    """转写端点的连通性探测结果: 只报能不能用, 不改任何配置."""

    ok: bool
    model: str
    latency_ms: int
    detail: str = ""


class SubtitleUploadResponse(BaseModel):
    """手动上传的字幕落盘位置."""

    path: str
    """落盘后的绝对路径: 与视频同目录, 名字为 ``<视频名>.<语言>.<扩展名>``."""
    bytes_written: int
    language: str


class SubtitleFileEntry(BaseModel):
    """视频旁边的一份字幕文件."""

    name: str
    path: str
    extension: str
    size: int
    language: str | None = None
    """从文件名里认出 ``<视频名>.<语言><扩展名>`` 的语言段, 认不出为空."""
    modified_at: datetime | None = None


class SubtitleFilesResponse(BaseModel):
    """某个视频旁边现有的字幕清单."""

    media_file_id: int
    video_path: str
    extensions: list[str]
    """媒体库认可的扩展名; 上传只接受这些."""
    files: list[SubtitleFileEntry]


class SubtitleGapItem(BaseModel):
    """一份缺字幕的文件: 为什么缺、试过几次、能不能重试."""

    media_file_id: int
    number: str | None = None
    """番号; 未刮削或文件名解析不出番号时为空."""
    path: str
    kind: Literal["missing", "multi_disc", "pointer"]
    """``missing`` 是本地成片, ``multi_disc`` 是分卷, ``pointer`` 是网盘直链 (``.strm``)."""
    reason: Literal["multi_disc", "pointer", "fetch_failed", "fetch_empty", "not_attempted"]
    detail: str
    """给人看的一句话原因."""
    attempts: int = 0
    """最近 ``window_days`` 天内的尝试次数."""
    failed: int = 0
    last_error: str | None = None
    last_attempt_at: datetime | None = None
    retryable: bool = True
    """分卷不可重试: 抓整片字幕也对不齐."""


class SubtitleGapCounts(BaseModel):
    """按原因分组的计数, 界面用它画分组标题."""

    missing: int = 0
    multi_disc: int = 0
    pointer: int = 0
    failed: int = 0
    fetch_empty: int = 0
    not_attempted: int = 0


class SubtitleGapsResponse(BaseModel):
    items: list[SubtitleGapItem]
    total: int
    """缺口总数 (不受 ``limit`` 影响)."""
    counts: SubtitleGapCounts
    window_days: int
