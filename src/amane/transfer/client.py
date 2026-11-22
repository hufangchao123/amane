"""115 web API 客户端: 只覆盖中转用到的三个端点 (列目录 / 搬移 / 删进回收站).

为什么不用 ``net/http.py::WebClient``: 那边的限流器面向并发抓取, 而 115 的节奏由它自己的
间隔与退避决定 (相邻调用 ≥0.8s、搬移类调用 ≥1.5s、限流退避 2/4/8s), 一次任务内严格串行;
换异步客户端只会把节奏拆到两处. 这里同步实现, 由 handler 丢进线程, 事件循环不被占住.

Cookie 只出现在请求头里: 不进日志、不进异常消息、不进返回值.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx2

API_BASE = "https://webapi.115.com"

REQ_INTERVAL = 0.8
"""相邻两次调用的最小间隔: 网盘按秒计频, 连发的第二枪就会被限流."""

MOVE_INTERVAL = 1.5
"""搬移与删除的最小间隔: 比读取更敏感, 取面板实测值."""

MAX_RETRY = 3
RETRY_BASE = 2.0
"""限流退避: 按 2/4/8 秒递增, 用完即报错, 把这一轮留给下一次任务."""

PAGE_LIMIT = 200
MAX_ENTRIES = 500
"""单个目录最多读这么多条. 中转源目录是"近期待搬"的窗口, 更大说明配置指错了目录."""

TIMEOUT_SECONDS = 30.0

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_RATELIMIT_MARKERS = ("开小差", "稍后", "频繁", "限流", "too many", "rate limit", "try again", "429")
_MESSAGE_KEYS = ("error", "err", "msg", "message")


class TransferError(RuntimeError):
    """115 调用失败. 消息里只有端点与网盘回的话, 永远不带 Cookie."""


class TransferNotConfigured(TransferError):
    """缺凭据: 属于"未启用"而不是故障, 任务据此直接收尾."""


@dataclass(frozen=True, slots=True)
class CloudEntry:
    """115 目录里的一条. 文件有 ``fid``, 文件夹有 ``cid``, 只有一种有值."""

    name: str
    fid: str | None
    cid: str | None
    size: int
    is_dir: bool

    @property
    def key(self) -> str:
        """落库主键: 文件用 fid, 文件夹用 cid (``transfer_items`` 只有一个 ID 列)."""
        return self.fid or self.cid or ""


class TransferClientLike(Protocol):
    """handler 使用的最小客户端契约; 测试以替身实现."""

    def list_dir(self, cid: str) -> list[CloudEntry]: ...

    def move(self, fid: str, pid: str) -> None: ...

    def trash(self, fid: str) -> None: ...

    def close(self) -> None: ...


class Transfer115Client:
    """同步的 115 客户端. 调用间隔与退避都在这里, 调用方只关心结果.

    ``sleep`` / ``clock`` 是测试注入点: 用例要断言间隔与退避秒数, 不能真等.
    """

    def __init__(
        self,
        cookies: str,
        *,
        base_url: str = API_BASE,
        interval: float = REQ_INTERVAL,
        move_interval: float = MOVE_INTERVAL,
        max_retry: int = MAX_RETRY,
        retry_base: float = RETRY_BASE,
        page_limit: int = PAGE_LIMIT,
        max_entries: int = MAX_ENTRIES,
        transport: httpx2.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not cookies.strip():
            raise TransferNotConfigured("115 Cookie 为空, 中转未启用")
        _check_limits(
            page_limit=page_limit,
            max_entries=max_entries,
            max_retry=max_retry,
            interval=interval,
            move_interval=move_interval,
            retry_base=retry_base,
        )
        self._cookies = cookies.strip()
        self._base_url = base_url.rstrip("/")
        self._interval = interval
        self._move_interval = move_interval
        self._max_retry = max_retry
        self._retry_base = retry_base
        # 单页比总量上限还大没有意义: 一页就拿满上限, 只会让 115 白回一批用不上的条目.
        self._page_limit = min(page_limit, max_entries)
        self._max_entries = max_entries
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None
        self._http = httpx2.Client(transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=True)

    def close(self) -> None:
        self._http.close()

    def list_dir(self, cid: str) -> list[CloudEntry]:
        """把一个目录读全, 最多 ``MAX_ENTRIES`` 条; 返回项里不可解析的条目直接丢掉."""
        entries: list[CloudEntry] = []
        offset = 0
        while offset < self._max_entries:
            payload = self._request(
                "GET",
                "/files",
                params={"aid": 1, "cid": cid, "offset": offset, "limit": self._page_limit, "show_dir": 1},
            )
            batch = _entry_list(payload)
            if not batch:
                # 翻页的终点由"这一页空了"决定, 不只靠"这一页不满": 网盘偶尔在还有条目时
                # 回一个空页, 而 offset 不再前进会把这个循环卡死.
                break
            entries.extend(entry for raw in batch if (entry := _parse_entry(raw)) is not None)
            offset += len(batch)
            if len(batch) < self._page_limit:
                break
        return entries[: self._max_entries]

    def move(self, fid: str, pid: str) -> None:
        """搬进目标目录.

        目标目录参数是 ``pid``: 面板吃过亏 —— 传 ``cid`` / ``to_cid`` 会被当成 pid=0 搬到根目录.
        """
        self._request("POST", "/files/move", data={"fid[0]": fid, "pid": pid}, mutating=True)

    def trash(self, fid: str) -> None:
        """删除到 115 回收站: 可恢复, 比就地删除安全."""
        self._request("POST", "/rb/delete", data={"fid[0]": fid}, mutating=True)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        data: dict[str, str] | None = None,
        mutating: bool = False,
    ) -> dict[str, object]:
        attempt = 0
        while True:
            self._wait_turn(self._move_interval if mutating else self._interval)
            try:
                response = self._http.request(
                    method,
                    f"{self._base_url}{path}",
                    params=params,
                    data=data,
                    headers={"Cookie": self._cookies, "User-Agent": _USER_AGENT},
                )
            except httpx2.HTTPError as exc:
                # 传输层失败不重试: 网盘抖动时重试会把这一轮拖长, 让任务无法按时收尾.
                raise TransferError(f"115 请求失败: {path} ({exc.__class__.__name__})") from exc
            finally:
                self._last_call = self._clock()
            payload = _json_object(response)
            if _rate_limited(response.status_code, payload):
                if attempt >= self._max_retry:
                    raise TransferError(f"115 持续限流: {path}, 本轮放弃")
                self._sleep(self._retry_base * 2**attempt)
                attempt += 1
                continue
            _check_status(response.status_code, path)
            if payload.get("state") is not True:
                raise TransferError(f"115 拒绝 {path}: {_message(payload) or '未知原因'}")
            return payload

    def _wait_turn(self, gap: float) -> None:
        """相邻两次调用之间至少隔 ``gap`` 秒; 首次调用不等."""
        if self._last_call is not None:
            remaining = gap - (self._clock() - self._last_call)
            if remaining > 0:
                self._sleep(remaining)


def _check_limits(
    *,
    page_limit: int,
    max_entries: int,
    max_retry: int,
    interval: float,
    move_interval: float,
    retry_base: float,
) -> None:
    """把构造参数挡在合法区间内: 越界的翻页窗口与间隔不会报错, 只会把一轮任务卡死或空转.

    分页参数尤其要挡: ``page_limit <= 0`` 时"本页不满就停"的条件永远不成立, 目录读不完整;
    间隔或退避为负则相当于关掉限流, 115 会直接把这一轮打成 429.
    """
    if page_limit < 1:
        raise ValueError(f"page_limit 必须 ≥ 1, 收到 {page_limit}")
    if max_entries < 1:
        raise ValueError(f"max_entries 必须 ≥ 1, 收到 {max_entries}")
    if max_retry < 0:
        raise ValueError(f"max_retry 不能为负, 收到 {max_retry}")
    for name, value in (("interval", interval), ("move_interval", move_interval), ("retry_base", retry_base)):
        if value < 0:
            raise ValueError(f"{name} 不能为负, 收到 {value}")


def _json_object(response: httpx2.Response) -> dict[str, object]:
    """115 的错误也可能带非 JSON 正文 (网关页), 解不出就当空对象, 由状态码决定报错."""
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _entry_list(payload: dict[str, object]) -> list[object]:
    data = payload.get("data")
    return data if isinstance(data, list) else []


def _text(value: object) -> str | None:
    """115 的 ID 有时是字符串有时是整数; 落库与比较统一按字符串."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _parse_entry(raw: object) -> CloudEntry | None:
    if not isinstance(raw, dict):
        return None
    name = _text(raw.get("n"))
    fid = _text(raw.get("fid"))
    cid = _text(raw.get("cid"))
    if name is None or (fid is None and cid is None):
        return None
    size = raw.get("s")
    return CloudEntry(
        name=name,
        fid=fid,
        cid=cid,
        size=size if isinstance(size, int) and not isinstance(size, bool) else 0,
        # 文件夹带 cid, 文件带 fid; ``tp`` 是时间戳, 不能拿来判类型 (面板踩过).
        is_dir=fid is None,
    )


def _message(payload: dict[str, object]) -> str:
    for key in _MESSAGE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _rate_limited(status: int, payload: dict[str, object]) -> bool:
    if status == 429:
        return True
    if payload.get("state") is True:
        return False
    text = _message(payload).lower()
    return any(marker in text for marker in _RATELIMIT_MARKERS)


def _check_status(status: int, path: str) -> None:
    if status in (401, 403):
        raise TransferError(f"115 鉴权失败: {path} (Cookie 可能已失效)")
    if status >= 400:
        raise TransferError(f"115 返回 {status}: {path}")
