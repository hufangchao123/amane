"""NFO 修补的只读现状.

修补动作走 ``NFO_PATCH`` 任务 (见 ``docs/dev/ops.md``): 路由只报库内演员头像数量与可选媒体库.
真正要改多少文件必须先跑一次预演, 因为那要遍历磁盘, 不适合放在请求里.
"""

from fastapi import APIRouter

from ..deps import RepoDep
from ..models.nfo import NfoLibraryOption, NfoStatusResponse

router = APIRouter(prefix="/nfo", tags=["nfo"])

ACTOR_PAGE = 500


@router.get("/status")
async def get_nfo_status(repo: RepoDep) -> NfoStatusResponse:
    """演员总数与其中带头像的数量; 分页遍历, 单页取满才继续."""
    actors = 0
    with_image = 0
    offset = 0
    while True:
        page = await repo.list_actors(offset=offset, limit=ACTOR_PAGE)
        for actor in page:
            actors += 1
            if actor.image_urls:
                with_image += 1
        if len(page) < ACTOR_PAGE:
            break
        offset += ACTOR_PAGE
    libraries = [
        NfoLibraryOption(library_id=library.id, name=library.name, path=library.path)
        for library in await repo.list_libraries()
        if library.id is not None
    ]
    return NfoStatusResponse(actors=actors, actors_with_image=with_image, libraries=libraries)
