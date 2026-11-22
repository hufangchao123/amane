"""URL 脱敏: 日志、录制与异常文案里不出现明文查询参数.

查询参数里的 ``token`` / ``api_key`` / ``secret`` 一类值与 URL userinfo 一律替换为 ``***``,
其余参数、路径与 fragment 保持不变, 便于比对同一请求.
"""

from __future__ import annotations

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


def is_secret_query_key(key: str) -> bool:
    """查询参数名是否属密钥类: 全名相等或带密钥类后缀, 避免把 ``keyword`` 认成 ``key``."""
    normalized = key.lower().replace("-", "_")
    if normalized in _SECRET_QUERY_KEYS:
        return True
    return normalized.endswith(_SECRET_QUERY_SUFFIXES)


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
    """返回脱敏后的 URL; 不含查询参数与 userinfo 时原样返回."""
    if "?" not in url and "@" not in url.split("//", 1)[-1].split("/", 1)[0]:
        return url
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
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
