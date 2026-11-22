"""历史回收站 (``.amane_trash``) 的只读现状.

清理动作走 ``TRASH_PURGE`` 任务 (见 ``docs/dev/ops.md``): 路由只报规模, 不动文件.
遍历发生在回收目录内, 与 ``GET /libraries/{id}/cleanup/trash`` 同一约束: 进线程池.
"""

from pathlib import Path

from fastapi import APIRouter

from ...library.cleanup import legacy
from ...library.rules import TRASH_DIRNAME
from ..deps import RepoDep
from ..models.trash import TrashLibraryStatus, TrashStatusResponse

router = APIRouter(prefix="/trash", tags=["trash"])


@router.get("/status")
async def get_trash_status(repo: RepoDep) -> TrashStatusResponse:
    """每个库的回收站规模; 修改时间最早的文件决定 ``oldest_days``."""
    libraries = await repo.list_libraries()
    items: list[TrashLibraryStatus] = []
    for library in libraries:
        if library.id is None:
            continue
        stats = await legacy.scan(Path(library.path) / TRASH_DIRNAME)
        items.append(
            TrashLibraryStatus(
                library_id=library.id,
                name=library.name,
                exists=stats.files > 0,
                files=stats.files,
                bytes=stats.bytes,
                oldest_days=stats.oldest_days,
            )
        )
    return TrashStatusResponse(
        libraries=items,
        total_files=sum(item.files for item in items),
        total_bytes=sum(item.bytes for item in items),
    )
