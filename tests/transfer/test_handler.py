"""``TRANSFER_115`` 的编排: 首见登记、成熟等待、广告策略、单轮上限与失败落库.

网盘客户端是替身: 用例只验证编排, 不该连 webapi.115.com.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest

from amane.config import HotSettings
from amane.config.manager import TransferConfig
from amane.db.models import TransferItemState
from amane.handlers import transfer115
from amane.handlers.models import Transfer115Payload
from amane.handlers.transfer115 import (
    AD_SKIPPED,
    AD_TRASHED,
    FOLDER_BLOCKED,
    MAX_MOVES_PER_RUN,
    Transfer115Handler,
)
from amane.transfer import CloudEntry, Transfer115Client, TransferError

if TYPE_CHECKING:
    from amane.db.repository import Repository

pytestmark = pytest.mark.asyncio(loop_scope="function")

SOURCE = "100"
TARGET = "5000"
COOKIE = "UID=1_A1_1;CID=fake;SEID=fake"
KEYWORDS = ["台湾uu"]


class _FakeCloud:
    """115 客户端替身: 按预设目录应答, 记录被搬与被删的 fid."""

    def __init__(
        self,
        listing: dict[str, list[CloudEntry]],
        *,
        fail_list: bool = False,
        fail_move: bool = False,
        fail_trash: bool = False,
    ) -> None:
        self.listing = listing
        self.fail_list = fail_list
        self.fail_move = fail_move
        self.fail_trash = fail_trash
        self.listed: list[str] = []
        self.moves: list[tuple[str, str]] = []
        self.trashed: list[str] = []
        self.closed = False

    def list_dir(self, cid: str) -> list[CloudEntry]:
        self.listed.append(cid)
        if self.fail_list:
            raise TransferError("115 请求失败: /files")
        return self.listing.get(cid, [])

    def move(self, fid: str, pid: str) -> None:
        if self.fail_move:
            raise TransferError("115 拒绝 /files/move: 目标目录不存在")
        self.moves.append((fid, pid))

    def trash(self, fid: str) -> None:
        if self.fail_trash:
            raise TransferError("115 拒绝 /rb/delete: 未知原因")
        self.trashed.append(fid)

    def close(self) -> None:
        self.closed = True


def _file(fid: str, name: str, size: int = 2048) -> CloudEntry:
    return CloudEntry(name=name, fid=fid, cid=None, size=size, is_dir=False)


def _folder(cid: str, name: str) -> CloudEntry:
    return CloudEntry(name=name, fid=None, cid=cid, size=0, is_dir=True)


def _no_client(cookies: str) -> _FakeCloud:
    raise AssertionError(f"未启用时不该构造客户端: {cookies}")


def _handler(
    repo: Repository,
    fake: _FakeCloud,
    *,
    enabled: bool = True,
    ad_action: Literal["skip", "trash"] = "skip",
    max_moves: int | None = None,
) -> Transfer115Handler:
    hot = HotSettings(
        transfer=TransferConfig(
            enabled=enabled,
            cookies=COOKIE,
            source_cid=SOURCE,
            target_cid=TARGET,
            ad_keywords=KEYWORDS,
            ad_action=ad_action,
        )
    )
    extra: dict[str, int] = {} if max_moves is None else {"max_moves": max_moves}
    return Transfer115Handler(repo, hot, client_factory=lambda cookies: fake, **extra)


async def _states(repo: Repository) -> dict[str, TransferItemState]:
    rows, _ = await repo.list_transfer_items(state=None, offset=0, limit=100)
    return {row.fid: row.state for row in rows}


async def _row(repo: Repository, fid: str) -> tuple[TransferItemState, int, str | None]:
    rows, _ = await repo.list_transfer_items(state=None, offset=0, limit=100)
    found = next(row for row in rows if row.fid == fid)
    return found.state, found.attempts, found.error


async def test_disabled_config_finishes_without_touching_cloud(transfer_repo: Repository) -> None:
    hot = HotSettings(transfer=TransferConfig(enabled=False))
    handler = Transfer115Handler(transfer_repo, hot, client_factory=_no_client)

    result = await handler.handle(Transfer115Payload())

    assert result.success is False
    assert result.error is not None and "未启用" in result.error


async def test_missing_cookie_is_unavailable_not_a_crash(transfer_repo: Repository) -> None:
    """缺凭据只报未启用; 抛异常会让任务以失败告终, 调度器还会反复重排."""
    hot = HotSettings(transfer=TransferConfig(enabled=True, cookies=None, source_cid=SOURCE, target_cid=TARGET))
    handler = Transfer115Handler(transfer_repo, hot, client_factory=Transfer115Client)

    result = await handler.handle(Transfer115Payload())

    assert result.success is False
    assert result.error is not None and "未启用" in result.error


async def test_first_run_only_tracks(transfer_repo: Repository) -> None:
    fake = _FakeCloud({SOURCE: [_file("f1", "IPX-123.mp4"), _file("f2", "IPX-124.mp4")]})
    handler = _handler(transfer_repo, fake)

    result = await handler.handle(Transfer115Payload())

    assert result.success is True
    assert result.result is not None
    assert (result.result.scanned, result.result.moved, result.result.skipped) == (2, 0, 2)
    assert fake.moves == [], "首见只登记: 网盘还在灌数据"
    assert await _states(transfer_repo) == {"f1": TransferItemState.PENDING, "f2": TransferItemState.PENDING}
    assert fake.closed is True


async def test_source_ignores_non_media(transfer_repo: Repository) -> None:
    fake = _FakeCloud({SOURCE: [_file("f1", "封面.jpg"), _file("f2", "IPX-123.nfo"), _file("f3", "IPX-123.mp4")]})
    result = await _handler(transfer_repo, fake).handle(Transfer115Payload())

    assert result.result is not None
    assert result.result.scanned == 1
    assert await _states(transfer_repo) == {"f3": TransferItemState.PENDING}


async def test_matured_file_moves(transfer_repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transfer115, "MIN_AGE_SECONDS", 0)
    fake = _FakeCloud({SOURCE: [_file("f1", "IPX-123.mp4")]})
    handler = _handler(transfer_repo, fake)

    await handler.handle(Transfer115Payload())
    result = await handler.handle(Transfer115Payload())

    assert result.result is not None
    assert (result.result.moved, result.result.skipped, result.result.failed) == (1, 0, 0)
    assert fake.moves == [("f1", TARGET)]
    assert await _states(transfer_repo) == {"f1": TransferItemState.MOVED}


async def test_young_file_waits_for_offline_download(transfer_repo: Repository) -> None:
    """离线下载先建文件再灌数据, 不等成熟就搬会把半截文件带进 STRM 目录."""
    fake = _FakeCloud({SOURCE: [_file("f1", "IPX-123.mp4")]})
    handler = _handler(transfer_repo, fake)

    await handler.handle(Transfer115Payload())
    result = await handler.handle(Transfer115Payload())

    assert result.result is not None
    assert result.result.moved == 0
    assert fake.moves == []
    assert await _states(transfer_repo) == {"f1": TransferItemState.PENDING}


async def test_ad_file_skipped_by_default(transfer_repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transfer115, "MIN_AGE_SECONDS", 0)
    fake = _FakeCloud({SOURCE: [_file("f1", "台湾uu-学生妹.mp4")]})
    handler = _handler(transfer_repo, fake)

    await handler.handle(Transfer115Payload())
    await handler.handle(Transfer115Payload())

    assert fake.moves == []
    assert fake.trashed == []
    assert await _row(transfer_repo, "f1") == (TransferItemState.SKIPPED, 0, AD_SKIPPED)


async def test_ad_file_trashed_when_configured(transfer_repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transfer115, "MIN_AGE_SECONDS", 0)
    fake = _FakeCloud({SOURCE: [_file("f1", "台湾uu-学生妹.mp4")]})
    handler = _handler(transfer_repo, fake, ad_action="trash")

    await handler.handle(Transfer115Payload())
    await handler.handle(Transfer115Payload())

    assert fake.trashed == ["f1"]
    assert fake.moves == []
    assert await _row(transfer_repo, "f1") == (TransferItemState.SKIPPED, 0, AD_TRASHED)


async def test_folder_with_ads_is_blocked(transfer_repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    """夹里有广告就先不搬: 搬过去会把广告一起带进媒体库."""
    monkeypatch.setattr(transfer115, "MIN_AGE_SECONDS", 0)
    folder = _folder("c1", "台湾uu合集")
    fake = _FakeCloud(
        {
            SOURCE: [folder],
            "c1": [_file("a1", "台湾uu1.mp4"), _file("a2", "IPX-123.mp4")],
        }
    )
    handler = _handler(transfer_repo, fake)

    # 首轮登记, 次轮记体积, 第三轮体积没变才判广告
    for _ in range(3):
        await handler.handle(Transfer115Payload())

    assert fake.moves == []
    assert await _row(transfer_repo, "c1") == (TransferItemState.SKIPPED, 0, FOLDER_BLOCKED)


async def test_folder_ads_trashed_then_folder_moves(transfer_repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transfer115, "MIN_AGE_SECONDS", 0)
    fake = _FakeCloud(
        {
            SOURCE: [_folder("c1", "台湾uu合集")],
            "c1": [_file("a1", "台湾uu1.mp4"), _file("a2", "IPX-123.mp4")],
        }
    )
    handler = _handler(transfer_repo, fake, ad_action="trash")

    for _ in range(3):
        await handler.handle(Transfer115Payload())

    assert fake.trashed == ["a1"]
    assert fake.moves == [("c1", TARGET)]
    # 夹内广告没有自己的行: 状态以文件夹为单位, 夹内文件不在源目录里逐条登记.
    assert await _states(transfer_repo) == {"c1": TransferItemState.MOVED}


async def test_empty_folder_is_left_alone(transfer_repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    """空夹可能还在下载中, 搬过去只会留一个空壳."""
    monkeypatch.setattr(transfer115, "MIN_AGE_SECONDS", 0)
    fake = _FakeCloud({SOURCE: [_folder("c1", "IPX-123")]})
    handler = _handler(transfer_repo, fake)

    for _ in range(3):
        await handler.handle(Transfer115Payload())

    assert fake.moves == []
    assert await _states(transfer_repo) == {"c1": TransferItemState.PENDING}, "空夹可能还在下载, 不落跳过"


async def test_move_failure_is_recorded(transfer_repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transfer115, "MIN_AGE_SECONDS", 0)
    fake = _FakeCloud({SOURCE: [_file("f1", "IPX-123.mp4")]}, fail_move=True)
    handler = _handler(transfer_repo, fake)

    await handler.handle(Transfer115Payload())
    result = await handler.handle(Transfer115Payload())

    assert result.success is True, "单条搬不动不算整轮失败"
    assert result.result is not None and result.result.failed == 1
    state, attempts, error = await _row(transfer_repo, "f1")
    assert (state, attempts) == (TransferItemState.FAILED, 1)
    assert error is not None and "目标目录不存在" in error


async def test_retry_stops_after_max_attempts(transfer_repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transfer115, "MIN_AGE_SECONDS", 0)
    fake = _FakeCloud({SOURCE: [_file("f1", "IPX-123.mp4")]}, fail_move=True)
    handler = _handler(transfer_repo, fake)

    for _ in range(4):
        await handler.handle(Transfer115Payload())

    state, attempts, _ = await _row(transfer_repo, "f1")
    assert (state, attempts) == (TransferItemState.FAILED, transfer115.MAX_ATTEMPTS)
    assert len(fake.listed) == 4, "达上限后只枚举源目录, 不再尝试搬移"


async def test_full_rerun_ignores_settled(transfer_repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transfer115, "MIN_AGE_SECONDS", 0)
    fake = _FakeCloud({SOURCE: [_file("f1", "IPX-123.mp4")]})
    handler = _handler(transfer_repo, fake)

    await handler.handle(Transfer115Payload())
    await handler.handle(Transfer115Payload())
    result = await handler.handle(Transfer115Payload(full=True))

    assert len(fake.moves) == 2, "full 忽略既往结论, 重扫一遍"
    assert result.result is not None and result.result.moved == 1


async def test_run_budget_caps_moves(transfer_repo: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    """一轮搬空会让任务长时间收不了尾, 也把限流一次撞满."""
    monkeypatch.setattr(transfer115, "MIN_AGE_SECONDS", 0)
    many = [_file(f"f{index}", f"IPX-{index:03d}.mp4") for index in range(MAX_MOVES_PER_RUN + 2)]
    fake = _FakeCloud({SOURCE: many})
    handler = _handler(transfer_repo, fake)

    await handler.handle(Transfer115Payload())
    result = await handler.handle(Transfer115Payload())

    assert result.result is not None
    assert (result.result.moved, result.result.skipped) == (MAX_MOVES_PER_RUN, 2)
    assert len(fake.moves) == MAX_MOVES_PER_RUN


async def test_source_list_failure_fails_the_run(transfer_repo: Repository) -> None:
    fake = _FakeCloud({}, fail_list=True)
    result = await _handler(transfer_repo, fake).handle(Transfer115Payload())

    assert result.success is False
    assert result.error is not None and "115 请求失败" in result.error
    assert fake.closed is True


async def test_empty_source_is_a_noop(transfer_repo: Repository) -> None:
    fake = _FakeCloud({SOURCE: []})
    result = await _handler(transfer_repo, fake).handle(Transfer115Payload())

    assert result.success is True
    assert result.result is not None
    assert (result.result.scanned, result.result.moved, result.result.failed) == (0, 0, 0)


async def test_single_run_cap_can_be_set_per_handler(
    transfer_repo: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单轮上限是部署侧的取舍: 网盘容易被限流时调小它, 一轮只搬指定条数."""
    monkeypatch.setattr(transfer115, "MIN_AGE_SECONDS", 0)
    fake = _FakeCloud({SOURCE: [_file(f"f{index}", f"IPX-{index:03d}.mp4") for index in range(3)]})
    handler = _handler(transfer_repo, fake, max_moves=1)

    await handler.handle(Transfer115Payload())
    result = await handler.handle(Transfer115Payload())

    assert result.result is not None
    assert (result.result.moved, result.result.skipped) == (1, 2)
    assert len(fake.moves) == 1


async def test_zero_single_run_cap_is_rejected(transfer_repo: Repository) -> None:
    """上限为 0 等于每轮空转: 那种配置只会让任务一直成功却什么都不搬."""
    hot = HotSettings(transfer=TransferConfig(enabled=True, cookies=COOKIE, source_cid=SOURCE, target_cid=TARGET))

    with pytest.raises(ValueError, match="max_moves"):
        Transfer115Handler(transfer_repo, hot, client_factory=_no_client, max_moves=0)


async def test_growing_file_waits_for_the_size_to_settle(
    transfer_repo: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成熟等待只看时间挡不住边下边搬: 体积还在长就重新计时."""
    monkeypatch.setattr(transfer115, "MIN_AGE_SECONDS", 0)
    fake = _FakeCloud({SOURCE: [_file("f1", "IPX-123.mp4", 1024)]})
    handler = _handler(transfer_repo, fake)

    await handler.handle(Transfer115Payload())
    fake.listing[SOURCE] = [_file("f1", "IPX-123.mp4", 4096)]
    grown = await handler.handle(Transfer115Payload())
    assert grown.result is not None
    assert (grown.result.moved, grown.result.waiting) == (0, 1)
    assert fake.moves == []

    settled = await handler.handle(Transfer115Payload())
    assert settled.result is not None
    assert (settled.result.moved, settled.result.waiting) == (1, 0)
    assert fake.moves == [("f1", TARGET)]


async def test_folder_waits_for_the_inner_size_to_settle(
    transfer_repo: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件夹自身的体积恒为 0, 要看夹内媒体文件的总体积有没有停."""
    monkeypatch.setattr(transfer115, "MIN_AGE_SECONDS", 0)
    fake = _FakeCloud({SOURCE: [_folder("c1", "IPX-123")], "c1": [_file("a1", "IPX-123.mp4", 1024)]})
    handler = _handler(transfer_repo, fake)

    await handler.handle(Transfer115Payload())
    first = await handler.handle(Transfer115Payload())
    assert first.result is not None and (first.result.moved, first.result.waiting) == (0, 1)

    fake.listing["c1"] = [_file("a1", "IPX-123.mp4", 1024), _file("a2", "IPX-124.mp4", 1024)]
    second = await handler.handle(Transfer115Payload())
    assert second.result is not None and (second.result.moved, second.result.waiting) == (0, 1)

    third = await handler.handle(Transfer115Payload())
    assert third.result is not None and (third.result.moved, third.result.waiting) == (1, 0)
    assert fake.moves == [("c1", TARGET)]
