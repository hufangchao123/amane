"""演员头像校验: 逐个下载首图, 按「能不能当图片用」分类.

头像地址来自刮削结果, 常年不变而站点会换路径, 于是库里留着大量 404 的 URL. 校验只读:
下载内容写进临时文件、认出图片格式即删, 不改库 (清空坏链接由任务按开关执行).

只有两种情形判 ``broken``: 拿到的字节不是图片, 或服务端明确回 404 / 410. 超时、连不上、
域名解析失败都归 ``unknown`` —— 站点抖动与链接失效在客户端看起来一样, 而清空链接是不可逆动作.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from ..net.egress import EgressPolicy
from ..net.errors import EgressRejected, RequestError

if TYPE_CHECKING:
    from ..net.http import WebClient

#: 单个头像的大小上限; 超过即判坏, 免得一个巨型文件占满磁盘与时间.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: 单次校验的超时 (秒), 与面板的 12 秒对齐.
VERIFY_TIMEOUT = 12.0

#: 结果里保留的样本条数.
SAMPLE_LIMIT = 20

#: 认得出的图片头. WebP 的 RIFF 头还需在第 8 字节看到 WEBP, 这里只按首 4 字节粗判.
_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"RIFF", b"BM")

#: 服务端明确声明资源不在了; 其余 4xx 与 5xx 可能是鉴权、限流或临时故障, 不据此清链接.
_GONE_STATUSES = frozenset({404, 410})

Verdict = Literal["ok", "broken", "unknown"]


class AvatarSource(Protocol):
    """头像来源: 下载落盘, 以及下载失败后问一次状态码.

    两者都要, 是因为客户端拿到的是「失败」而非原因: 只有再问一次状态码才能把 404 与超时分开.
    """

    async def download(self, url: str, dest: Path, *, max_bytes: int) -> bool: ...

    async def status(self, url: str) -> int | None: ...


@dataclass(frozen=True, slots=True)
class AvatarFinding:
    """一条校验结论."""

    actor_id: int
    name: str
    url: str
    verdict: Verdict
    detail: str


@dataclass(frozen=True, slots=True)
class AvatarCheckOutcome:
    """一次校验的汇总; ``samples`` 只留前 ``SAMPLE_LIMIT`` 条结论."""

    actors: int = 0
    checked: int = 0
    ok: int = 0
    broken: int = 0
    unknown: int = 0
    cleared: int = 0
    samples: list[AvatarFinding] = field(default_factory=list)


def looks_like_image(head: bytes) -> bool:
    """按文件头判断; 内容不是图片说明该地址返回的是错误页或占位图."""
    return any(head.startswith(magic) for magic in _IMAGE_MAGIC)


async def verify_location(
    source: AvatarSource, url: str, dest: Path, *, egress: EgressPolicy | None = None
) -> tuple[Verdict, str]:
    """下载到 ``dest`` 并判定. 内网地址不算坏: 那是本地部署的媒体服务器, 不是失效链接.

    结论分三种: ``ok`` 拿到图片字节; ``broken`` 内容不是图片或状态码明确是 404 / 410;
    ``unknown`` 没能判定 (地址指向内网、协议不支持、超时、连不上、解析失败). 调用方据此决定
    是否清空链接 —— 只有 ``broken`` 可以清, 清空是不可逆动作.
    """
    policy = egress if egress is not None else EgressPolicy()
    try:
        await policy.ensure_allowed(url)
    except EgressRejected as error:
        return "unknown", f"未校验: {error.detail or error.message}"
    except RequestError as error:
        return "unknown", f"未判定: {error.message}"
    try:
        downloaded = await source.download(str(url), dest, max_bytes=MAX_IMAGE_BYTES)
        if downloaded and dest.exists():
            if looks_like_image(dest.read_bytes()[:16]):
                return "ok", ""
            return "broken", "内容不是图片"
    finally:
        dest.unlink(missing_ok=True)
    status = await source.status(str(url))
    if status in _GONE_STATUSES:
        return "broken", f"HTTP {status}"
    return "unknown", "下载失败" if status is None else f"HTTP {status}"


class WebAvatarSource:
    """``WebClient`` 收成头像校验要的两个动作; 出站策略在构造时固定, 一次任务只判一套规则."""

    def __init__(self, web: WebClient, *, egress: EgressPolicy | None = None) -> None:
        self._web = web
        self._egress = egress if egress is not None else EgressPolicy()

    async def download(self, url: str, dest: Path, *, max_bytes: int) -> bool:
        return await self._web.download(url, dest, egress=self._egress, max_bytes=max_bytes)

    async def status(self, url: str) -> int | None:
        """问一次状态码. 服务器不认 HEAD 或网络故障时返回 ``None``, 由调用方归入未判定."""
        try:
            response = await self._web.request("HEAD", url, egress=self._egress, timeout=VERIFY_TIMEOUT, max_attempts=1)
        except RequestError:
            return None
        return response.status_code
