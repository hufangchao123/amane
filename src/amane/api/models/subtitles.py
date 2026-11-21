from pydantic import BaseModel


class SubtitleStatusResponse(BaseModel):
    enabled: bool
    missing: int
    queued: int
