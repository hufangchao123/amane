"""NFO 修补的现状: 库内有多少演员带头像, 有哪些媒体库可供选择."""

from pydantic import BaseModel


class NfoLibraryOption(BaseModel):
    """可作为修补目标的媒体库."""

    library_id: int
    name: str
    path: str


class NfoStatusResponse(BaseModel):
    """演员头像的库存情况; 待补条数由修补任务预演后才知道, 这里只报可用素材."""

    actors: int = 0
    """库内演员总数."""
    actors_with_image: int = 0
    """有头像地址的演员数; 只有这部分能补进 NFO."""
    libraries: list[NfoLibraryOption] = []
