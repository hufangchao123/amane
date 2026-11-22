"""历史回收站的现状: 每个库一行, 供页面在清理前先看规模."""

from pydantic import BaseModel


class TrashLibraryStatus(BaseModel):
    """一个库的 ``.amane_trash`` 现状; ``oldest_days`` 为空表示目录里没有文件."""

    library_id: int
    name: str
    exists: bool
    files: int = 0
    bytes: int = 0
    oldest_days: int | None = None


class TrashStatusResponse(BaseModel):
    """全部库的回收站现状; 没有库时 ``libraries`` 为空."""

    libraries: list[TrashLibraryStatus]
    total_files: int = 0
    total_bytes: int = 0
