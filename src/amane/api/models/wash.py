from pydantic import BaseModel


class WashStatusResponse(BaseModel):
    enabled: bool
    groups: int
    trashed: int
    restorable_bytes: int
