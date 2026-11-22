"""NFO 修补任务: 把演员头像补进已有的 NFO, 或删掉导演行.

修的是磁盘上的文件, 因此不建索引、不改数据库; 真跑时按媒体库取整理锁, 与整理/删除/洗版互斥.
演员头像来自库内 ``Actor.image_urls`` 的第一条, 与媒体服务器上的演员图表同源.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..library.nfo.patch import NfoPatchOutcome, fill_actor_thumbs, strip_directors
from .models import NfoPatchPayload, NfoPatchResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..db.models import Library
    from ..db.repository import Repository
    from ._common import LibraryTaskLocks

logger = structlog.get_logger()

ACTOR_PAGE = 500


class NfoPatchHandler(TaskHandler[NfoPatchPayload, NfoPatchResult]):
    """两类修补共用一套流程: 选库 → 逐库扫 NFO → 按 ``kind`` 改写."""

    def __init__(self, repo: Repository, library_locks: LibraryTaskLocks) -> None:
        super().__init__(payload_t=NfoPatchPayload, result_t=NfoPatchResult)
        self._repo = repo
        self._library_locks = library_locks

    async def handle(self, payload: NfoPatchPayload) -> TaskResult[NfoPatchResult]:
        libraries = await self._select_libraries(payload)
        if libraries is None:
            return TaskResult(success=False, error=f"媒体库 {payload.library_id} 不存在")
        images = await self._actor_images() if payload.kind == "thumbs" else {}
        total = NfoPatchOutcome()
        missing_paths = 0
        for library in libraries:
            if library.id is None or not Path(library.path).is_dir():
                missing_paths += 1
                continue
            total.merge(await self._patch_one(library, payload, images))
        result = NfoPatchResult(
            kind=payload.kind,
            dry_run=payload.dry_run,
            libraries=len(libraries),
            files=total.files,
            changed=total.changed,
            filled=total.filled,
            missing_image=total.missing_image,
            removed=total.removed,
            failed=total.failed + missing_paths,
            samples=total.samples,
        )
        logger.info(
            "nfo patch finished",
            kind=payload.kind,
            dry_run=payload.dry_run,
            files=total.files,
            changed=total.changed,
            failed=result.failed,
        )
        return TaskResult(success=result.failed == 0, result=result)

    async def _select_libraries(self, payload: NfoPatchPayload) -> list[Library] | None:
        if payload.library_id is None:
            return await self._repo.list_libraries()
        library = await self._repo.get_library(payload.library_id)
        return None if library is None else [library]

    async def _patch_one(self, library: Library, payload: NfoPatchPayload, images: dict[str, str]) -> NfoPatchOutcome:
        if payload.dry_run or library.id is None:
            if payload.kind == "thumbs":
                return await fill_actor_thumbs([library.path], images, dry_run=True)
            return await strip_directors([library.path], dry_run=True)
        lock = await self._library_locks.get(library.id)
        async with lock:
            if payload.kind == "thumbs":
                return await fill_actor_thumbs([library.path], images, dry_run=False)
            return await strip_directors([library.path], dry_run=False)

    async def _actor_images(self) -> dict[str, str]:
        """演员展示名到第一张头像地址; 同名只留先出现的那个."""
        images: dict[str, str] = {}
        offset = 0
        while True:
            actors = await self._repo.list_actors(offset=offset, limit=ACTOR_PAGE)
            for actor in actors:
                urls = actor.image_urls or []
                if actor.name and actor.name not in images and urls and urls[0]:
                    images[actor.name] = urls[0]
            if len(actors) < ACTOR_PAGE:
                return images
            offset += ACTOR_PAGE
