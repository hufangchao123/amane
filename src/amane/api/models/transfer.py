from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from ...db.models import TransferItemState
from ...enums import TransferLoginClient


class TransferStatusResponse(BaseModel):
    enabled: bool
    reason: str | None
    """未启用时缺什么; 已启用时为 ``None``, 页面据此给出可操作的那一句."""
    pending: int
    moved: int
    skipped: int
    failed: int


class TransferItemResponse(BaseModel):
    """一条逐文件状态; ``error`` 在跳过时承载原因, 在失败时承载错误."""

    id: int
    fid: str
    file_name: str
    state: TransferItemState
    attempts: int
    error: str | None
    moved_at: datetime | None
    updated_at: datetime


class TransferDirEntry(BaseModel):
    """网盘目录里的一层文件夹: 只有名字与 ID, 用来填源目录与目标目录."""

    id: str
    name: str


class TransferDirListResponse(BaseModel):
    """一层目录的子文件夹. ``cid`` 是这一层的目录 ID, 根目录为 ``0``."""

    enabled: bool
    cid: str
    items: list[TransferDirEntry]
    reason: str | None = None


class TransferQrStartRequest(BaseModel):
    """要按哪个客户端换二维码; 不同客户端拿到的 Cookie 只能用于对应接口."""

    client: TransferLoginClient = TransferLoginClient.WEB


class TransferQrTicketResponse(BaseModel):
    """一张待扫的二维码; 前端拿 ``uid`` / ``time`` / ``sign`` 轮询, 拿 ``qrcode_url`` 显示."""

    enabled: bool
    """中转未启用时也允许扫码登录 (Cookie 是启用条件的另一半), 这里恒为真, 留给前端判断是否需要提示."""
    uid: str
    time: str
    sign: str
    client: TransferLoginClient
    qrcode_url: str
    expires_in: int


class TransferQrPollResponse(BaseModel):
    status: Literal["waiting", "scanned", "confirmed", "expired"]
    saved: bool = False
    """确认后 Cookie 已写进配置; 前端据此刷新状态与提示."""


class TransferItemListResponse(BaseModel):
    items: list[TransferItemResponse]
    total: int
    """不受本页 ``limit`` 截断."""
