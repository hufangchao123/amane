"""115 中转的只读查询; 搬移本体是 ``TRANSFER_115`` 任务, 这里不开写端点.

"立即中转"走 ``POST /tasks``: 搬移要几十秒到几分钟, 路由里跑长循环会占住事件循环, 也没法取消.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response

from ...db.models import TransferItemState
from ...enums import TransferLoginClient
from ...transfer import TransferService, TransferSummary
from ...transfer.login import QrLoginError, TransferQrLogin
from ...utils.model import to_resp
from ..deps import ConfigDep, RepoDep, RuntimeDep
from ..models.transfer import (
    TransferItemListResponse,
    TransferItemResponse,
    TransferQrPollResponse,
    TransferQrStartRequest,
    TransferQrTicketResponse,
    TransferStatusResponse,
)

router = APIRouter(prefix="/transfer", tags=["transfer"])


@router.get("/status")
async def transfer_status(config: ConfigDep, repo: RepoDep) -> TransferStatusResponse:
    service = TransferService(config.hot.transfer)
    summary = TransferSummary.from_counts(await repo.transfer_state_counts())
    reason = None if service.enabled else service.unavailable_reason()
    return TransferStatusResponse(enabled=service.enabled, reason=reason, **summary.model_dump())


def _qr_login() -> TransferQrLogin:
    """单独成函数, 测试替换这一层就能拦下真实网络调用."""
    return TransferQrLogin()


@router.post("/qrcode", status_code=201)
async def start_transfer_qrcode(req: TransferQrStartRequest) -> TransferQrTicketResponse:
    """换一张二维码. 扫码要人来做, 因此这里只发码, 结果由前端轮询 ``/qrcode/status``."""
    try:
        ticket = await _qr_login().create(req.client)
    except QrLoginError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return TransferQrTicketResponse(
        enabled=True,
        uid=ticket.uid,
        time=ticket.time,
        sign=ticket.sign,
        client=req.client,
        qrcode_url=ticket.qrcode_url,
        expires_in=ticket.expires_in,
    )


@router.get("/qrcode/status")
async def poll_transfer_qrcode(
    config: ConfigDep,
    runtime: RuntimeDep,
    client: TransferLoginClient,
    uid: Annotated[str, Query(min_length=1)],
    time: Annotated[str, Query(min_length=1)],
    sign: Annotated[str, Query(min_length=1)],
) -> TransferQrPollResponse:
    """轮询一次; 确认后把 Cookie 写进配置并立刻重建运行时服务."""
    try:
        poll = await _qr_login().poll(client=client, uid=uid, time=time, sign=sign)
    except QrLoginError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if poll.status != "confirmed" or poll.cookies is None:
        return TransferQrPollResponse(status=poll.status)
    config.update({"transfer": {"cookies": poll.cookies}})
    await runtime.apply_rebuild()
    return TransferQrPollResponse(status="confirmed", saved=True)


@router.get("/qrcode/image")
async def transfer_qrcode_image(
    client: TransferLoginClient,
    uid: Annotated[str, Query(min_length=1)],
) -> Response:
    """把二维码图片转给浏览器. 图片本身不含凭据, 但仍不让前端直连 115 域名."""
    try:
        image = await _qr_login().image(client=client, uid=uid)
    except QrLoginError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return Response(content=image, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/items")
async def list_transfer_items(
    repo: RepoDep,
    state: Annotated[TransferItemState | None, Query(description="仅该状态的条目")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200, description="每页条数")] = 50,
) -> TransferItemListResponse:
    rows, total = await repo.list_transfer_items(state=state, offset=offset, limit=limit)
    return TransferItemListResponse(items=[to_resp(TransferItemResponse, row) for row in rows], total=total)
