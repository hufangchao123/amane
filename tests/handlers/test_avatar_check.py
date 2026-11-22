"""头像校验任务: 首图可用即算通过, 坏链接按开关摘掉, 内网地址只报不判."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from amane.avatars import MAX_IMAGE_BYTES, looks_like_image, verify_location
from amane.avatars.audit import AvatarHttp
from amane.config import HotSettings
from amane.db.repo_types import WriteMode
from amane.handlers.avatar_check import AvatarCheckHandler
from amane.handlers.models import AvatarCheckPayload
from amane.net.egress import EgressPolicy
from amane.net.errors import EgressRejected

if TYPE_CHECKING:
    from amane.db.repository import Repository


class _OpenPolicy(EgressPolicy):
    """测试不解析 DNS: 出站判定一律放行."""

    async def ensure_allowed(self, url: str) -> None:
        return None


# 最小 PNG 头: 只验文件头判定, 不解码像素
PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


class _FakeWeb:
    """按 URL 决定下载结果; 记录每次调用, 便于断言只查首图."""

    def __init__(self, *, payloads: dict[str, bytes | None]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    async def download(
        self,
        url: str,
        dest: Path,
        *,
        use_proxy: bool = True,
        egress: EgressPolicy | None = None,
        max_bytes: int | None = None,
    ) -> bool:
        self.calls.append(url)
        body = self.payloads.get(url)
        if body is None:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return True


async def _seed(repo: Repository, number: str, name: str, urls: list[str]) -> int:
    await repo.upsert_metadata(number=number, title=f"t-{number}", actors=[name])
    (actor,) = await repo.get_actors_by_names([name])
    actor_id = actor.id
    assert actor_id is not None
    await repo.update_actor(actor_id, image_urls=urls, mode=WriteMode.MANUAL)
    return actor_id


def _handler(repo: Repository, web: AvatarHttp, tmp_path: Path) -> AvatarCheckHandler:
    return AvatarCheckHandler(repo, HotSettings(), web, state_dir=tmp_path, egress=_OpenPolicy())


def test_looks_like_image_cases() -> None:
    assert looks_like_image(PNG_HEAD)
    assert looks_like_image(b"\xff\xd8\xff\xe0" + b"\x00" * 8)
    assert not looks_like_image(b"<html>not found</html>")


@pytest.mark.asyncio(loop_scope="function")
async def test_verify_location_rejects_non_image(repo: Repository, tmp_path: Path) -> None:
    web = _FakeWeb(payloads={"https://img.test/a.png": b"<html>404</html>", "https://img.test/ok.png": PNG_HEAD})
    assert (await verify_location(web, "https://img.test/ok.png", tmp_path / "a.bin", egress=_OpenPolicy()))[0] == "ok"
    assert (await verify_location(web, "https://img.test/a.png", tmp_path / "b.bin", egress=_OpenPolicy()))[
        0
    ] == "broken"
    assert (await verify_location(web, "https://img.test/missing.png", tmp_path / "c.bin", egress=_OpenPolicy()))[
        0
    ] == "broken"
    # 下载产物要清掉: 临时目录里只剩数据库与样本文件
    assert sorted(item.name for item in tmp_path.iterdir()) == ["amane.db"]


@pytest.mark.asyncio(loop_scope="function")
async def test_verify_location_skips_internal_address(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _reject(self: EgressPolicy, url: str) -> None:
        raise EgressRejected(url, "地址指向内网", detail="不支持内网地址")

    monkeypatch.setattr(EgressPolicy, "ensure_allowed", _reject)
    web = _FakeWeb(payloads={})
    verdict, detail = await verify_location(web, "http://192.168.1.9/a.png", tmp_path / "a.bin")
    assert verdict == "unknown"
    assert "内网" in detail
    assert web.calls == []


@pytest.mark.asyncio(loop_scope="function")
async def test_handler_counts_and_clears(repo: Repository, tmp_path: Path) -> None:
    good = await _seed(repo, "AAA-001", "好演员", ["https://img.test/good.png", "https://img.test/second.png"])
    bad = await _seed(repo, "AAA-002", "坏演员", ["https://img.test/bad.png"])
    await _seed(repo, "AAA-003", "无图演员", [])
    web = _FakeWeb(payloads={"https://img.test/good.png": PNG_HEAD})
    result = await _handler(repo, web, tmp_path).handle(AvatarCheckPayload(limit=10, clear_broken=True))
    assert result.success is True
    assert result.result is not None
    assert (result.result.checked, result.result.ok, result.result.broken, result.result.cleared) == (2, 1, 1, 1)
    assert web.calls == ["https://img.test/good.png", "https://img.test/bad.png"]
    remaining = await repo.get_actor(bad)
    assert remaining is not None
    assert remaining.image_urls == []
    kept = await repo.get_actor(good)
    assert kept is not None
    assert kept.image_urls == ["https://img.test/good.png", "https://img.test/second.png"]


@pytest.mark.asyncio(loop_scope="function")
async def test_handler_respects_limit(repo: Repository, tmp_path: Path) -> None:
    for index in range(3):
        await _seed(repo, f"BBB-00{index}", f"演员{index}", [f"https://img.test/{index}.png"])
    web = _FakeWeb(payloads={})
    result = await _handler(repo, web, tmp_path).handle(AvatarCheckPayload(limit=2))
    assert result.result is not None
    assert result.result.checked == 2
    assert len(web.calls) == 2
    assert result.result.cleared == 0


@pytest.mark.asyncio(loop_scope="function")
async def test_download_cap_is_bounded(repo: Repository, tmp_path: Path) -> None:
    """校验用的下载必须带上限, 免得一个巨型文件占满磁盘."""
    seen: list[int | None] = []

    class _Recorder:
        async def download(self, url: str, dest: Path, *, use_proxy: bool = True, egress=None, max_bytes=None) -> bool:
            seen.append(max_bytes)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(_PNG_FILE(tmp_path), dest)
            return True

    await _seed(repo, "CCC-001", "上限演员", ["https://img.test/cap.png"])
    await _handler(repo, _Recorder(), tmp_path).handle(AvatarCheckPayload(limit=1))
    assert seen == [MAX_IMAGE_BYTES]


def _PNG_FILE(tmp_path: Path) -> Path:
    path = tmp_path / "sample.png"
    if not path.exists():
        path.write_bytes(PNG_HEAD)
    return path
