"""来源抓取的传输层: 出站一律走 ``net`` 包, 不另造 HTTP 客户端.

两个理由: 逐 host 限速与重试是 ``WebClient`` 的能力, 自建 urllib 调用会绕开它们把来源打挂;
外链下载地址来自第三方页面, 出站策略 (协议白名单 / 内网判定 / 逐跳复查) 也只在 ``net.egress`` 里.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Final, Protocol

from ..config.manager import NetworkConfig, SubtitleProvider
from ..net import RateLimiters, WebClient
from ..net.egress import EgressPolicy
from ..net.errors import EgressRejected
from .content import unwrap_subtitle
from .models import MAX_SUBTITLE_BYTES, SubtitleDownload, SubtitleSourceError

USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
"""两个来源都按浏览器应答排版; 默认 UA 会被 subtitlecat 当成爬虫直接回空列表."""

_ACCEPT_LANGUAGE: Final[str] = "zh-CN,zh;q=0.9,en;q=0.8"


class SubtitleHttp(Protocol):
    """抓取器需要的出站能力.

    用 Protocol 而不是直接吃 ``WebClient``: 单测用假实现替换, 不必起真连接, 也不会误连来源.
    """

    async def get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        use_proxy: bool = True,
    ) -> str: ...

    async def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        use_proxy: bool = True,
    ) -> object: ...

    async def download(
        self,
        url: str,
        dest: Path,
        *,
        use_proxy: bool = True,
        egress: EgressPolicy | None = None,
        max_bytes: int | None = None,
    ) -> bool: ...


def default_headers() -> dict[str, str]:
    """每次返回新 dict: 调用方会往里塞来源特有的头, 共享常量会被串改."""
    return {"User-Agent": USER_AGENT, "Accept-Language": _ACCEPT_LANGUAGE}


def staging_dir() -> Path:
    """建一个本次任务专属的临时目录; 目录名带前缀, 便于异常退出后人工识别残留."""
    return Path(tempfile.mkdtemp(prefix="amane-subtitle-"))


def build_web_client(network: NetworkConfig) -> WebClient:
    """按 ``network`` 配置造一个带限速的出站通道.

    只有 handler 在任务里自建并在结束时关闭; 请求作用域不造客户端 (会漏连接).
    """
    limiters = RateLimiters.from_config(
        network.rate_limits,
        {},
        {},
        default_rate=network.default_rate_limit,
    )
    return WebClient(
        proxy=network.proxy,
        timeout=network.timeout,
        max_retries=network.max_retries,
        max_clients=network.max_clients,
        limiters=limiters,
        chunked_threshold=network.chunked_threshold,
        chunk_size=network.chunk_size,
        download_concurrency=network.concurrency,
    )


async def ensure_public_url(url: str) -> None:
    """外链下载前的出站校验: 只允许 http(s), 禁私网与 localhost.

    来源页面里的下载链接是外部输入 (可被劫持或本来就指向内网), 不校验就等于给外部一个
    "让 amane 去读任意内网地址并落盘" 的入口. 这里只做一次性判定, 重定向由
    ``WebClient.request`` 的 ``egress`` 逐跳复查补上.
    """
    await EgressPolicy().ensure_allowed(url)


async def download_subtitle(
    web: SubtitleHttp,
    url: str,
    *,
    provider: SubtitleProvider,
    description: str,
    name_hint: str = "",
) -> SubtitleDownload:
    """校验 → 限长下载到临时文件 → 解包校验, 返回可落位的字幕正文.

    先落临时文件而不是整读进内存: ``download(max_bytes=...)`` 边写边计数, 来源回一个
    几百 MB 的假文件时能立刻停, 不会先把内存吃满.
    """
    try:
        await ensure_public_url(url)
    except EgressRejected as exc:
        raise SubtitleSourceError(f"下载地址被拒绝: {exc.detail}", transient=False) from exc

    directory = await asyncio.to_thread(staging_dir)
    try:
        target = directory / "payload"
        # egress 交给 WebClient: 给了它就连重定向也逐跳复查; 同时禁掉自动跟随之外的多余放行.
        ok = await web.download(url, target, egress=EgressPolicy(), max_bytes=MAX_SUBTITLE_BYTES)
        if not ok:
            raise SubtitleSourceError("字幕下载失败或超过大小上限", transient=True)
        payload = await asyncio.to_thread(target.read_bytes)
    finally:
        await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)

    if len(payload) > MAX_SUBTITLE_BYTES:
        raise SubtitleSourceError(f"字幕超过大小上限 {MAX_SUBTITLE_BYTES} 字节", transient=False)
    content, suffix = unwrap_subtitle(payload, name_hint=name_hint)
    return SubtitleDownload(content=content, suffix=suffix, provider=provider, description=description)
