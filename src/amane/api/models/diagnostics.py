from pydantic import BaseModel


class CheckItemResponse(BaseModel):
    key: str
    ok: bool
    detail: str


class DiagnosticsResponse(BaseModel):
    items: list[CheckItemResponse]
    failed: int
