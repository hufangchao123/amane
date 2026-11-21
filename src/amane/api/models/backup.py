from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class BackupEntryResponse(BaseModel):
    name: str
    kind: Literal["db", "assets"]
    size_bytes: int
    created_at: datetime


class BackupStatusResponse(BaseModel):
    enabled: bool
    root: str
    entries: list[BackupEntryResponse]
