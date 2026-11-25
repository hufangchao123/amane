"""URL 脱敏: 日志、录制与异常文案里不出现明文查询参数.

查询参数里的 ``token`` / ``api_key`` / ``secret`` 一类值与 URL userinfo 一律替换为 ``***``,
其余参数、路径与 fragment 保持不变, 便于比对同一请求.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

REDACTED_QUERY_VALUE = "***"

_SECRET_QUERY_KEYS = frozenset(
    {
        "token",
        "key",
        "secret",
        "password",
        "passwd",
        "auth",
        "cookie",
        "session",
        "sessionid",
        "sign",
        "sig",
        "credential",
    }
)

_SECRET_QUERY_SUFFIXES = ("_key", "_token", "_secret", "_password", "_passwd", "_sign", "_sig", "_auth")

#: 名字里出现这些片段就当密钥: apikey / accessToken / AccessKeyId / signature 都不带下划线.
_SECRET_QUERY_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "apikey",
    "accesskey",
    "credential",
    "signature",
    "cookie",
    "authorization",
)

#: 路径段满足这三条才当密钥: 够长、只有 URL 安全字符、字母与数字都有.
_MIN_SECRET_SEGMENT = 16

#: 通知渠道把 token 当路径段拼进地址, 形状固定, 按形状判断.
#: Telegram: ``https://api.telegram.org/bot<id>:<token>/sendMessage``.
_TELEGRAM_BOT_SEGMENT = re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}")
#: ServerChan: ``https://sctapi.ftqq.com/<SendKey>.send``, SendKey 形如 ``SCT12345T...``.
_SERVERCHAN_SEGMENT = re.compile(r"sct[A-Za-z0-9]+t[A-Za-z0-9]{8,}\.send", re.IGNORECASE)


def is_secret_query_key(key: str) -> bool:
    """查询参数名是否属密钥类.

    判据三条取「或」: 全名相等、带密钥类后缀、名字里含密钥片段. ``keyword`` / ``signed``
    这类正常参数不会被误伤 —— 前者不在片段表里, 后者不满足后缀与全名.
    """
    normalized = key.lower().replace("-", "_")
    if normalized in _SECRET_QUERY_KEYS:
        return True
    if normalized.endswith(_SECRET_QUERY_SUFFIXES):
        return True
    compact = normalized.replace("_", "")
    return any(fragment in compact for fragment in _SECRET_QUERY_FRAGMENTS)


def is_secret_path_segment(segment: str) -> bool:
    """路径段是否像密钥 (bark 这类把设备键写在路径里的地址).

    Telegram 与 ServerChan 的路径段带 ``:`` 与 ``.``, 过不了字符类别判据, 先按形状判断.
    """
    if _TELEGRAM_BOT_SEGMENT.fullmatch(segment) is not None:
        return True
    if _SERVERCHAN_SEGMENT.fullmatch(segment) is not None:
        return True
    if len(segment) < _MIN_SECRET_SEGMENT:
        return False
    if not all(char.isalnum() or char in "-_" for char in segment):
        return False
    return any(char.isdigit() for char in segment) and any(char.isalpha() for char in segment)


def _redact_path(path: str) -> str:
    if "/" not in path:
        return path
    return "/".join(REDACTED_QUERY_VALUE if is_secret_path_segment(segment) else segment for segment in path.split("/"))


def redact_query(query: str) -> str:
    """只脱敏查询串; 空串原样返回."""
    if not query:
        return query
    pairs = parse_qsl(query, keep_blank_values=True)
    if not any(is_secret_query_key(name) for name, _ in pairs):
        return query
    return "&".join(
        f"{name}={REDACTED_QUERY_VALUE}" if is_secret_query_key(name) else f"{name}={value}" for name, value in pairs
    )


def redact_url(url: str) -> str:
    """返回脱敏后的 URL; 不含查询参数、userinfo 与疑似密钥路径段时原样返回."""
    if "?" not in url and "@" not in url.split("//", 1)[-1].split("/", 1)[0]:
        return _redact_path(url)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return url
    netloc = parts.netloc
    if "@" in netloc:
        host = parts.hostname or ""
        netloc = host if port is None else f"{host}:{port}"
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        if any(is_secret_query_key(name) for name, _ in pairs):
            query = "&".join(
                f"{name}={REDACTED_QUERY_VALUE}" if is_secret_query_key(name) else f"{name}={value}"
                for name, value in pairs
            )
    return urlunsplit((parts.scheme, netloc, _redact_path(parts.path), query, parts.fragment))


_URL_IN_TEXT = re.compile(r"https?://[^\s'\"<>()\[\]]+")
_BEARER_IN_TEXT = re.compile(r"(?i)\b(bearer|token|api[_-]?key|password)=?\s*([A-Za-z0-9._~+/-]{8,})")


def redact_text(text: str) -> str:
    """把自由文本里的 URL 与常见凭据形态脱敏.

    异常文案、渠道报错、HTTP 响应摘要都会被直接展示或入库, 里面常带着带参 URL 与
    ``Authorization`` 头. 这里按形态替换, 不依赖调用方知不知道哪个字段敏感.
    """
    if not text:
        return text
    redacted = _URL_IN_TEXT.sub(lambda match: redact_url(match.group(0)), text)
    return _BEARER_IN_TEXT.sub(lambda match: f"{match.group(1)}={REDACTED_QUERY_VALUE}", redacted)
