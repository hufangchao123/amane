from datetime import datetime

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
