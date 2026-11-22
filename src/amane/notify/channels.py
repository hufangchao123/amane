"""各通知渠道的请求形状.

渠道之间的差别只在方法、地址与载荷三件事, 因此集中在一张表里, 发送逻辑只消费这张表.
地址由使用者在配置里填写, 可能指向内网 (自建 Bark / Server 酱代理), 因此这里不做内网出站限制.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from ..config.manager import NotifyKind

REQUEST_TIMEOUT_FLOOR = 1.0


@dataclass(frozen=True)
class NotifyRequest:
    """一次推送的请求形状; ``form`` 与 ``json`` 至多一个非空."""

    method: str
    url: str
    json: dict[str, str] | None = None
    form: dict[str, str] | None = None


def build_request(kind: NotifyKind, url: str, title: str, body: str) -> NotifyRequest:
    """按渠道拼出请求; 未知渠道按 ``webhook`` 处理, 与配置校验的兜底一致."""
    if kind == "bark":
        return NotifyRequest("GET", f"{url.rstrip('/')}/{quote(title, safe='')}/{quote(body, safe='')}")
    if kind == "serverchan":
        return NotifyRequest("POST", url, form={"title": title, "desp": body})
    if kind == "telegram":
        return NotifyRequest("POST", url, json={"text": f"{title}\n{body}"})
    return NotifyRequest("POST", url, json={"title": title, "body": body})
