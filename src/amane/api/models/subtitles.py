from pydantic import BaseModel


class SubtitleStatusResponse(BaseModel):
    enabled: bool
    missing: int
    """库内成片没有外挂字幕的份数 (不含分卷)."""
    multi_disc: int = 0
    """分卷里没有外挂字幕的份数: 整片字幕与拆开的文件对不齐, 不算缺口."""
    queued: int
