from pathlib import Path
from typing import TYPE_CHECKING, cast

import structlog
from fastapi import APIRouter, HTTPException, Response

from ...db.models import TaskType
from ...handlers import RefreshPayload, ScanMode
from ...utils.model import to_resp
from ..deps import RepoDep, RuntimeDep
from ..models import (
    LibraryCreateRequest,
    LibraryListResponse,
    LibraryResponse,
    LibraryUpdateRequest,
    PathTemplateSchemaResponse,
    path_template_schema,
)
from ..support.path_validation import validate_directory_path

if TYPE_CHECKING:
    from ...db.repo_types import LibraryUpdates

logger = structlog.get_logger()

router = APIRouter(prefix="/libraries", tags=["libraries"])


@router.get("/path-template-schema")
async def get_path_template_schema() -> PathTemplateSchemaResponse:
    """与 ORGANIZE 实际使用的默认值同源."""
    return path_template_schema()


@router.get("")
async def list_libraries(repo: RepoDep) -> LibraryListResponse:
    items = await repo.list_libraries()
    return LibraryListResponse(items=[to_resp(LibraryResponse, lib) for lib in items])


def _reject_overlap(path: Path, other: Path, *, field: str, other_name: str) -> None:
    """两个目录互相包含时拒绝: 扫描与搬运会互相触发, 同一个文件被反复登记或反复搬动."""
    if path == other or other in path.parents or path in other.parents:
        raise HTTPException(status_code=422, detail=f"{field}不能与{other_name}重叠: {path}")


def _check_scrape_dirs(*, transfer: bool, input_dir: str | None, fail_dir: str | None, library_path: str) -> None:
    """刮削后转移的目录校验: 开启时输入目录必填, 三个目录互不重叠.

    开启后库根是成品目录: 待整理文件先落在输入目录, 刮削结束再搬进库根或失败目录. 目录之间
    一旦重叠, 搬运结果会被扫描重新登记, 或刚搬出的文件被扫回, 因此直接拒绝.
    """
    if not transfer:
        return
    if not input_dir:
        raise HTTPException(status_code=422, detail="开启刮削后转移时必须填输入目录")
    root = Path(library_path).resolve()
    source = Path(input_dir).resolve()
    _reject_overlap(source, root, field="输入目录", other_name="库根")
    if fail_dir:
        target = Path(fail_dir).resolve()
        _reject_overlap(target, root, field="失败目录", other_name="库根")
        _reject_overlap(target, source, field="失败目录", other_name="输入目录")


@router.post("", status_code=201)
async def create_library(req: LibraryCreateRequest, repo: RepoDep, runtime: RuntimeDep) -> LibraryResponse:
    # 校验路径: 须存在、是目录、位于 safe_dirs 内
    await validate_directory_path(req.path, runtime.safe_dirs)
    _check_scrape_dirs(
        transfer=req.scrape_transfer,
        input_dir=req.scrape_input_dir,
        fail_dir=req.scrape_fail_dir,
        library_path=req.path,
    )

    name = req.name or Path(req.path).name

    try:
        lib = await repo.create_library(
            name=name,
            path=req.path,
            automation=req.automation,
            auto_organize=req.auto_organize,
            scrape_transfer=req.scrape_transfer,
            scrape_input_dir=req.scrape_input_dir,
            scrape_fail_dir=req.scrape_fail_dir,
            ingest=req.ingest,
            cloud_path=req.cloud_path,
            recursive=req.recursive,
            patterns=req.patterns,
            move_mode=req.move_mode,
            video_template=req.video_template,
            link_template=req.link_template,
            link_mode=req.link_mode,
            strm_content_template=req.strm_content_template,
            thumb_template=req.thumb_template,
            poster_template=req.poster_template,
            fanart_template=req.fanart_template,
            extrafanart_template=req.extrafanart_template,
            extrafanart_name_template=req.extrafanart_name_template,
            nfo_template=req.nfo_template,
            trailer_template=req.trailer_template,
            subtitle_template=req.subtitle_template,
            subtitle_extensions=req.subtitle_extensions,
            write_nfo=req.write_nfo,
            copy_resources=req.copy_resources,
            trailer_pattern=req.trailer_pattern,
            blacklist_patterns=req.blacklist_patterns,
            min_file_size=req.min_file_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    assert lib.id is not None

    logger.info(
        "library created",
        library_id=lib.id,
        name=name,
        path=req.path,
        automation=req.automation,
        ingest=req.ingest,
    )

    if runtime.watcher_service:
        runtime.watcher_service.sync_library(lib)

    # 提交初始 Refresh 任务
    if req.scan:
        await repo.create_task(
            TaskType.REFRESH,
            RefreshPayload(
                library_id=lib.id,
                recursive=req.recursive,
                patterns=req.patterns,
                # 用落库后的路径: 入口已把库路径解析为真实路径, 首次扫描必须与它同一形式.
                path=lib.path,
                scan={ScanMode.add},
                scrape=set(),
            ),
        )

    return to_resp(LibraryResponse, lib)


@router.get("/{library_id}")
async def get_library(library_id: int, repo: RepoDep) -> LibraryResponse:
    lib = await repo.get_library(library_id)
    if lib is None:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    return to_resp(LibraryResponse, lib)


@router.patch("/{library_id}")
async def update_library(
    library_id: int,
    req: LibraryUpdateRequest,
    repo: RepoDep,
    runtime: RuntimeDep,
) -> LibraryResponse:
    updates = cast("LibraryUpdates", req.model_dump(exclude_unset=True))
    if not updates:
        raise HTTPException(status_code=422, detail="没有需要修改的字段")

    # 仅 path 被显式更新时才校验
    if "path" in updates and updates["path"] is not None:
        await validate_directory_path(updates["path"], runtime.safe_dirs)

    current = await repo.get_library(library_id)
    if current is None:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    _check_scrape_dirs(
        transfer=updates.get("scrape_transfer", current.scrape_transfer),
        input_dir=updates.get("scrape_input_dir", current.scrape_input_dir),
        fail_dir=updates.get("scrape_fail_dir", current.scrape_fail_dir),
        library_path=updates.get("path", current.path),
    )

    try:
        lib = await repo.update_library(library_id, **updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if lib is None:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    logger.info("library updated", library_id=library_id, fields=list(updates.keys()))

    watch_fields = {
        "automation",
        "ingest",
        "cloud_path",
        "path",
        "recursive",
        "patterns",
        "trailer_pattern",
        "blacklist_patterns",
        "min_file_size",
    }
    if runtime.watcher_service and watch_fields & updates.keys():
        runtime.watcher_service.sync_library(lib)
    # 清单记录生成时的库根: 路径改掉后按新库根重解释相对路径会删错文件.
    if "path" in updates:
        runtime.inventory_store.drop_library(library_id)

    return to_resp(LibraryResponse, lib)


@router.delete("/{library_id}", status_code=204)
async def delete_library(library_id: int, repo: RepoDep, runtime: RuntimeDep):
    """级联删除该库 MediaFile (library_id 非空 FK). 仅删除数据库索引, 不动磁盘文件."""
    existing = await repo.list_libraries()
    if not any(lib.id == library_id for lib in existing):
        raise HTTPException(status_code=404, detail="媒体库不存在")

    # 先停止监控, 避免删库后 watcher 仍为已删库 id 创建悬空 MediaFile
    if runtime.watcher_service:
        runtime.watcher_service.remove_library(library_id)

    deleted_media = await repo.delete_library(library_id)
    runtime.inventory_store.drop_library(library_id)
    logger.info("library deleted", library_id=library_id, deleted_media=deleted_media)
    return Response(status_code=204)
