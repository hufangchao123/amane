"""115 客户端: 分页、调用间隔、限流退避与错误文案.

全部走 ``MockTransport``: 用例不连 webapi.115.com, 也不碰任何真实 Cookie.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import httpx2
import pytest

from amane.transfer import CloudEntry, Transfer115Client, TransferError, TransferNotConfigured

if TYPE_CHECKING:
    from collections.abc import Callable

COOKIE = "UID=1_A1_1;CID=fake;SEID=fake"
"""假 Cookie: 仓库里不得出现真实凭据, 用例只验证它进请求头、不进错误消息."""


def _client(
    handler: Callable[[httpx2.Request], httpx2.Response],
    sleeps: list[float],
    *,
    page_limit: int = 2,
    max_entries: int = 500,
    max_retry: int = 3,
) -> Transfer115Client:
    # clock 恒为 0: 每次调用都要等满间隔, 用例才能断言节奏而不真睡.
    return Transfer115Client(
        COOKIE,
        transport=httpx2.MockTransport(handler),
        page_limit=page_limit,
        max_entries=max_entries,
        max_retry=max_retry,
        sleep=sleeps.append,
        clock=lambda: 0.0,
    )


def _file(fid: str, name: str) -> dict[str, object]:
    return {"fid": fid, "n": name, "s": 2048}


def _folder(cid: str, name: str) -> dict[str, object]:
    return {"cid": cid, "n": name}


def _ok(data: list[object]) -> httpx2.Response:
    return httpx2.Response(200, json={"state": True, "data": data})


def test_list_dir_pages_until_short_batch() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.params["offset"])
        if request.url.params["offset"] == "0":
            return _ok([_file("1", "IPX-123.mp4"), _file("2", "IPX-124.mp4")])
        return _ok([_folder("9", "IPX-125")])

    sleeps: list[float] = []
    client = _client(handler, sleeps)
    entries = client.list_dir("100")
    client.close()

    assert [entry.key for entry in entries] == ["1", "2", "9"]
    assert [entry.is_dir for entry in entries] == [False, False, True]
    assert seen == ["0", "2"], "读到短页就停, 不猜总数"


def test_list_dir_sends_auth_and_window_params() -> None:
    captured: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(request)
        return _ok([])

    client = _client(handler, [])
    client.list_dir("100")
    client.close()

    request = captured[0]
    assert request.headers["cookie"] == COOKIE
    assert request.url.params["cid"] == "100"
    assert request.url.params["aid"] == "1"
    assert request.url.params["show_dir"] == "1"


def test_list_dir_truncates_at_max_entries() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.params["offset"])
        return _ok([_file("1", "a.mp4"), _file("2", "b.mp4"), _file("3", "c.mp4")])

    sleeps: list[float] = []
    client = _client(handler, sleeps, max_entries=2)
    entries = client.list_dir("100")
    client.close()

    assert [entry.key for entry in entries] == ["1", "2"]
    assert seen == ["0"], "读够上限就不再请求下一页"


def test_list_dir_drops_unparsable_entries() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        offset = request.url.params["offset"]
        seen.append(offset)
        return _ok([None, {"n": "无名.mp4"}, {"fid": 7, "n": "ok.mp4"}, "junk"] if offset == "0" else [])

    client = _client(handler, [], page_limit=4)
    entries = client.list_dir("100")
    client.close()

    assert [(entry.key, entry.fid) for entry in entries] == [("7", "7")]
    assert seen == ["0", "4"], "坏条目仍占分页窗口, 否则会把后面的页漏掉"


def test_adjacent_calls_keep_interval() -> None:
    responses = [_ok([_file("1", "a.mp4")]), _ok([])]
    requests = iter(responses)

    sleeps: list[float] = []
    client = _client(lambda request: next(requests), sleeps, page_limit=1)
    client.list_dir("100")
    client.close()

    assert sleeps == [0.8], "第二枪要等满相邻间隔"


def test_move_and_trash_use_longer_gap_and_pid() -> None:
    bodies: list[dict[str, list[str]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(parse_qs(request.content.decode()))
        assert request.method == "POST"
        return httpx2.Response(200, json={"state": True})

    sleeps: list[float] = []
    client = _client(handler, sleeps)
    client.move("1", "5000")
    client.trash("2")
    client.close()

    assert bodies == [{"fid[0]": ["1"], "pid": ["5000"]}, {"fid[0]": ["2"]}]
    assert sleeps == [1.5], "搬移类调用之间用更长的间隔"


def test_rate_limit_backs_off_exponentially() -> None:
    state = {"calls": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        state["calls"] += 1
        if state["calls"] <= 2:
            return httpx2.Response(429, text="too many requests")
        return _ok([])

    sleeps: list[float] = []
    client = _client(handler, sleeps)
    assert client.list_dir("100") == []
    client.close()

    assert [gap for gap in sleeps if gap >= 2.0] == [2.0, 4.0]


def test_rate_limit_message_is_detected_without_429() -> None:
    """115 常以 200 + state=false + "操作频繁" 的形式限流, 不能只认状态码."""
    state = {"calls": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx2.Response(200, json={"state": False, "error": "操作太频繁, 请稍后再试"})
        return _ok([])

    sleeps: list[float] = []
    client = _client(handler, sleeps)
    assert client.list_dir("100") == []
    client.close()

    assert sleeps == [2.0, 0.8], "退避之后还要继续守间隔"


def test_rate_limit_gives_up_after_retries() -> None:
    sleeps: list[float] = []
    client = _client(lambda request: httpx2.Response(429, text="too many"), sleeps)

    with pytest.raises(TransferError, match="持续限流"):
        client.list_dir("100")
    client.close()

    assert [gap for gap in sleeps if gap >= 2.0] == [2.0, 4.0, 8.0]


def test_transport_error_is_not_retried() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("boom")

    sleeps: list[float] = []
    client = _client(handler, sleeps)

    with pytest.raises(TransferError, match="115 请求失败: /files"):
        client.list_dir("100")
    client.close()

    assert sleeps == [], "传输层失败不重试, 否则这一轮会被拖长"


def test_auth_failure_message_never_contains_cookie() -> None:
    client = _client(lambda request: httpx2.Response(401, text="unauthorized"), [])

    with pytest.raises(TransferError) as excinfo:
        client.list_dir("100")
    client.close()

    message = str(excinfo.value)
    assert "鉴权失败" in message
    assert COOKIE not in message


def test_rejected_state_raises_with_reason() -> None:
    client = _client(lambda request: httpx2.Response(200, json={"state": False, "error": "目录不存在"}), [])

    with pytest.raises(TransferError, match="115 拒绝 /files: 目录不存在"):
        client.list_dir("404")
    client.close()


def test_empty_cookie_is_not_configured() -> None:
    """缺 Cookie 属"未启用", 不是故障: 上层据此收尾, 不该当成异常崩掉."""
    with pytest.raises(TransferNotConfigured, match="未启用"):
        Transfer115Client("   ")


def test_cloud_entry_key_prefers_fid() -> None:
    assert CloudEntry(name="a.mp4", fid="1", cid=None, size=0, is_dir=False).key == "1"
    assert CloudEntry(name="夹", fid=None, cid="9", size=0, is_dir=True).key == "9"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"page_limit": 0}, "page_limit"),
        ({"page_limit": -1}, "page_limit"),
        ({"max_entries": 0}, "max_entries"),
        ({"max_retry": -1}, "max_retry"),
        ({"retry_base": -0.5}, "retry_base"),
    ],
)
def test_absurd_limits_are_rejected(overrides: dict[str, object], message: str) -> None:
    """越界参数不会立刻报错, 只会在跑起来之后把分页卡死或把限流关掉: 构造时就挡住."""
    kwargs: dict[str, object] = {"transport": httpx2.MockTransport(lambda request: _ok([])), **overrides}

    with pytest.raises(ValueError, match=message):
        Transfer115Client(COOKIE, sleep=lambda _: None, clock=lambda: 0.0, **kwargs)  # type: ignore[arg-type]


def test_page_limit_is_capped_by_total_window() -> None:
    """单页比总量上限还大等于白要: 一页就够满, 请求里的 ``limit`` 收到上限为止."""
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.params["limit"])
        return _ok([_file("1", "a.mp4"), _file("2", "b.mp4")])

    client = _client(handler, [], page_limit=50, max_entries=2)
    entries = client.list_dir("100")
    client.close()

    assert [entry.key for entry in entries] == ["1", "2"]
    assert seen == ["2"], "单页窗口被夹到总量上限, 且读满上限就不再翻页"


def test_empty_page_ends_paging_even_when_previous_page_was_full() -> None:
    """网盘在还有条目时也可能回一个空页; 空页即终点, 否则这个循环会一直转."""
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        offset = request.url.params["offset"]
        seen.append(offset)
        if offset == "0":
            return _ok([_file("1", "a.mp4"), _file("2", "b.mp4")])
        return _ok([])

    client = _client(handler, [], page_limit=2, max_entries=10)
    entries = client.list_dir("100")
    client.close()

    assert [entry.key for entry in entries] == ["1", "2"]
    assert seen == ["0", "2"], "空页之后就停, 不再按 offset 往上加"
