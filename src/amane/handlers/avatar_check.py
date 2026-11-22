"""演员头像校验任务: 按 id 顺序抽查若干演员的首图, 可选清掉坏链接."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..avatars import AvatarFinding, verify_location
from ..avatars.audit import SAMPLE_LIMIT
from ..db.repo_types import WriteMode
from ..net.egress import EgressPolicy
from .models import AvatarCheckPayload, AvatarCheckResult
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..avatars.audit import AvatarHttp
    from ..config import HotSettings
    from ..db.repository import Repository

logger = structlog.get_logger(__name__)

#: 一页读多少演员; 与库列表的页大小无关, 这里只为少发几次查询.
ACTOR_PAGE = 200


class AvatarCheckHandler(TaskHandler[AvatarCheckPayload, AvatarCheckResult]):
    """校验不写库, 除非 ``clear_broken``: 那时把坏链接从 ``image_urls`` 里摘掉.

    只查首图: 一个演员挂多个地址时后面的地址不单独校验 —— 首图可用就说明来源还活着.
    """

    def __init__(
        self,
        repo: Repository,
        hot: HotSettings,
        web: AvatarHttp,
        *,
        state_dir: Path | None = None,
        egress: EgressPolicy | None = None,
    ) -> None:
        super().__init__(payload_t=AvatarCheckPayload, result_t=AvatarCheckResult)
        self._repo = repo
        self._hot = hot
        self._web = web
        self._state_dir = state_dir if state_dir is not None else Path("./data")
        self._egress = egress if egress is not None else EgressPolicy()

    async def handle(self, payload: AvatarCheckPayload) -> TaskResult[AvatarCheckResult]:
        scratch = self._state_dir / "avatar-check"
        checked = ok = broken = unknown = cleared = 0
        samples: list[AvatarFinding] = []
        offset = 0
        while checked < payload.limit:
            page = await self._repo.list_actors(offset=offset, limit=ACTOR_PAGE)
            if not page:
                break
            offset += len(page)
            for actor in page:
                if checked >= payload.limit:
                    break
                if actor.id is None or not actor.image_urls:
                    continue
                checked += 1
                url = actor.image_urls[0]
                verdict, detail = await verify_location(
                    self._web, url, scratch / f"{actor.id}.bin", egress=self._egress
                )
                if verdict == "ok":
                    ok += 1
                    continue
                if len(samples) < SAMPLE_LIMIT:
                    samples.append(
                        AvatarFinding(actor_id=actor.id, name=actor.name, url=url, verdict=verdict, detail=detail)
                    )
                if verdict == "unknown":
                    unknown += 1
                    continue
                broken += 1
                if payload.clear_broken and await self._clear(actor.id, url):
                    cleared += 1
        result = AvatarCheckResult(
            checked=checked,
            ok=ok,
            broken=broken,
            unknown=unknown,
            cleared=cleared,
            samples=[f"#{item.actor_id} {item.name}: {item.detail or item.verdict}" for item in samples],
        )
        logger.info("avatar check done", checked=checked, broken=broken, unknown=unknown, cleared=cleared)
        return TaskResult(success=True, result=result)

    async def _clear(self, actor_id: int, url: str) -> bool:
        """摘掉这个坏地址; 其余地址保留."""
        actor = await self._repo.get_actor(actor_id)
        if actor is None:
            return False
        await self._repo.update_actor(
            actor_id, image_urls=[item for item in actor.image_urls if item != url], mode=WriteMode.MANUAL
        )
        return True
