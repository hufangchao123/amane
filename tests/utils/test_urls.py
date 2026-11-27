"""URL 脱敏: 密钥类查询参数与 userinfo 不得落进日志、录制与异常文案."""

import pytest

from amane.net.errors import FailureKind, RequestError, RequestFailure
from amane.utils.urls import redact_text, redact_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param(
            "https://sub.example.com/v1/sub/search?q=ABC-123&token=s3cret",
            "https://sub.example.com/v1/sub/search?q=ABC-123&token=***",
            id="query-token",
        ),
        pytest.param("https://host/a?api_key=k&page=2", "https://host/a?api_key=***&page=2", id="query-api-key"),
        pytest.param("https://host/a?access_token=t", "https://host/a?access_token=***", id="query-access-token"),
        pytest.param("https://host/a?sign=abc&ts=1", "https://host/a?sign=***&ts=1", id="query-sign"),
        pytest.param("https://user:pw@host:8443/a", "https://host:8443/a", id="userinfo"),
        pytest.param("https://host/a?keyword=abc", "https://host/a?keyword=abc", id="keyword-not-secret"),
        pytest.param("https://host/a?signed=1", "https://host/a?signed=1", id="signed-not-secret"),
        pytest.param("https://host/a?apikey=k&page=2", "https://host/a?apikey=***&page=2", id="query-apikey"),
        pytest.param("https://host/a?accessToken=t", "https://host/a?accessToken=***", id="query-camel-access"),
        pytest.param("https://host/a?AccessKeyId=id", "https://host/a?AccessKeyId=***", id="query-accesskeyid"),
        pytest.param("https://host/a?signature=s", "https://host/a?signature=***", id="query-signature"),
        pytest.param("https://host/a?X-Amz-Credential=c", "https://host/a?X-Amz-Credential=***", id="query-credential"),
        pytest.param(
            "https://api.day.app/AbCdEf1234567890XyZ/标题/正文",
            "https://api.day.app/***/标题/正文",
            id="path-device-key",
        ),
        pytest.param("https://host/api/v1/items", "https://host/api/v1/items", id="path-not-secret"),
        pytest.param(
            "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage",
            "https://api.telegram.org/***/sendMessage",
            id="path-telegram-bot-token",
        ),
        pytest.param(
            "https://sctapi.ftqq.com/SCT12345Tabcdefghijklmnop.send",
            "https://sctapi.ftqq.com/***",
            id="path-serverchan-sendkey",
        ),
        pytest.param(
            "https://api.telegram.org/bot123456789/getMe",
            "https://api.telegram.org/bot123456789/getMe",
            id="path-telegram-method",
        ),
        pytest.param(
            "https://host/files/report.send", "https://host/files/report.send", id="path-send-but-not-sendkey"
        ),
        pytest.param(
            "https://host/media/video:1080p-h264", "https://host/media/video:1080p-h264", id="path-colon-not-bot"
        ),
        pytest.param("https://host/v1.2/items.html", "https://host/v1.2/items.html", id="path-dot-not-secret"),
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
        "https://sub.example.com/v1/sub/search?q=ABC-123&token=s3cret",
        RequestFailure(kind=FailureKind.TIMEOUT, message="timeout"),
    )
    assert "s3cret" not in str(exc)
    assert "token=***" in str(exc)
    assert exc.url.endswith("token=s3cret"), "原始 URL 保留给调用方, 供重试与定位"


class TestRedactText:
    def test_query_secrets_in_free_text(self) -> None:
        text = "GET https://sub.example.com/search?q=AD-1&api_key=SECRET failed"
        assert redact_text(text) == "GET https://sub.example.com/search?q=AD-1&api_key=*** failed"

    def test_bearer_token_is_masked(self) -> None:
        assert "SUPERSECRETVALUE" not in redact_text("Authorization: Bearer SUPERSECRETVALUE")
        assert "Bearer=***" in redact_text("Authorization: Bearer SUPERSECRETVALUE")

    def test_plain_text_is_untouched(self) -> None:
        assert redact_text("磁盘已满") == "磁盘已满"
        assert redact_text("") == ""

    def test_userinfo_is_stripped(self) -> None:
        assert redact_text("connecting to http://user:pass@10.0.0.2:7890/") == ("connecting to http://10.0.0.2:7890/")
