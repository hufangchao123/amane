"""备份产物列举; 生成与修剪是 ``BACKUP`` 任务."""

from fastapi import APIRouter

from ...backup import BackupManager
from ..deps import RuntimeDep
from ..models.backup import BackupEntryResponse, BackupStatusResponse

router = APIRouter(prefix="/backup", tags=["backup"])


@router.get("/status", response_model=BackupStatusResponse)
async def backup_status(runtime: RuntimeDep) -> BackupStatusResponse:
    manager = BackupManager(runtime.config.hot.backup, runtime.config.cold.data_dir)
    entries = await manager.list_backups()
    return BackupStatusResponse(
        enabled=runtime.config.hot.backup.enabled,
        root=str(manager.root),
        entries=[
            BackupEntryResponse(
                name=entry.name, kind=entry.kind, size_bytes=entry.size_bytes, created_at=entry.created_at
            )
            for entry in entries
        ],
    )
