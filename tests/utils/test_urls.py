"""URL 脱敏: 密钥类查询参数与 userinfo 不得落进日志、录制与异常文案."""

import pytest

from amane.net.errors import FailureKind, RequestError, RequestFailure
from amane.utils.urls import redact_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param(
            "https://api.assrt.net/v1/sub/search?q=ABC-123&token=s3cret",
            "https://api.assrt.net/v1/sub/search?q=ABC-123&token=***",
            id="query-token",
        ),
        pytest.param("https://host/a?api_key=k&page=2", "https://host/a?api_key=***&page=2", id="query-api-key"),
        pytest.param("https://host/a?access_token=t", "https://host/a?access_token=***", id="query-access-token"),
        pytest.param("https://host/a?sign=abc&ts=1", "https://host/a?sign=***&ts=1", id="query-sign"),
        pytest.param("https://user:pw@host:8443/a", "https://host:8443/a", id="userinfo"),
        pytest.param("https://host/a?keyword=abc", "https://host/a?keyword=abc", id="keyword-not-secret"),
        pytest.param("https://host/a?page=2", "https://host/a?page=2", id="plain-query"),
        pytest.param("https://host/a", "https://host/a", id="no-query"),
        pytest.param(
            "https://host:not-a-port/a?token=t", "https://host:not-a-port/a?token=t", id="invalid-port-passthrough"
        ),
    ],
)
def test_redact_url(url: str, expected: str) -> None:
    assert redact_url(url) == expected


def test_request_error_str_hides_token() -> None:
    """异常文案会进日志与任务记录, 不能带明文 token."""
    exc = RequestError(
        "https://api.assrt.net/v1/sub/search?q=ABC-123&token=s3cret",
        RequestFailure(kind=FailureKind.TIMEOUT, message="timeout"),
    )
    assert "s3cret" not in str(exc)
    assert "token=***" in str(exc)
    assert exc.url.endswith("token=s3cret"), "原始 URL 保留给调用方, 供重试与定位"
