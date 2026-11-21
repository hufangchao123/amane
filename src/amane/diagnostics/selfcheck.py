"""只读体检: 把「当前配置能否正常工作」的判据逐项列出.

三项约束: 不写库、不修改文件、不发网络请求. 因此可以在请求内直接运行, 最慢的探测也只是
``stat`` 与磁盘容量查询, 且都在线程池内执行.

判据只覆盖「当前配置下一定出问题」的项. 未启用的功能一律通过: 关闭的开关不应使体检失败.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ..backup import BackupManager
from ..db import TaskStatus
from ..utils.threads import in_thread

if TYPE_CHECKING:
    from ..config.manager import ColdSettings, HotSettings
    from ..db.repository import Repository

DISK_MIN_FREE_BYTES = 1 << 30
"""剩余空间阈值: 低于该值时整理、超分、备份都可能中断, 提前报出."""

QUEUE_BACKLOG_LIMIT = 500
"""队列积压上限: 超过该值通常表示 worker 无响应或某任务反复重试."""

BACKUP_MAX_AGE_DAYS = 7


class CheckItem(BaseModel):
    key: str
    ok: bool
    detail: str = ""


class SelfCheckReport(BaseModel):
    items: list[CheckItem] = []

    @property
    def failed(self) -> list[CheckItem]:
        return [item for item in self.items if not item.ok]


@dataclass
class _FsFacts:
    """一次线程池往返取得全部文件系统事实, 避免为每项探测单独切换线程."""

    data_dir_ok: bool
    free_bytes: int
    library_count: int = 0
    missing_libraries: list[str] = field(default_factory=list)
    missing_safe_dirs: list[str] = field(default_factory=list)


@in_thread
def _probe_filesystem(data_dir: Path, library_paths: list[tuple[str, str]], safe_dirs: list[Path]) -> _FsFacts:
    data_dir_ok = data_dir.is_dir()
    try:
        free_bytes = shutil.disk_usage(data_dir if data_dir_ok else data_dir.parent).free
    except OSError:
        free_bytes = -1
    return _FsFacts(
        data_dir_ok=data_dir_ok,
        free_bytes=free_bytes,
        library_count=len(library_paths),
        missing_libraries=[f"{name}: {path}" for name, path in library_paths if not Path(path).is_dir()],
        missing_safe_dirs=[str(path) for path in safe_dirs if not path.is_dir()],
    )


class SelfCheck:
    """只读体检: 每项探测不写库、不修改文件, 因此随时可在请求内运行."""

    def __init__(self, cold: ColdSettings, hot: HotSettings, repo: Repository) -> None:
        self._cold = cold
        self._hot = hot
        self._repo = repo

    async def run(self) -> SelfCheckReport:
        libraries = await self._repo.list_libraries()
        queued = await self._repo.count_tasks(statuses=[TaskStatus.QUEUED])
        running = await self._repo.count_tasks(statuses=[TaskStatus.RUNNING])
        media_files = await self._repo.count_media_files()

        safe_dirs = self._cold.safe_dirs if isinstance(self._cold.safe_dirs, list) else []
        facts = await _probe_filesystem(
            Path(self._cold.data_dir),
            [(library.name, library.path) for library in libraries],
            safe_dirs,
        )

        return SelfCheckReport(
            items=[
                self._data_dir_item(facts),
                self._disk_item(facts),
                self._db_item(media_files, queued, running),
                self._libraries_item(facts),
                self._safe_dirs_item(facts),
                self._token_item(),
                self._transfer_item(),
                self._subtitles_item(),
                self._wash_item(),
                await self._backup_item(),
                self._queue_item(queued),
            ]
        )

    def _data_dir_item(self, facts: _FsFacts) -> CheckItem:
        detail = "" if facts.data_dir_ok else f"不存在: {self._cold.data_dir}"
        return CheckItem(key="data_dir", ok=facts.data_dir_ok, detail=detail)

    def _disk_item(self, facts: _FsFacts) -> CheckItem:
        if facts.free_bytes < 0:
            return CheckItem(key="disk", ok=False, detail="无法读取磁盘容量")
        gigibytes = facts.free_bytes / (1 << 30)
        enough = facts.free_bytes >= DISK_MIN_FREE_BYTES
        detail = f"剩余 {gigibytes:.1f} GiB"
        if not enough:
            detail += ", 低于 1 GiB"
        return CheckItem(key="disk", ok=enough, detail=detail)

    def _db_item(self, media_files: int, queued: int, running: int) -> CheckItem:
        return CheckItem(key="db", ok=True, detail=f"媒体文件 {media_files} 份, {queued} 条待执行, {running} 条执行中")

    def _libraries_item(self, facts: _FsFacts) -> CheckItem:
        if facts.library_count == 0:
            return CheckItem(key="libraries", ok=True, detail="尚未建库")
        if facts.missing_libraries:
            return CheckItem(key="libraries", ok=False, detail="路径不可用: " + "; ".join(facts.missing_libraries))
        return CheckItem(key="libraries", ok=True, detail=f"{facts.library_count} 个库路径均可用")

    def _safe_dirs_item(self, facts: _FsFacts) -> CheckItem:
        if facts.missing_safe_dirs:
            return CheckItem(key="safe_dirs", ok=False, detail="不存在: " + "; ".join(facts.missing_safe_dirs))
        return CheckItem(key="safe_dirs", ok=True)

    def _token_item(self) -> CheckItem:
        """``off`` 是显式关闭鉴权, 只在该取值下报告; 空值表示自动生成, 属于正常状态."""
        if self._cold.token == "off":
            return CheckItem(key="token", ok=False, detail="接口鉴权已关闭 (AMANE_TOKEN=off), 不应暴露到公网")
        return CheckItem(key="token", ok=True, detail="接口鉴权已启用")

    def _transfer_item(self) -> CheckItem:
        config = self._hot.transfer
        if not config.enabled:
            return CheckItem(key="transfer", ok=True, detail="未启用")
        missing = [
            name
            for name, value in (
                ("cookies", config.cookies),
                ("source_dir", config.source_dir),
                ("target_dir", config.target_dir),
            )
            if not value
        ]
        if missing:
            return CheckItem(key="transfer", ok=False, detail="缺少配置项: " + ", ".join(missing))
        return CheckItem(key="transfer", ok=True, detail="配置完整")

    def _subtitles_item(self) -> CheckItem:
        config = self._hot.subtitles
        if not config.enabled:
            return CheckItem(key="subtitles", ok=True, detail="未启用")
        if "assrt" in config.providers and not config.assrt_token:
            return CheckItem(key="subtitles", ok=False, detail="来源含 assrt 但未填写 token")
        return CheckItem(key="subtitles", ok=True, detail="配置完整")

    def _wash_item(self) -> CheckItem:
        config = self._hot.wash
        if not config.enabled:
            return CheckItem(key="wash", ok=True, detail="未启用")
        if not config.local and not config.strm:
            return CheckItem(key="wash", ok=False, detail="已启用但 local 与 strm 均关闭, 无法产生候选")
        return CheckItem(key="wash", ok=True, detail="配置完整")

    async def _backup_item(self) -> CheckItem:
        """启用备份却长期没有产物, 等于没有备份; 此处只报告, 不补执行."""
        config = self._hot.backup
        if not config.enabled:
            return CheckItem(key="backup", ok=True, detail="未启用")
        manager = BackupManager(config, Path(self._cold.data_dir))
        entries = [entry for entry in await manager.list_backups() if entry.kind == "db"]
        if not entries:
            return CheckItem(key="backup", ok=False, detail=f"尚无数据库备份, 目标目录 {manager.root}")
        age_days = (datetime.now(UTC) - max(entry.created_at for entry in entries)).days
        detail = f"最近一份数据库备份在 {age_days} 天前"
        if age_days > BACKUP_MAX_AGE_DAYS:
            return CheckItem(key="backup", ok=False, detail=detail + f", 已超过 {BACKUP_MAX_AGE_DAYS} 天")
        return CheckItem(key="backup", ok=True, detail=detail)

    def _queue_item(self, queued: int) -> CheckItem:
        ok = queued <= QUEUE_BACKLOG_LIMIT
        detail = f"{queued} 条排队"
        if not ok:
            detail += f", 超过上限 {QUEUE_BACKLOG_LIMIT}, 检查 worker 是否无响应"
        return CheckItem(key="queue", ok=ok, detail=detail)
