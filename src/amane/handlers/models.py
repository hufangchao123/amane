from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field
from starlette.status import HTTP_403_FORBIDDEN

from ..db import Library, MediaFileStatus, Repository
from ..db.embeddings import EmbeddingKind
from ..db.models import TaskType
from ..enums import DownloadableResource
from ..observability.models import OrganizeConflict, SiteOutcomeRecord
from ..parsing import ContentType, infer_content_type
from ..utils.path import is_descendant, is_resolved_path, resolved_path

if TYPE_CHECKING:
    from ..db.models import Feed


class LibraryBase(BaseModel):
    """library_id + path. 覆盖项在 resolve 之前为 None, 不能当最终值读取."""

    library_id: int = Field(
        description="所属 Library ID; 扫描/整理在该媒体库下进行", json_schema_extra={"x-widget": "LibraryPicker"}
    )
    path: str = Field(
        default="",
        description="要扫描的目录路径 (覆盖 Library 路径, 必须为 Library 子目录).",
        json_schema_extra={"x-widget": "PathPicker", "x-path-type": "directory"},
    )

    async def resolve(self, repo: Repository) -> None:
        """就地写回 Library 默认值与覆盖; path 非库子目录时 403."""
        lib = await repo.get_library(self.library_id)
        if lib is None:
            raise HTTPException(status_code=404, detail=f"媒体库 {self.library_id} 不存在")
        if self.path and not is_descendant(self.path, lib.path):
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN,
                detail=f"路径 {self.path} 不在媒体库路径 {lib.path} 之下",
            )
        # 范围路径与库路径必须同一形式 (文件选择器给的是真实路径): 索引与清理清单都按字面路径比较,
        # 两种写法会把同一个文件算成两条索引. 库路径尚未解析为真实路径的旧库保持原样, 否则范围会与既有索引分家.
        if self.path and is_resolved_path(lib.path):
            self.path = str(resolved_path(self.path))
        self.path = self.path or lib.path
        self._apply_library(lib)

    def _apply_library(self, lib: Library) -> None:
        return


class LibraryScanBase(LibraryBase):
    """REFRESH / SCAN_INVALID 扫描范围: 可覆盖库的 recursive / patterns."""

    recursive: bool | None = Field(default=None, description="覆盖 Library 的 recursive; None 沿用库设置")
    patterns: list[str] | None = Field(default=None, description="覆盖 Library 的 patterns; None 沿用库设置")

    def _apply_library(self, lib: Library) -> None:
        self.recursive = self.recursive if self.recursive is not None else lib.recursive
        self.patterns = self.patterns or lib.patterns


# --- SCAN ---


class ScanMode(StrEnum):
    add = "add"
    """扫描并注册新文件; 已存在的文件不变."""
    remove = "remove"
    """检查并删除文件失效的 MediaFile 记录."""


class CacheKind(StrEnum):
    """刮削可复用的缓存种类. use_cache 为其集合: 含某项 = 该缓存生效, 不含 = 强制刷新该项."""

    metadata = "metadata"
    """元数据缓存: 复用 DB 中既有的 per-site raw 快照, 仅补缺失/失败站点. 不含则全站强制重爬."""
    trans = "trans"
    """译文缓存: 命中则跳过 LLM 调用. 不含则强制重译 (并刷新缓存)."""


class RefreshPayload(LibraryScanBase):
    scan: set[ScanMode] = {ScanMode.add}
    """空集 = 不扫描."""
    scrape: set[MediaFileStatus] = {MediaFileStatus.PENDING}
    use_cache: set[CacheKind] = {CacheKind.metadata, CacheKind.trans}
    """空集 = 全部强制刷新. 原样转发给派生的 SCRAPE."""


class RefreshResult(BaseModel):
    type: Literal[TaskType.REFRESH] = TaskType.REFRESH
    added: int
    removed: int
    scrape: int


# --- SCRAPE ---


class ScrapePayload(BaseModel):
    number: str
    content_type: ContentType = ContentType.CENSORED
    media_file_id: int | None = None
    use_cache: set[CacheKind] = {CacheKind.metadata, CacheKind.trans}


def build_feed_scrape_payload(feed: Feed, number: str) -> ScrapePayload:
    content_type = feed.content_type or infer_content_type(number)

    use_cache: set[CacheKind] = set()
    for raw_kind in feed.use_cache:
        try:
            use_cache.add(CacheKind(raw_kind))
        except TypeError, ValueError:
            continue

    return ScrapePayload(
        number=number,
        content_type=content_type,
        media_file_id=None,
        use_cache=use_cache,
    )


class ScrapeResult(BaseModel):
    type: Literal[TaskType.SCRAPE] = TaskType.SCRAPE
    metadata_id: int | None = None
    """刮削失败时没有元数据行, 为 None."""
    field_sources: dict[str, str]
    failed_sites: list[str]
    outcomes: list[SiteOutcomeRecord] = Field(default_factory=list)
    """各来源的结果明细; 与记录里的 summary 同源."""


# --- ORGANIZE ---


class OrganizePayload(LibraryBase):
    """write_nfo / copy_resources 为 None 时沿用 Library 设置.

    范围三选一: 缺省 (resolve 后 path 为库根) 处理该库全部索引; 显式 path 按前缀过滤;
    `media_file_ids` 为勾选快照 (空列表空跑). 显式 path 与 `media_file_ids` 不能同时给出.
    不含 `recursive` / `patterns`.
    """

    write_nfo: bool | None = Field(default=None, description="覆盖 Library.write_nfo; None 沿用库设置")
    copy_resources: list[DownloadableResource] | None = Field(
        default=None, description="覆盖 Library.copy_resources; None 沿用库设置"
    )
    media_file_ids: list[int] | None = Field(
        default=None,
        description="勾选快照; 与 path 不能同时指定. None 表示 path 范围内的全部索引",
    )
    prune_empty_dirs: bool = Field(
        default=True,
        description="移动后删除本次腾空的目录 (库根与 .amane_trash 除外); 复制 / 硬链接 / 符号链接方式不移走源文件, 该开关无效",
    )

    async def resolve(self, repo: Repository) -> None:
        if self.media_file_ids is not None and self.path:
            raise HTTPException(status_code=422, detail="media_file_ids 与 path 不能同时指定")
        if self.media_file_ids:
            found = await repo.list_media_files(ids=self.media_file_ids, limit=None)
            if any(mf.library_id != self.library_id for mf in found):
                raise HTTPException(status_code=422, detail="media_file_ids 含其它库的文件")
        await super().resolve(repo)

    def _apply_library(self, lib: Library) -> None:
        if self.write_nfo is None:
            self.write_nfo = lib.write_nfo
        if self.copy_resources is None:
            # JSON 列读回是 str; Pydantic dump 要 enum, 否则 UnexpectedValue 警告.
            self.copy_resources = [DownloadableResource(r) for r in lib.copy_resources]


ORGANIZE_CONFLICT_LIMIT = 200
"""conflicts 的条数上限; 超出只累加 conflicted, 不写进任务结果."""


class OrganizeResult(BaseModel):
    type: Literal[TaskType.ORGANIZE] = TaskType.ORGANIZE
    organized: int
    skipped: int
    conflicted: int
    failed: int
    pruned_dirs: int
    conflicts: list[OrganizeConflict]
    """未处理的条目, 最多 ORGANIZE_CONFLICT_LIMIT 条; 恒为 conflicted 条目的前缀, 差额只留在任务日志里."""


# --- SCAN INVALID ---


class ScanInvalidPayload(LibraryScanBase):
    """只读遍历 path 范围内的无效文件与空目录, 产出清单. path 缺省为库根."""


class ScanInvalidResult(BaseModel):
    type: Literal[TaskType.SCAN_INVALID] = TaskType.SCAN_INVALID
    inventory_id: str
    entries: int
    dirs: int
    scope_path: str | None = None
    truncated: bool = False
    skipped_dirs: int = 0
    skipped_files: int = 0
    blocked_dirs: int = 0
    """因子树里有无法识别的文件而未登记的候选目录数; 与「读不到」的 skipped 分开计."""


# --- DELETE ---


class DeletePayload(BaseModel):
    """按清单标识删除; 执行集合取自清单条目 — 命中排除项的不删除, 命中更深的纳入项的仍删除."""

    library_id: int = Field(description="清单所属 Library ID")
    inventory_id: str = Field(description="后端生成的清单标识; 不存在或已过期则失败")
    exclude: list[str] = Field(
        default_factory=list,
        description="排除项: 库内为清单库根下的相对路径, 库外为绝对路径; 按路径分量匹配",
    )
    include: list[str] = Field(
        default_factory=list,
        description=(
            "在排除项内重新纳入的路径 (路径约定同 exclude); 与排除项互为祖先时按最深的一条判定, "
            "同一路径同时命中两组时按纳入处理"
        ),
    )
    prune_empty_dirs: bool = Field(default=True, description="删除本次腾空的目录 (库根与 .amane_trash 除外)")


class DeleteResult(BaseModel):
    type: Literal[TaskType.DELETE] = TaskType.DELETE
    deleted: int
    changed: int
    failed: int
    freed_bytes: int = 0
    """按 inode 归并后的释放空间; 硬链接名字不齐时不计入."""
    hardlink_items: int = 0
    """已删除但不释放空间的硬链接项数."""
    pruned_dirs: int = 0
    excluded: int = 0
    """因排除项而不删除、且未被纳入项恢复的清单条目数."""
    indexed: int = 0
    """随之删除的 MediaFile 行数."""
    reverify_rejected: int = 0
    """执行前复验未通过的条目数; 拒绝原因见 `delete.py::_reverify` (目录里出现媒体 / 未识别的
    文件 / 下载进度, 目录不再为空或无法读取), 这些条目同时计入 `failed`."""


# --- CLEANUP ---


class CleanupPayload(BaseModel):
    remove_missing_files: bool = Field(default=True, description="删除磁盘上不存在的 MediaFile 记录 (不触碰 Metadata)")
    remove_unreferenced_resources: bool = Field(
        default=True, description="删除不被任何 Metadata URL 字段引用的 Resource (含派生裁剪)"
    )


class CleanupResult(BaseModel):
    type: Literal[TaskType.CLEANUP] = TaskType.CLEANUP
    files_removed: int
    resources_removed: int


# --- UPSCALE ---


class UpscalePayload(BaseModel):
    max_dim_threshold: int | None = None
    """覆盖 sr.max_dim_threshold; None 沿用配置."""
    max_bytes_threshold: int | None = None
    """覆盖 sr.max_bytes_threshold; None 沿用配置."""
    limit: int = 200
    """单次最多处理的资源数; 超出则本批结束, 避免长时间占用 worker."""


class UpscaleResult(BaseModel):
    type: Literal[TaskType.UPSCALE] = TaskType.UPSCALE
    scanned: int
    upscaled: int
    skipped: int
    failed: int


# --- SEMANTIC_INDEX ---


class SemanticIndexPayload(BaseModel):
    limit: int | None = Field(default=None, ge=1)
    """单次最多处理的条目数; None 表示全库."""
    force: bool = False
    """True 时忽略 (resource_url, content_hash) 未变的跳过判据, 全部重算."""
    kinds: list[EmbeddingKind] = Field(default_factory=lambda: list(EmbeddingKind), min_length=1)
    """要建立向量的图片来源."""


class SemanticIndexResult(BaseModel):
    type: Literal[TaskType.SEMANTIC_INDEX] = TaskType.SEMANTIC_INDEX
    scanned: int
    indexed: int
    skipped: int
    missing: int
    failed: int


class SemanticBenchmarkPayload(BaseModel):
    """留空: 样本与轮次固定, 由服务决定."""


class SemanticBenchmarkResult(BaseModel):
    type: Literal[TaskType.SEMANTIC_BENCHMARK] = TaskType.SEMANTIC_BENCHMARK
    backend: str
    """推理后端展示名."""
    model: str
    """模型展示名."""
    images_per_second: float
    """端到端编码速度 (解码 + 推理)."""
    query_ms: float
    """单次查询的文本编码耗时中位数."""
    load_ms: float
    """本次测试冷加载两塔的耗时; 测试前已加载则为 0."""
    samples: int
    """参与测量的图片数."""
    measured_at: str
    """测量完成的本地时间 (ISO 8601)."""


# --- R18 IMPORT ---


class R18ImportPayload(BaseModel):
    force: bool = False
    """忽略已导入版本的元数据比对, 强制重新导入."""


class R18ImportResult(BaseModel):
    type: Literal[TaskType.R18_IMPORT] = TaskType.R18_IMPORT
    imported: bool
    """False 表示远程未变化而跳过, 不是导入失败."""
    etag: str | None = None
    """导入后记录的 dump ETag, 供下次比对."""


# --- ACTOR SCRAPE ---


class ActorScrapePayload(BaseModel):
    actor_id: int = Field(description="Actor 实体 ID")
    use_cache: set[CacheKind] = Field(
        default_factory=lambda: {CacheKind.metadata, CacheKind.trans},
        description="启用的缓存种类 (metadata: 复用 Actor.raw per-site 快照; trans: 预留演员译文). 空集 = 全部强制刷新",
    )


class ActorScrapeResult(BaseModel):
    type: Literal[TaskType.ACTOR_SCRAPE] = TaskType.ACTOR_SCRAPE
    actor_id: int
    field_sources: dict[str, str]
    failed_sites: list[str]
    image_count: int
    outcomes: list[SiteOutcomeRecord] = Field(default_factory=list)
    """各来源的结果明细; 与记录里的 summary 同源."""


# --- ACTOR PUSH ---


class ActorPushPayload(BaseModel):
    actor_ids: list[int] = Field(default_factory=list, description="要推送的演员 ID; 空表示全部演员")
    force: bool = Field(default=False, description="覆盖媒体服务器上已有的头像与人物字段")


class ActorPushResult(BaseModel):
    type: Literal[TaskType.ACTOR_PUSH] = TaskType.ACTOR_PUSH
    requested: int
    """本次选中的演员数; 自带 ID 列表时含不存在的 id."""
    matched: int
    """媒体服务器上命中的同名人物条目数; 同一人物按 Id 只计一次."""
    updated: int
    """写入了至少一个字段的人物数."""
    images: int
    """上传的主图数."""
    skipped: int
    """无需写入的人物数; 写入失败只计 ``failed``."""
    not_found: int
    """媒体服务器上没有同名人物的演员数."""
    no_image: int
    """本地没有可推送头像的演员数."""
    missing: int
    """自带 ID 列表中不存在的演员数."""
    failed: int
    """失败的人物数; 明细见 ``failures``."""
    failures: list[str]
    """失败明细 (演员 / 人物: 原因), 有上限."""


# --- RESCRAPE ---


class RescrapeTarget(StrEnum):
    """滚动补刮选取的实体种类. 每个已选项各自选取 limit 条."""

    metadata = "metadata"
    """影片 Metadata."""
    actor = "actor"
    """演员 Actor."""


class RescrapePayload(BaseModel):
    """按 updated_at 选取最久未更新的目标, 派生 priority=-1 的非 force 刮削.
    类型取条目列 (进而决定路由), 不依赖挂载文件.
    """

    limit: int = Field(default=100, ge=1, le=1000, description="每个已选目标单次最多补刮的条数 (避免长占 worker 队列)")
    min_age_days: int | None = Field(
        default=None, ge=1, description="仅补刮 updated_at 距今超过该天数的条目; None 不设门槛"
    )
    targets: set[RescrapeTarget] = Field(
        default={RescrapeTarget.metadata},
        min_length=1,
        description="补刮对象; 每个已选项各自选取 limit 条. 缺省仅影片, 兼容既有 Schedule.payload",
    )


class RescrapeResult(BaseModel):
    type: Literal[TaskType.RESCRAPE] = TaskType.RESCRAPE
    submitted: int
    metadata: int = 0
    actors: int = 0


# --- 媒体运维 (115 中转 / 字幕下载 / 洗版 / 备份) ---


class Transfer115Payload(BaseModel):
    """115 中转范围; ``full`` 忽略 ``transfer_items`` 里的既往结果重扫一遍."""

    full: bool = False


class Transfer115Result(BaseModel):
    type: Literal[TaskType.TRANSFER_115] = TaskType.TRANSFER_115
    scanned: int = 0
    moved: int = 0
    skipped: int = 0
    failed: int = 0


class SubtitleFetchPayload(BaseModel):
    """字幕下载范围; 两者都空表示全库按覆盖缺口挑."""

    media_file_id: int | None = None
    library_id: int | None = None


class SubtitleFetchResult(BaseModel):
    type: Literal[TaskType.SUBTITLE_FETCH] = TaskType.SUBTITLE_FETCH
    fetched: int = 0
    missing: int = 0
    failed: int = 0


class SubtitleAuditPayload(BaseModel):
    """巡检范围; 空表示全部媒体库."""

    library_id: int | None = None


class SubtitleAuditResult(BaseModel):
    """``missing`` 取自库内标记, ``orphans`` 取自磁盘, 两者不互相校正. ``samples`` 只放前 20 条路径."""

    type: Literal[TaskType.SUBTITLE_AUDIT] = TaskType.SUBTITLE_AUDIT
    libraries: int = 0
    videos: int = 0
    subtitles: int = 0
    missing: int = 0
    orphans: int = 0
    samples: list[str] = []


class TrashPurgePayload(BaseModel):
    """历史回收站的清理范围; ``dry_run`` 默认开, 因为删除不可回滚."""

    library_id: int | None = None
    keep_days: int = Field(default=30, ge=1, le=3650, description="保留天数, 修改时间更早的文件被永久删除")
    dry_run: bool = Field(default=True, description="只统计不删除")


class TrashPurgeResult(BaseModel):
    type: Literal[TaskType.TRASH_PURGE] = TaskType.TRASH_PURGE
    dry_run: bool = True
    libraries: int = 0
    scanned: int = 0
    purged: int = 0
    bytes_freed: int = 0
    kept: int = 0
    failed: int = 0
    samples: list[str] = []


class WashScanPayload(BaseModel):
    """洗版扫描范围; 只出候选清单, 一律不移动文件."""

    kind: Literal["local", "strm", "all"] = "all"
    library_id: int | None = None


class WashScanResult(BaseModel):
    type: Literal[TaskType.WASH_SCAN] = TaskType.WASH_SCAN
    groups: int = 0
    """同番号存在多份文件的分组数."""
    droppable: int = 0
    """可移走的文件数; 每个分组至少留一份."""
    bytes_freed: int = 0
    """全部移走后可释放的体积."""


class WashApplyPayload(BaseModel):
    """回收站动作与洗版的执行请求; 三类可同时带, 执行顺序是 还原 → 清理 → 洗版."""

    group_keys: list[str] = []
    """要洗的分组键; 三类都空表示按当前策略重算后执行全部候选."""
    restore_ids: list[int] = []
    """要搬回原位的回收站记录 id."""
    purge_ids: list[int] = []
    """要真删的回收站记录 id; 未到期的由任务跳过."""
    kind: Literal["local", "strm", "all"] = "all"
    library_id: int | None = None


class WashApplyResult(BaseModel):
    type: Literal[TaskType.WASH_APPLY] = TaskType.WASH_APPLY
    trashed: int = 0
    """移入回收站的文件数."""
    restored: int = 0
    """搬回库内的回收站记录数."""
    purged: int = 0
    """真删的回收站记录数."""
    bytes_freed: int = 0
    """移入回收站与真删合计释放的体积."""
    bytes_restored: int = 0
    """还原写回库内占用的体积, 与释放量分开计."""
    skipped: int = 0
    failed: int = 0


class BackupPayload(BaseModel):
    """备份范围; 保留份数由 ``BackupConfig.keep_db`` / ``keep_assets`` 决定."""

    kind: Literal["db", "assets", "all"] = "all"


class BackupResult(BaseModel):
    type: Literal[TaskType.BACKUP] = TaskType.BACKUP
    files: list[str] = []
    bytes_written: int = 0
    pruned: int = 0


# --- 媒体库刷新 ---


class MediaRefreshPayload(BaseModel):
    """刷新的范围; Emby 与 Jellyfin 都只提供整库刷新, 因此没有字段."""


class MediaRefreshResult(BaseModel):
    type: Literal[TaskType.MEDIA_REFRESH] = TaskType.MEDIA_REFRESH
    kind: str = ""
    """`media_server.kind`, 说明刷新发去了哪一家."""
    url: str = ""
    """刷新请求的目标地址; 不含密钥, 可直接展示."""


# --- NFO 修补 ---


class NfoPatchPayload(BaseModel):
    """两类修补共用一套载荷, 由 ``kind`` 选择: 给演员块补缩略图, 或删掉导演行."""

    kind: Literal["thumbs", "directors"] = "thumbs"
    library_id: int | None = Field(default=None, description="只修补这一个媒体库; 空表示全部媒体库")
    dry_run: bool = Field(default=True, description="只报告将要改动的文件数, 不写磁盘")


class NfoPatchResult(BaseModel):
    type: Literal[TaskType.NFO_PATCH] = TaskType.NFO_PATCH
    kind: Literal["thumbs", "directors"] = "thumbs"
    dry_run: bool = True
    libraries: int = 0
    """参与的媒体库数."""
    files: int = 0
    """扫到的 NFO 文件数."""
    changed: int = 0
    """需要改写 (dry_run) 或已改写 (真跑) 的文件数."""
    filled: int = 0
    """补上的 ``<thumb>`` 数."""
    missing_image: int = 0
    """演员在库内没有头像地址, 跳过未补的条目数."""
    removed: int = 0
    """删掉的 ``<director>`` 行数."""
    failed: int = 0
    """读取或写入失败的文件数."""
    samples: list[str] = []
    """改动最多的前若干个文件名, 便于人工抽查."""
