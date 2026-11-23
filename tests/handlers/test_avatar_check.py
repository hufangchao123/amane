"""头像校验任务: 首图可用即算通过, 坏链接按开关摘掉, 内网与瞬时故障只报不判."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from amane.avatars import MAX_IMAGE_BYTES, WebAvatarSource, looks_like_image, verify_location
from amane.db.repo_types import WriteMode
from amane.handlers.avatar_check import AvatarCheckHandler
from amane.handlers.models import AvatarCheckPayload
from amane.net.egress import EgressPolicy
from amane.net.errors import EgressRejected, FailureKind, RequestError, RequestFailure

if TYPE_CHECKING:
    from amane.db.repository import Repository


class _OpenPolicy(EgressPolicy):
    """测试不解析 DNS: 出站判定一律放行."""

    async def ensure_allowed(self, url: str) -> None:
        return None


# 最小 PNG 头: 只验文件头判定, 不解码像素
PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


class _FakeSource:
    """按 URL 决定下载结果与状态码; 记录顺序, 便于断言只查首图."""

    def __init__(self, *, payloads: dict[str, bytes] | None = None, statuses: dict[str, int] | None = None) -> None:
        self.payloads = payloads or {}
        self.statuses = statuses or {}
        self.downloads: list[str] = []
        self.statuses_asked: list[str] = []

    async def download(self, url: str, dest: Path, *, max_bytes: int) -> bool:
        self.downloads.append(url)
        body = self.payloads.get(url)
        if body is None:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return True

    async def status(self, url: str) -> int | None:
        self.statuses_asked.append(url)
        return self.statuses.get(url)


class _ExplodingSource(_FakeSource):
    """一个地址直接抛异常: 单个演员的意外不该报废整轮."""

    def __init__(self, *, boom: str) -> None:
        super().__init__()
        self.boom = boom

    async def download(self, url: str, dest: Path, *, max_bytes: int) -> bool:
        if url == self.boom:
            raise RuntimeError("磁盘满了")
        return await super().download(url, dest, max_bytes=max_bytes)


async def _seed(repo: Repository, number: str, name: str, urls: list[str]) -> int:
    await repo.upsert_metadata(number=number, title=f"t-{number}", actors=[name])
    (actor,) = await repo.get_actors_by_names([name])
    actor_id = actor.id
    assert actor_id is not None
    await repo.update_actor(actor_id, image_urls=urls, mode=WriteMode.AUTO)
    return actor_id


def _handler(repo: Repository, source: _FakeSource, tmp_path: Path) -> AvatarCheckHandler:
    return AvatarCheckHandler(repo, source, state_dir=tmp_path, egress=_OpenPolicy())


def test_looks_like_image_cases() -> None:
    assert looks_like_image(PNG_HEAD)
    assert looks_like_image(b"\xff\xd8\xff\xe0" + b"\x00" * 8)
    # avif / heic 的 ftyp 盒子不在开头, 少认这一种会把有效链接判成坏图.
    assert looks_like_image(b"\x00\x00\x00\x20ftypavif" + b"\x00" * 8)
    assert looks_like_image(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
    assert not looks_like_image(b"<html>not found</html>")


@pytest.mark.asyncio(loop_scope="function")
async def test_verify_location_verdicts(tmp_path: Path) -> None:
    source = _FakeSource(
        payloads={"https://img.test/ok.png": PNG_HEAD, "https://img.test/html.png": b"<html>404</html>"},
        statuses={"https://img.test/gone.png": 404, "https://img.test/busy.png": 503},
    )
    assert (await verify_location(source, "https://img.test/ok.png", tmp_path / "a.bin", egress=_OpenPolicy()))[
        0
    ] == "ok"
    assert (await verify_location(source, "https://img.test/html.png", tmp_path / "b.bin", egress=_OpenPolicy()))[
        0
    ] == "broken"
    assert (await verify_location(source, "https://img.test/gone.png", tmp_path / "c.bin", egress=_OpenPolicy()))[
        0
    ] == "broken"
    # 5xx 与「连不上」都可能是抖动: 判破就要清链接, 而清链接不可逆
    assert (await verify_location(source, "https://img.test/busy.png", tmp_path / "d.bin", egress=_OpenPolicy()))[
        0
    ] == "unknown"
    assert (await verify_location(source, "https://img.test/none.png", tmp_path / "e.bin", egress=_OpenPolicy()))[
        0
    ] == "unknown"
    # 下载产物要清掉
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio(loop_scope="function")
async def test_verify_location_skips_internal_address(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _reject(self: EgressPolicy, url: str) -> None:
        raise EgressRejected(url, "地址指向内网", detail="不支持内网地址")

    monkeypatch.setattr(EgressPolicy, "ensure_allowed", _reject)
    source = _FakeSource()
    verdict, detail = await verify_location(source, "http://192.168.1.9/a.png", tmp_path / "a.bin")
    assert verdict == "unknown"
    assert "内网" in detail
    assert source.downloads == []


@pytest.mark.asyncio(loop_scope="function")
async def test_verify_location_treats_resolve_failure_as_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """域名解析失败过去被判坏链接: 一次 DNS 抖动就会清掉有效地址."""

    async def _unresolved(self: EgressPolicy, url: str) -> None:
        raise RequestError(url, RequestFailure(kind=FailureKind.CURL, message="主机解析失败"))

    monkeypatch.setattr(EgressPolicy, "ensure_allowed", _unresolved)
    source = _FakeSource()
    verdict, detail = await verify_location(source, "https://img.test/a.png", tmp_path / "a.bin")
    assert verdict == "unknown"
    assert "未判定" in detail
    assert source.downloads == []


@pytest.mark.asyncio(loop_scope="function")
async def test_handler_counts_and_clears(repo: Repository, tmp_path: Path) -> None:
    good = await _seed(repo, "AAA-001", "好演员", ["https://img.test/good.png", "https://img.test/second.png"])
    bad = await _seed(repo, "AAA-002", "坏演员", ["https://img.test/bad.png"])
    await _seed(repo, "AAA-003", "无图演员", [])
    source = _FakeSource(payloads={"https://img.test/good.png": PNG_HEAD}, statuses={"https://img.test/bad.png": 404})
    result = await _handler(repo, source, tmp_path).handle(AvatarCheckPayload(limit=10, clear_broken=True))
    assert result.success is True
    assert result.result is not None
    assert (result.result.checked, result.result.ok, result.result.broken, result.result.cleared) == (2, 1, 1, 1)
    assert source.downloads == ["https://img.test/good.png", "https://img.test/bad.png"]
    remaining = await repo.get_actor(bad)
    assert remaining is not None
    assert remaining.image_urls == []
    kept = await repo.get_actor(good)
    assert kept is not None
    assert kept.image_urls == ["https://img.test/good.png", "https://img.test/second.png"]


@pytest.mark.asyncio(loop_scope="function")
async def test_clearing_does_not_lock_the_field(repo: Repository, tmp_path: Path) -> None:
    """清坏图是运维动作, 不该把 ``image_urls`` 锁进手动字段, 否则刮削不再维护头像."""
    actor_id = await _seed(repo, "DDD-001", "锁测试", ["https://img.test/gone.png"])
    source = _FakeSource(statuses={"https://img.test/gone.png": 404})
    await _handler(repo, source, tmp_path).handle(AvatarCheckPayload(limit=1, clear_broken=True))
    actor = await repo.get_actor(actor_id)
    assert actor is not None
    assert actor.image_urls == []
    assert not actor.locked_fields
    await repo.update_actor(actor_id, image_urls=["https://img.test/fresh.png"], mode=WriteMode.AUTO)
    refreshed = await repo.get_actor(actor_id)
    assert refreshed is not None
    assert refreshed.image_urls == ["https://img.test/fresh.png"]


@pytest.mark.asyncio(loop_scope="function")
async def test_handler_respects_limit(repo: Repository, tmp_path: Path) -> None:
    for index in range(3):
        await _seed(repo, f"BBB-00{index}", f"演员{index}", [f"https://img.test/{index}.png"])
    source = _FakeSource()
    result = await _handler(repo, source, tmp_path).handle(AvatarCheckPayload(limit=2))
    assert result.result is not None
    assert result.result.checked == 2
    assert len(source.downloads) == 2
    assert result.result.cleared == 0


@pytest.mark.asyncio(loop_scope="function")
async def test_single_actor_failure_keeps_the_run(repo: Repository, tmp_path: Path) -> None:
    await _seed(repo, "EEE-001", "炸掉的", ["https://img.test/boom.png"])
    await _seed(repo, "EEE-002", "正常的", ["https://img.test/ok.png"])
    source = _ExplodingSource(boom="https://img.test/boom.png")
    source.payloads["https://img.test/ok.png"] = PNG_HEAD
    result = await _handler(repo, source, tmp_path).handle(AvatarCheckPayload(limit=10))
    assert result.result is not None
    assert result.result.failed == 1
    assert result.result.ok == 1
    assert result.success is False


@pytest.mark.asyncio(loop_scope="function")
async def test_scratch_directory_is_removed(repo: Repository, tmp_path: Path) -> None:
    await _seed(repo, "FFF-001", "临时目录", ["https://img.test/ok.png"])
    source = _FakeSource(payloads={"https://img.test/ok.png": PNG_HEAD})
    await _handler(repo, source, tmp_path).handle(AvatarCheckPayload(limit=1))
    assert not (tmp_path / "avatar-check").exists()


@pytest.mark.asyncio(loop_scope="function")
async def test_download_cap_is_bounded(repo: Repository, tmp_path: Path) -> None:
    """校验用的下载必须带上限, 免得一个巨型文件占满磁盘."""
    seen: list[int] = []

    class _Recorder(_FakeSource):
        async def download(self, url: str, dest: Path, *, max_bytes: int) -> bool:
            seen.append(max_bytes)
            dest.write_bytes(PNG_HEAD)
            return True

    await _seed(repo, "CCC-001", "上限演员", ["https://img.test/cap.png"])
    await _handler(repo, _Recorder(), tmp_path).handle(AvatarCheckPayload(limit=1))
    assert seen == [MAX_IMAGE_BYTES]


def test_web_source_is_exported() -> None:
    """适配器要能拿到, 否则运行时只能注入替身."""
    assert WebAvatarSource is not None
