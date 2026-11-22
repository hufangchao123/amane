"""演员头像校验: 逐个下载首图, 按「能不能当图片用」分类.

头像地址来自刮削结果, 常年不变而站点会换路径, 于是库里留着大量 404 的 URL. 校验只读:
下载内容写进临时文件、认出图片格式即删, 不改库 (清空坏链接由任务按开关执行).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from ..net.egress import EgressPolicy
from ..net.errors import EgressRejected, RequestError

#: 单个头像的大小上限; 超过即判坏, 免得一个巨型文件占满磁盘与时间.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

#: 单次校验的超时 (秒), 与面板的 12 秒对齐.
VERIFY_TIMEOUT = 12.0

#: 结果里保留的样本条数.
SAMPLE_LIMIT = 20

#: 认得出的图片头. WebP 的 RIFF 头还需在第 8 字节看到 WEBP, 这里只按首 4 字节粗判.
_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"RIFF", b"BM")

Verdict = Literal["ok", "broken", "unknown"]


class AvatarHttp(Protocol):
    """只用到下载: 头像校验不解析页面, 也不重试多次."""

    async def download(
        self,
        url: str,
        dest: Path,
        *,
        use_proxy: bool = True,
        egress: EgressPolicy | None = None,
        max_bytes: int | None = None,
    ) -> bool: ...


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
    web: AvatarHttp, url: str, dest: Path, *, egress: EgressPolicy | None = None
) -> tuple[Verdict, str]:
    """下载到 ``dest`` 并判定. 内网地址不算坏: 那是本地部署的媒体服务器, 不是失效链接.

    结论分三种: ``ok`` 拿到图片字节; ``broken`` 下载失败或内容不是图片; ``unknown`` 有意不校验
    (地址指向内网、协议不支持). 调用方据此决定是否清空链接 —— 只有 ``broken`` 可以清.
    """
    policy = egress if egress is not None else EgressPolicy()
    try:
        await policy.ensure_allowed(url)
    except EgressRejected as error:
        return "unknown", f"未校验: {error.detail or error.message}"
    except RequestError as error:
        # 域名解析不出来说明这个链接已经废了, 按坏链接处理
        return "broken", error.message
    downloaded = await web.download(str(url), dest, egress=policy, max_bytes=MAX_IMAGE_BYTES)
    try:
        if not downloaded:
            return "broken", "下载失败"
        if not dest.exists():
            return "broken", "没有产物"
        head = dest.read_bytes()[:16]
    finally:
        dest.unlink(missing_ok=True)
    if not looks_like_image(head):
        return "broken", "内容不是图片"
    return "ok", ""
