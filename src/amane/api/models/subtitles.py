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
