"""提交请求 → 任务 payload: 库内任务由 library_id 派生 path, REFRESH / SCAN_INVALID 另派生 recursive / patterns."""

from typing import TYPE_CHECKING, assert_never

from ...db.models import TaskType
from ...handlers import (
    ActorPushPayload,
    ActorScrapePayload,
    AvatarCheckPayload,
    CleanupPayload,
    DbVacuumPayload,
    DeletePayload,
    MediaPurgePayload,
    MediaRefreshPayload,
    NfoPatchPayload,
    NotifyPayload,
    OrganizePayload,
    R18ImportPayload,
    RefreshPayload,
    RescrapePayload,
    ScanInvalidPayload,
    ScrapePayload,
    ScrapeTransferPayload,
    SemanticBenchmarkPayload,
    SemanticIndexPayload,
    SubtitleAuditPayload,
    SubtitleFetchPayload,
    SubtitleGeneratePayload,
    UpscalePayload,
    WashApplyPayload,
    WashScanPayload,
)
from ..models import (
    ActorPushSubmission,
    ActorScrapeSubmission,
    AvatarCheckSubmission,
    CleanupSubmission,
    DbVacuumSubmission,
    DeleteSubmission,
    MediaPurgeSubmission,
    MediaRefreshSubmission,
    NfoPatchSubmission,
    NotifySubmission,
    OrganizeSubmission,
    R18ImportSubmission,
    RefreshSubmission,
    RescrapeSubmission,
    ScanInvalidSubmission,
    ScrapeSubmission,
    ScrapeTransferSubmission,
    SemanticBenchmarkSubmission,
    SemanticIndexSubmission,
    SubtitleAuditSubmission,
    SubtitleFetchSubmission,
    SubtitleGenerateSubmission,
    TaskSubmission,
    UpscaleSubmission,
    WashApplySubmission,
    WashScanSubmission,
)

if TYPE_CHECKING:
    from ...db.repository import Repository

ResolvedPayload = (
    RefreshPayload
    | ScrapePayload
    | ScrapeTransferPayload
    | ScanInvalidPayload
    | DeletePayload
    | MediaPurgePayload
    | OrganizePayload
    | CleanupPayload
    | DbVacuumPayload
    | UpscalePayload
    | R18ImportPayload
    | ActorScrapePayload
    | ActorPushPayload
    | RescrapePayload
    | SemanticIndexPayload
    | SemanticBenchmarkPayload
    | SubtitleAuditPayload
    | SubtitleFetchPayload
    | SubtitleGeneratePayload
    | WashScanPayload
    | NfoPatchPayload
    | AvatarCheckPayload
    | NotifyPayload
    | WashApplyPayload
    | MediaRefreshPayload
)


async def resolve_submission(req: TaskSubmission, repo: Repository) -> tuple[TaskType, ResolvedPayload]:
    match req:
        case RefreshSubmission():
            await req.resolve(repo)
            return TaskType.REFRESH, req
        case OrganizeSubmission():
            await req.resolve(repo)
            return TaskType.ORGANIZE, req
        case DeleteSubmission():
            return TaskType.DELETE, req
        case ScanInvalidSubmission():
            await req.resolve(repo)
            return TaskType.SCAN_INVALID, req
        case ScrapeTransferSubmission():
            return TaskType.SCRAPE_TRANSFER, ScrapeTransferPayload(media_file_id=req.media_file_id, outcome=req.outcome)
        case ScrapeSubmission():
            return TaskType.SCRAPE, await req.resolve(repo)
        case CleanupSubmission():
            return TaskType.CLEANUP, CleanupPayload(
                remove_missing_files=req.remove_missing_files,
                remove_unreferenced_resources=req.remove_unreferenced_resources,
                remove_orphan_metadata=req.remove_orphan_metadata,
            )
        case DbVacuumSubmission():
            return TaskType.DB_VACUUM, DbVacuumPayload()
        case UpscaleSubmission():
            return TaskType.UPSCALE, UpscalePayload(
                max_dim_threshold=req.max_dim_threshold, max_bytes_threshold=req.max_bytes_threshold, limit=req.limit
            )
        case R18ImportSubmission():
            return TaskType.R18_IMPORT, R18ImportPayload(force=req.force)
        case ActorScrapeSubmission():
            return TaskType.ACTOR_SCRAPE, ActorScrapePayload(actor_id=req.actor_id, use_cache=req.use_cache)
        case ActorPushSubmission():
            return TaskType.ACTOR_PUSH, ActorPushPayload(actor_ids=req.actor_ids, force=req.force)
        case RescrapeSubmission():
            return TaskType.RESCRAPE, RescrapePayload(
                limit=req.limit, min_age_days=req.min_age_days, targets=req.targets
            )
        case SemanticIndexSubmission():
            return TaskType.SEMANTIC_INDEX, SemanticIndexPayload(limit=req.limit, force=req.force, kinds=req.kinds)
        case SemanticBenchmarkSubmission():
            return TaskType.SEMANTIC_BENCHMARK, SemanticBenchmarkPayload()
        case SubtitleAuditSubmission():
            return TaskType.SUBTITLE_AUDIT, SubtitleAuditPayload(library_id=req.library_id)
        case SubtitleFetchSubmission():
            return TaskType.SUBTITLE_FETCH, SubtitleFetchPayload(
                media_file_id=req.media_file_id, library_id=req.library_id, number=req.number, limit=req.limit
            )
        case SubtitleGenerateSubmission():
            return TaskType.SUBTITLE_GENERATE, SubtitleGeneratePayload(
                media_file_id=req.media_file_id, library_id=req.library_id, limit=req.limit
            )
        case WashScanSubmission():
            return TaskType.WASH_SCAN, WashScanPayload(kind=req.kind, library_id=req.library_id)
        case AvatarCheckSubmission():
            return TaskType.AVATAR_CHECK, AvatarCheckPayload(limit=req.limit, clear_broken=req.clear_broken)
        case NotifySubmission():
            return TaskType.NOTIFY, NotifyPayload(kind=req.kind, title=req.title, body=req.body)
        case MediaPurgeSubmission():
            return TaskType.MEDIA_PURGE, MediaPurgePayload(
                library_id=req.library_id,
                metadata_ids=req.metadata_ids,
                delete_files=req.delete_files,
                prune_empty_dirs=req.prune_empty_dirs,
            )

        case NfoPatchSubmission():
            return TaskType.NFO_PATCH, NfoPatchPayload(kind=req.kind, library_id=req.library_id, dry_run=req.dry_run)
        case WashApplySubmission():
            return TaskType.WASH_APPLY, WashApplyPayload(
                group_keys=req.group_keys,
                restore_ids=req.restore_ids,
                purge_ids=req.purge_ids,
                kind=req.kind,
                library_id=req.library_id,
            )
        case MediaRefreshSubmission():
            return TaskType.MEDIA_REFRESH, MediaRefreshPayload()
        case _:
            assert_never(req)
