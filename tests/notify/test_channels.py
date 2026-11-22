"""渠道请求形状的表测试: 方法、地址与载荷."""

from __future__ import annotations

import pytest

from amane.config.manager import NotifyKind
from amane.notify.channels import build_request

CASES = [
    pytest.param("webhook", "https://hook.test/a", "POST", "https://hook.test/a", {"title": "标题"}, id="webhook"),
    pytest.param(
        "bark",
        "https://bark.test/key",
        "GET",
        "https://bark.test/key/%E6%A0%87%E9%A2%98/%E6%AD%A3%E6%96%87",
        None,
        id="bark",
    ),
    pytest.param("serverchan", "https://sct.test/x", "POST", "https://sct.test/x", {"desp": "正文"}, id="serverchan"),
    pytest.param("telegram", "https://tg.test/x", "POST", "https://tg.test/x", {"text": "标题\n正文"}, id="telegram"),
]


@pytest.mark.parametrize(("kind", "url", "method", "expected_url", "field"), CASES)
def test_request_shape(
    kind: NotifyKind, url: str, method: str, expected_url: str, field: dict[str, str] | None
) -> None:
    request = build_request(kind, url, "标题", "正文")
    assert request.method == method
    assert request.url == expected_url
    if field is not None:
        payload = request.form if request.form is not None else request.json
        assert payload is not None
        for key, value in field.items():
            assert payload[key] == value
