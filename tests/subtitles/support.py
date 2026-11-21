"""字幕抓取用例的共享工具: 假通道与建库脚手架.

两个来源都不许真连: 页面与 JSON 全由用例给定, 下载写固定字节. 任何未登记的请求直接断言失败,
这样"意外多打了一次网"会在测试里立刻暴露, 而不是变成偶发超时.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from amane.db.models import MediaFileStatus
from amane.enums import MoveMode
from amane.net import FailureKind, RequestError, RequestFailure

if TYPE_CHECKING:
    from collections.abc import Mapping

    from amane.db.models import Library, MediaFile
    from amane.db.repository import Repository

SUBTITLE_BODY = b"1\n00:00:01,000 --> 00:00:02,000\n\xe4\xbd\xa0\xe5\xa5\xbd\n"


@dataclass
class FakeHttp:
    """按 URL 前缀路由的假出站通道."""

    texts: dict[str, str] = field(default_factory=dict)
    """URL 前缀 → 文本应答."""
    jsons: dict[str, object] = field(default_factory=dict)
    """URL 前缀 → JSON 应答."""
    payload: bytes = SUBTITLE_BODY
    """下载写出的字节."""
    download_ok: bool = True
    """False 模拟来源回了一个无法下载的地址."""
    transient_failures: dict[str, int] = field(default_factory=dict)
    """URL 前缀 → 还要以临时失败告终的次数."""
    calls: list[str] = field(default_factory=list)
    download_calls: list[tuple[str, int | None]] = field(default_factory=list)
    """下载过的地址与调用方给的字节上限."""

    def _match(self, table: Mapping[str, object], url: str) -> object:
        for prefix, value in table.items():
            if url.startswith(prefix):
                self._consume_failure(prefix, url)
                return value
        raise AssertionError(f"未登记的请求: {url}")

    def _consume_failure(self, prefix: str, url: str) -> None:
        remaining = self.transient_failures.get(prefix, 0)
        if remaining > 0:
            self.transient_failures[prefix] = remaining - 1
            raise RequestError(url, RequestFailure(kind=FailureKind.TIMEOUT, message="timeout"))

    async def get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        use_proxy: bool = True,
    ) -> str:
        self.calls.append(url)
        value = self._match(self.texts, url)
        assert isinstance(value, str)
        return value

    async def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        use_proxy: bool = True,
    ) -> object:
        self.calls.append(url)
        return self._match(self.jsons, url)

    async def download(
        self,
        url: str,
        dest: Path,
        *,
        use_proxy: bool = True,
        egress: object | None = None,
        max_bytes: int | None = None,
    ) -> bool:
        self.download_calls.append((url, max_bytes))
        if not self.download_ok:
            return False
        dest.write_bytes(self.payload)
        return True


async def seed_media(
    repo: Repository,
    root: Path,
    *,
    number: str,
    library: Library | None = None,
    suffix: str = ".mp4",
    subtitle: str | None = None,
    subtitle_extensions: list[str] | None = None,
    scraped: bool = True,
    with_metadata: bool = True,
    move_mode: MoveMode = MoveMode.MOVE,
) -> tuple[Library, MediaFile]:
    """建一条"成片 + 条目"的库行, 并可选地放一份已有字幕."""
    root.mkdir(parents=True, exist_ok=True)
    if library is None:
        library = await repo.create_library(
            name=f"lib-{number}",
            path=str(root),
            subtitle_extensions=[".srt", ".ass"] if subtitle_extensions is None else subtitle_extensions,
            write_nfo=False,
            move_mode=move_mode,
        )
    video = root / f"{number}{suffix}"
    video.write_bytes(b"not a real video")
    if subtitle is not None:
        (root / subtitle).write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n")

    metadata_id: int | None = None
    if with_metadata:
        await repo.upsert_metadata(number=number, title=f"{number} 标题")
        metadata = await repo.get_metadata_by_number(number)
        assert metadata is not None
        metadata_id = metadata.id
    media = await repo.create_media_file(
        library.id,
        path=str(video),
        number=number,
        metadata_id=metadata_id,
        status=MediaFileStatus.SCRAPED if scraped else MediaFileStatus.PENDING,
    )
    return library, media
