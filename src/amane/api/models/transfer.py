from pydantic import BaseModel


class TransferStatusResponse(BaseModel):
    enabled: bool
    pending: int
    moved: int
    skipped: int
    failed: int
