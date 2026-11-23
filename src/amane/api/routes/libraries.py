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


def _outside_only(path: Path, root: Path, *, field: str, other_name: str) -> None:
    """目的目录可以等于上位目录 (等于就是留在原地), 但不能落在它下面: 搬进库内的子目录会被当成新文件重新入库."""
    if path != root and root in path.parents:
        raise HTTPException(status_code=422, detail=f"{field}不能位于{other_name}之下: {path}")


def _check_scrape_dirs(
    *,
    transfer: bool,
    input_dir: str | None,
    ok_dir: str | None,
    fail_dir: str | None,
    library_path: str,
) -> None:
    """刮削后转移的目录校验: 三个目录都可空, 但要留一个.

    输入目录给定时它必须在库外, 否则与扫描互相触发; 成功与失败目录可以等于库根 (等于就是留在
    原地), 但不能落在库根之下, 否则搬过去的文件会被重新登记成新文件. 三者之间不得互相包含.
    """
    if not transfer:
        return
    if not input_dir and not ok_dir and not fail_dir:
        raise HTTPException(status_code=422, detail="开启刮削后转移时至少填一个目录")

    root = Path(library_path).resolve()
    source = Path(input_dir).resolve() if input_dir else None
    if source is not None:
        _reject_overlap(source, root, field="输入目录", other_name="库根")

    targets: list[tuple[str, Path]] = []
    for field, raw in (("成功目录", ok_dir), ("失败目录", fail_dir)):
        if not raw:
            continue
        target = Path(raw).resolve()
        _outside_only(target, root, field=field, other_name="库根")
        if source is not None:
            _reject_overlap(target, source, field=field, other_name="输入目录")
        targets.append((field, target))

    if len(targets) == 2 and targets[0][1] == targets[1][1]:
        raise HTTPException(status_code=422, detail="成功目录与失败目录不能相同")


@router.post("", status_code=201)
async def create_library(req: LibraryCreateRequest, repo: RepoDep, runtime: RuntimeDep) -> LibraryResponse:
    # 校验路径: 须存在、是目录、位于 safe_dirs 内
    await validate_directory_path(req.path, runtime.safe_dirs)
    _check_scrape_dirs(
        transfer=req.scrape_transfer,
        input_dir=req.scrape_input_dir,
        ok_dir=req.scrape_ok_dir,
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
            scrape_ok_dir=req.scrape_ok_dir,
            scrape_fail_dir=req.scrape_fail_dir,
            keep_source_dirs=req.keep_source_dirs,
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
        ok_dir=updates.get("scrape_ok_dir", current.scrape_ok_dir),
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
