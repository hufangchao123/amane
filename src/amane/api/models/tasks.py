from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from fastapi import HTTPException
from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator

from ...db import MediaFile, Repository, TaskStatus, TaskType
from ...db.embeddings import EmbeddingKind
from ...handlers import (
    ActorPushPayload,
    ActorPushResult,
    ActorScrapeResult,
    AvatarCheckPayload,
    AvatarCheckResult,
    CacheKind,
    CleanupPayload,
    CleanupResult,
    DbVacuumPayload,
    DbVacuumResult,
    DeletePayload,
    DeleteResult,
    MediaPurgePayload,
    MediaPurgeResult,
    MediaRefreshPayload,
    MediaRefreshResult,
    NfoPatchPayload,
    NfoPatchResult,
    NotifyPayload,
    NotifyResult,
    OrganizePayload,
    OrganizeResult,
    R18ImportPayload,
    R18ImportResult,
    RefreshPayload,
    RefreshResult,
    RescrapePayload,
    RescrapeResult,
    ScanInvalidPayload,
    ScanInvalidResult,
    ScrapePayload,
    ScrapeResult,
    ScrapeTransferPayload,
    ScrapeTransferResult,
    SemanticBenchmarkResult,
    SemanticIndexResult,
    SubtitleAuditPayload,
    SubtitleAuditResult,
    SubtitleFetchPayload,
    SubtitleFetchResult,
    SubtitleGeneratePayload,
    SubtitleGenerateResult,
    UpscalePayload,
    UpscaleResult,
    WashApplyPayload,
    WashApplyResult,
    WashScanPayload,
    WashScanResult,
)
from ...parsing import ContentType, infer_content_type, parse_file_info


class TaskChildStatusCounts(BaseModel):
    """直接后继按状态计数. 四字段之和等于 child_count."""

    queued: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0


TaskResultPayload = Annotated[
    RefreshResult
    | OrganizeResult
    | ScanInvalidResult
    | DeleteResult
    | MediaPurgeResult
    | ScrapeResult
    | ScrapeTransferResult
    | CleanupResult
    | DbVacuumResult
    | UpscaleResult
    | R18ImportResult
    | ActorScrapeResult
    | ActorPushResult
    | RescrapeResult
    | SemanticIndexResult
    | SemanticBenchmarkResult
    | SubtitleAuditResult
    | SubtitleFetchResult
    | SubtitleGenerateResult
    | NfoPatchResult
    | NotifyResult
    | AvatarCheckResult
    | WashScanResult
    | WashApplyResult
    | MediaRefreshResult,
    Field(discriminator="type"),
]
"""任务结果按任务类型判别; 各成员自带 `type` 字面量, 前端据此收窄."""

_RESULT_ADAPTER: TypeAdapter[TaskResultPayload] = TypeAdapter(TaskResultPayload)


class TaskListItem(BaseModel):
    """列表与树: 不含 payload 与 result 等重字段, 展开时再取详情."""

    id: int
    type: TaskType
    status: TaskStatus
    title: str | None = None
    """scrape→番号, actor_scrape→演员名, refresh/organize/trash→库名."""
    error: str | None = None
    retries: int = 0
    priority: int = 0
    root_task_id: int | None = None
    """根任务指向自己; 裸任务为 None. 前端据此判断是否顶级节点."""
    child_count: int = 0
    """TaskLink 出边数. 树节点是否可展开看这个."""
    child_status: TaskChildStatusCounts = Field(default_factory=TaskChildStatusCounts)
    """折叠节点据此标失败/运行数, 不必展开整层."""
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TaskChildItem(TaskListItem):
    link_key: str
    """父节点内后继语义键 (如 scrape:{media_file_id})."""


class TaskResponse(TaskListItem):
    """详情: 额外带 payload 与按类型的 result."""

    payload: dict = Field(default_factory=dict)
    result: TaskResultPayload | None = None

    @field_validator("result", mode="before")
    @classmethod
    def _validated_result(cls, value: object) -> object:
        """结果按判别联合校验; 旧行与坏数据读不出来时置空, 不做迁移."""
        if value is None or isinstance(value, BaseModel):
            return value
        try:
            return _RESULT_ADAPTER.validate_python(value)
        except ValueError:
            return None


class TaskListResponse(BaseModel):
    items: list[TaskListItem]
    total: int


class TaskChildListResponse(BaseModel):
    items: list[TaskChildItem]
    total: int
    """不受本页 limit/offset 截断."""


class TaskBatchAction(StrEnum):
    CANCEL = "cancel"
    DELETE = "delete"
    RETRY = "retry"


class TaskBatchRequest(BaseModel):
    action: TaskBatchAction
    task_ids: list[int] | None = Field(default=None, min_length=1, max_length=1000)
    """按 ID 操作; 与 status/type 互斥."""
    status: list[TaskStatus] | None = Field(default=None, min_length=1)
    """与列表查询同形; 未传则不限. 与 task_ids 互斥."""
    type: list[TaskType] | None = Field(default=None, min_length=1)
    """与列表查询同形; 未传则不限. 与 task_ids 互斥."""

    @model_validator(mode="after")
    def _exclusive_scope(self) -> Self:
        if self.task_ids is not None and (self.status is not None or self.type is not None):
            raise ValueError("task_ids 与 status/type 不能同时指定")
        return self


class TaskBatchResponse(BaseModel):
    affected: int = 0
    skipped: int = 0
    missing: int = 0
    submitted: int = 0
    task_ids: list[int] = Field(default_factory=list)
    """retry 新建任务的 id; 其它 action 为空."""


class TaskWorkerResponse(BaseModel):
    paused: bool


class ScrapeRequest(BaseModel):
    number: str | None = Field(default=None)
    media_id: int | None = Field(default=None, description="MediaFile ID. 通常仅用于内部提交任务，手动提交无需指定")
    content_type: ContentType | None = Field(
        default=None, description="内容类型; 未给出时: 仅 media_id 按文件路径推断, 有 number 时按番号推断"
    )
    use_cache: set[CacheKind] = Field(
        default_factory=lambda: {CacheKind.metadata, CacheKind.trans},
        description="启用的缓存种类 (metadata: 复用 DB per-site 快照; trans: 复用译文). 空集 = 全部强制刷新",
    )

    @field_validator("number", mode="before")
    @classmethod
    def _normalize_number(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @model_validator(mode="after")
    def _check_source(self) -> Self:
        if self.number is None and self.media_id is None:
            raise ValueError("Either 'number' or 'media_id' must be provided")
        return self

    async def resolve(self, repo: Repository) -> ScrapePayload:
        """media 不存在时抛 HTTPException(404)."""
        media = await repo.get_media_file(self.media_id) if self.media_id is not None else None
        payload = self.build_payload(media)
        if payload is None:
            raise HTTPException(status_code=404, detail=f"媒体文件 {self.media_id} 不存在")
        return payload

    def build_payload(self, media: MediaFile | None) -> ScrapePayload | None:
        """``media_id`` 非空时用调用方查到的行组装; 该行为 None 时返回 None, 由调用方决定计入 missing 还是 404."""
        if self.media_id is not None:
            if media is None:
                return None
            if self.number is not None:
                return ScrapePayload(
                    number=self.number,
                    content_type=self.content_type or infer_content_type(self.number),
                    media_file_id=self.media_id,
                    use_cache=self.use_cache,
                )
            parsed = parse_file_info(media.path)
            assert parsed.number is not None
            return ScrapePayload(
                number=parsed.number,
                content_type=self.content_type or parsed.content_type,
                media_file_id=self.media_id,
                use_cache=self.use_cache,
            )
        assert self.number is not None
        return ScrapePayload(
            number=self.number,
            content_type=self.content_type or infer_content_type(self.number),
            use_cache=self.use_cache,
        )


class RefreshSubmission(RefreshPayload):
    type: Literal["refresh"]


class OrganizeSubmission(OrganizePayload):
    type: Literal["organize"]


class ScanInvalidSubmission(ScanInvalidPayload):
    type: Literal["scan_invalid"]


class DeleteSubmission(DeletePayload):
    type: Literal["delete"]


class ScrapeSubmission(ScrapeRequest):
    type: Literal["scrape"]


class ScrapeTransferSubmission(ScrapeTransferPayload):
    """刮削后的单文件转移: 通常由刮削任务扇出, 也可手工提交重试."""

    type: Literal["scrape_transfer"]


class CleanupSubmission(CleanupPayload):
    type: Literal["cleanup"]


class UpscaleSubmission(UpscalePayload):
    type: Literal["upscale"]


class DbVacuumSubmission(DbVacuumPayload):
    type: Literal["db_vacuum"]


class R18ImportSubmission(R18ImportPayload):
    type: Literal["r18_import"]


class RescrapeSubmission(RescrapePayload):
    type: Literal["rescrape"]


class SemanticIndexSubmission(BaseModel):
    type: Literal["semantic_index"]
    limit: int | None = Field(default=None, ge=1, description="单次最多处理的条目数; 空表示全库")
    force: bool = Field(default=False, description="忽略未变化的跳过判据, 全部重算")
    kinds: list[EmbeddingKind] = Field(
        default_factory=lambda: list(EmbeddingKind),
        min_length=1,
    )


class SemanticBenchmarkSubmission(BaseModel):
    type: Literal["semantic_benchmark"]


class ActorScrapeSubmission(BaseModel):
    type: Literal["actor_scrape"]
    actor_id: int = Field(description="Actor 实体 ID")
    use_cache: set[CacheKind] = Field(
        default_factory=lambda: {CacheKind.metadata, CacheKind.trans},
        description="启用的缓存种类 (metadata: 复用 Actor.raw; trans: 预留译文). 空集 = 全部强制刷新",
    )


class ActorPushSubmission(ActorPushPayload):
    type: Literal["actor_push"]


class SubtitleAuditSubmission(SubtitleAuditPayload):
    """巡检范围; 空表示全部媒体库."""

    type: Literal["subtitle_audit"]


class SubtitleFetchSubmission(SubtitleFetchPayload):
    """字幕批量下载: 不指定范围时按字幕覆盖缺口全量挑."""

    type: Literal["subtitle_fetch"]


class SubtitleGenerateSubmission(SubtitleGeneratePayload):
    """转写生成字幕: 外部站都取不到时的兜底, 一份要跑几十分钟."""

    type: Literal["subtitle_generate"]


class MediaPurgeSubmission(MediaPurgePayload):
    """片库条目清理: 由条目 id 出发删文件与元数据."""

    type: Literal["media_purge"] = "media_purge"


class NfoPatchSubmission(NfoPatchPayload):
    """NFO 修补; 默认只统计不写盘."""

    type: Literal["nfo_patch"]


class NotifySubmission(NotifyPayload):
    """即时消息或周报; 正文留空时由任务现算摘要."""

    type: Literal["notify"]


class AvatarCheckSubmission(AvatarCheckPayload):
    type: Literal["avatar_check"] = "avatar_check"


class WashScanSubmission(WashScanPayload):
    """洗版扫描: 只出候选清单, 一律不移动文件."""

    type: Literal["wash_scan"]


class WashApplySubmission(WashApplyPayload):
    """洗版执行: 按扫描挑出的分组移动文件; 空 ``group_keys`` 表示重算后执行."""

    type: Literal["wash_apply"]


class MediaRefreshSubmission(MediaRefreshPayload):
    """媒体库刷新: 目标服务器由 ``media_server`` 配置决定."""

    type: Literal["media_refresh"]


TaskSubmission = Annotated[
    RefreshSubmission
    | OrganizeSubmission
    | ScanInvalidSubmission
    | DeleteSubmission
    | ScrapeSubmission
    | ScrapeTransferSubmission
    | CleanupSubmission
    | UpscaleSubmission
    | DbVacuumSubmission
    | R18ImportSubmission
    | ActorScrapeSubmission
    | ActorPushSubmission
    | RescrapeSubmission
    | SemanticIndexSubmission
    | SemanticBenchmarkSubmission
    | SubtitleAuditSubmission
    | SubtitleFetchSubmission
    | SubtitleGenerateSubmission
    | WashScanSubmission
    | WashApplySubmission
    | MediaRefreshSubmission
    | MediaPurgeSubmission
    | NfoPatchSubmission
    | NotifySubmission
    | AvatarCheckSubmission,
    Field(discriminator="type"),
]


class NotifyDigestRoutine(BaseModel):
    """周报例行; 摘要由任务按当前数据库现算, 因此没有参数."""

    type: Literal["notify_digest"] = "notify_digest"


class WashPurgeRoutine(BaseModel):
    """回收站到期清理; 到期范围由任务自己按 ``expires_at`` 判定, 因此没有参数."""

    type: Literal["wash_purge"] = "wash_purge"


class MediaRefreshRoutine(BaseModel):
    """媒体库刷新的例行; 目标服务器由 ``media_server`` 配置决定, 因此没有参数."""

    type: Literal["media_refresh"] = "media_refresh"


class DbVacuumRoutine(BaseModel):
    """数据库整理的例行; 参数为空, 整理范围是主库文件本身."""

    type: Literal["db_vacuum"] = "db_vacuum"


class SubtitleAuditRoutine(BaseModel):
    """字幕巡检的例行; 指定库时巡检后会自动排一轮补全, 不指定则只统计全部库."""

    type: Literal["subtitle_audit"] = "subtitle_audit"
    library_id: int | None = None


RoutineSubmission = Annotated[
    CleanupSubmission
    | UpscaleSubmission
    | R18ImportSubmission
    | RescrapeSubmission
    | SemanticIndexSubmission
    | WashScanSubmission
    | WashPurgeRoutine
    | DbVacuumRoutine
    | SubtitleAuditRoutine
    | NotifyDigestRoutine
    | MediaRefreshRoutine,
    Field(discriminator="type"),
]


class TaskBatchSubmitRequest(BaseModel):
    """逐项与 ``POST /tasks`` 的 body 同形, 只是包成数组."""

    submissions: list[TaskSubmission] = Field(min_length=1, max_length=1000)
    """上限是单次请求的任务条数, 与既有批量端点的 id 列表同口径."""


class TaskBatchSubmitResponse(BaseModel):
    submitted: int
    """实际入队的条数; ACTOR_SCRAPE 复用既有 queued/running 任务时也计入."""
    missing: int
    """目标实体不存在的条数 (404). 三个计数之和等于请求的条数."""
    skipped: int
    """与同请求内前面某条语义重复 (同番号同文件 / 同演员) 而丢弃的条数."""
    task_ids: list[int]
    """与请求同序, 已去掉被丢弃的条目."""
