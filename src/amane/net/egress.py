"""出站 URL 策略: 协议白名单与内网地址判定.

协议是全局底线 (``WebClient`` 的每个请求都校验): curl 本身还支持 ``file`` / ``gopher`` 等协议,
而调用方的 URL 可能来自第三方数据. 地址判定按调用点开启 — 资源下载把第三方 URL 带进出站通道,
是否允许私网由调用方决定 (见 ``docs/dev/api.md``).
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from urllib.parse import SplitResult, urlsplit

from .errors import EgressRejected, FailureKind, RequestError, RequestFailure

_ALLOWED_SCHEMES = frozenset({"http", "https"})

_DEFAULT_PORTS = {"http": 80, "https": 443}

_INTERNAL_NETWORKS = (
    ip_network("0.0.0.0/8"),
    ip_network("10.0.0.0/8"),
    ip_network("100.64.0.0/10"),
    ip_network("127.0.0.0/8"),
    ip_network("169.254.0.0/16"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("::/128"),
    ip_network("::1/128"),
    ip_network("64:ff9b:1::/48"),
    ip_network("fc00::/7"),
    ip_network("fe80::/10"),
    ip_network("fec0::/10"),
)
"""指向本机或内网的地址段. 判定只拒绝这些段, 其余地址一律放行, 不可达的地址在真实连接时失败.

不得改用 ``is_global`` / ``is_private``: 两者把 Teredo (``2001::/32``)、基准测试段 (``198.18.0.0/15``、
``2001:2::/48``) 与文档段 (``2001:db8::/32``) 等算作非公网, 而 DNS 封堵应答 (``2001::/32`` 内的随机地址)
与 fake-IP DNS (基准测试段) 恰好落在其中. 它们不指向内网, 按非公网拒绝会把网络环境问题误报为地址不合规.

``64:ff9b:1::/48`` 是本地 NAT64 前缀 (RFC 8215), 网关可将其转换为内网 IPv4; 内嵌位置随前缀长度
变化, 无法拆出判定, 因此整段拒绝."""

_NAT64_PREFIX = ip_network("64:ff9b::/96")


def _rejected(url: str, message: str, *, detail: str = "URL 不受支持") -> EgressRejected:
    return EgressRejected(url, message, detail=detail)


def _unresolved(url: str, message: str) -> RequestError:
    """解析失败按网络故障处理: 它不是策略拒绝, 调用方不应据此判断地址不合规."""
    return RequestError(url, RequestFailure(kind=FailureKind.CURL, message=message))


def _split(url: str) -> SplitResult:
    try:
        return urlsplit(url)
    except ValueError as exc:
        raise _rejected(url, f"URL 无法解析: {exc}") from exc


def ensure_http_scheme(url: str) -> None:
    """出站 URL 只接受 http(s): 其余协议不进入 curl."""
    scheme = _split(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise _rejected(url, f"拒绝的协议: {scheme or '(空)'}")


def is_internal_address(address: str) -> bool:
    """地址是否指向本机或内网. 按内嵌地址判定的 IPv6 形态有两种: IPv4 映射由内核直接连接内嵌地址,
    well-known NAT64 前缀 (``64:ff9b::/96``) 由网关转换后连接内嵌地址.

    已知边界: RFC 6052 允许网络自选 /32–/96 的 NAT64 前缀, 这类前缀不拆出判定, 其中内嵌的内网地址
    会被放行. 本地 NAT64 前缀 ``64:ff9b:1::/48`` 整段列入 ``_INTERNAL_NETWORKS``. 6to4 / Teredo 内嵌的
    是隧道端点: 封装后的包发往内嵌地址, 但到不了其上的 TCP 服务, 不拆出判定."""
    ip = ip_address(address)
    if isinstance(ip, IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64_PREFIX:
            ip = IPv4Address(int(ip) & 0xFFFFFFFF)
    return any(ip in network for network in _INTERNAL_NETWORKS)


_LOCAL_HOST_SUFFIXES = (".local", ".lan", ".internal", ".home", ".localdomain", ".intranet")


def _host_part(target: str) -> str | None:
    """从完整 URL 或裸 ``host[:port]`` 取出主机名."""
    text = target.strip()
    if not text:
        return None
    if "://" in text:
        return urlsplit(text).hostname
    host = text.rsplit("@", 1)[-1].split("/", 1)[0]
    if host.startswith("[") and "]" in host:
        return host[1 : host.index("]")]
    return host.rsplit(":", 1)[0] or None


def is_local_host(host: str) -> bool:
    """主机名是否明显指向本机或内网.

    字面地址按网段判定; 名字按形态判定: ``localhost``、单段名字 (``whisper`` / ``nas``)、
    以及常见内网域名后缀. 必须解析 DNS 才知道归属的公网名字不在此列 — 那类分流交给代理侧规则.
    """
    name = host.strip().rstrip(".").lower()
    if not name:
        return False
    bare = name[1:-1] if name.startswith("[") and name.endswith("]") else name
    try:
        return is_internal_address(bare)
    except ValueError:
        pass
    if bare == "localhost" or bare.endswith(".localhost"):
        return True
    if "." not in bare:
        return True
    return bare.endswith(_LOCAL_HOST_SUFFIXES)


def proxy_for_target(target: str | None, proxy: str | None) -> str | None:
    """按目标决定是否套代理: 内网目标一律直连.

    代理侧的分流规则 (``IP-CIDR,192.168.0.0/16,DIRECT``) 只约束「被劫持的数据包」, 未必覆盖
    「客户端主动连代理端口」这条显式代理路径 — 2026-11-27 的线上事故正是这样: 大模型端点是内网
    地址, 经代理每次白等 60 秒超时, 翻译 100% 失败而报错还归到「端点没有可用账号」.
    """
    if proxy is None or target is None:
        return proxy
    host = _host_part(target)
    if host and is_local_host(host):
        return None
    return proxy


async def resolve_addresses(host: str, port: int) -> set[str]:
    """解析 host 的全部地址. 模块级函数便于测试替换, 策略对象自身不发起 I/O."""
    infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    return {str(info[4][0]) for info in infos}


def _looks_numeric(host: str) -> bool:
    """纯数值写法 (十进制 / 八进制 / 短写 / 十六进制)."""
    if host.lower().startswith("0x"):
        return True
    return bool(host) and all(ch.isdigit() or ch == "." for ch in host)


def _is_canonical_ip(host: str) -> bool:
    try:
        ip_address(host)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    """出站目标判定: ``allow_private`` 决定是否接受内网地址, ``max_redirects`` 是逐跳复查的跳数上限."""

    allow_private: bool = False
    max_redirects: int = 5

    def ensure_allowed_static(self, url: str) -> None:
        """不解析主机名的地址判定: 只查协议与 URL 里直接写出的地址.

        同步场景 (播放列表改写等回调) 无法等待 DNS, 用本方法挡掉写成字面量或数值形式的内网目标.
        主机名解析到内网的情况查不到, 那条路径必须另外调用 ``ensure_allowed``.
        """
        ensure_http_scheme(url)
        if self.allow_private:
            return

        parsed = _split(url)
        host = parsed.hostname
        if not host:
            raise _rejected(url, "URL 缺少主机名")
        if _looks_numeric(host) and not _is_canonical_ip(host):
            raise _rejected(url, f"非规范的数值主机: {host}")
        if _is_canonical_ip(host) and is_internal_address(host):
            raise _rejected(url, f"地址指向内网: {host}", detail="不支持内网地址")

    async def ensure_allowed(self, url: str) -> None:
        ensure_http_scheme(url)
        if self.allow_private:
            return

        parsed = _split(url)
        host = parsed.hostname
        if not host:
            raise _rejected(url, "URL 缺少主机名")
        # curl 自己解析数值主机: 0177.0.0.1 是八进制、2130706433 是十进制, 与 getaddrinfo 的解释不同,
        # 只按解析结果判定会漏掉这些写法. 因此数值形态必须是规范写法.
        if _looks_numeric(host) and not _is_canonical_ip(host):
            raise _rejected(url, f"非规范的数值主机: {host}")
        try:
            port = parsed.port or _DEFAULT_PORTS[parsed.scheme.lower()]
        except ValueError as exc:
            raise _rejected(url, f"端口非法: {exc}") from exc

        try:
            addresses = await resolve_addresses(host, port)
        except (OSError, UnicodeError) as exc:
            raise _unresolved(url, f"主机解析失败: {exc}") from exc
        if not addresses:
            raise _unresolved(url, "主机解析无结果")

        # 任一地址指向内网就整体拒绝: 双栈主机可能同时给出公网与内网地址, 放行等于交由 curl 选择连接目标.
        for address in addresses:
            if is_internal_address(address):
                raise _rejected(url, f"地址指向内网: {address}", detail="不支持内网地址")
