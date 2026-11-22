"""写操作留痕: 写接口留下记录, 读接口不留; 清空接口返回 204."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from amane.api.middleware import AuditMiddleware

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio(loop_scope="function")
async def test_write_request_is_audited(client: AsyncClient, safe_path: Path) -> None:
    assert (await client.post("libraries", json={"path": str(safe_path), "name": "留痕"})).status_code == 201
    listed = (await client.get("audit")).json()
    assert listed["total"] >= 1
    entry = listed["items"][0]
    assert entry["method"] == "POST"
    assert entry["path"] == "/api/libraries"
    assert entry["status"] == 201


@pytest.mark.asyncio(loop_scope="function")
async def test_read_request_is_not_audited(client: AsyncClient) -> None:
    await client.get("libraries")
    assert (await client.get("audit")).json()["total"] == 0


@pytest.mark.asyncio(loop_scope="function")
async def test_clear_empties_records(client: AsyncClient, safe_path: Path) -> None:
    assert (await client.post("libraries", json={"path": str(safe_path), "name": "留痕"})).status_code == 201
    assert (await client.delete("audit")).status_code == 204
    # 清空动作本身也是写请求, 因此清完会留下这一条
    listed = (await client.get("audit")).json()
    assert listed["total"] == 1
    assert listed["items"][0]["method"] == "DELETE"
    assert listed["items"][0]["path"] == "/api/audit"


class _RecordingAudit:
    """替身: 中间件调用一次记一条, 便于断言留没留痕."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def record(self, **entry: Any) -> bool:
        self.entries.append(entry)
        return True


def _probe_app(audit: _RecordingAudit) -> FastAPI:
    """只装审计中间件的小应用: 用来构造这两个仅在异常与拒绝路径出现的场景."""
    app = FastAPI()
    app.state.runtime = SimpleNamespace(audit=audit)
    app.add_middleware(AuditMiddleware)

    @app.post("/api/boom")
    async def boom() -> None:
        raise RuntimeError("炸了")

    @app.post("/api/denied")
    async def denied() -> Any:
        from starlette.responses import JSONResponse

        return JSONResponse({"detail": "没有权限"}, status_code=401)

    @app.get("/api/read")
    async def read() -> dict[str, bool]:
        return {"ok": True}

    return app


async def _post(app: FastAPI, path: str) -> int:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(path)
    return response.status_code


@pytest.mark.asyncio(loop_scope="function")
async def test_exception_is_audited_as_500() -> None:
    """端点抛异常时最该追溯, 没有响应也得以 500 留痕."""
    audit = _RecordingAudit()
    assert await _post(_probe_app(audit), "/api/boom") == 500
    assert [(item["method"], item["path"], item["status"]) for item in audit.entries] == [("POST", "/api/boom", 500)]


@pytest.mark.asyncio(loop_scope="function")
async def test_rejected_write_is_not_recorded() -> None:
    """被拒的写请求没有发生写入: 逐条同步 commit 会被高频匿名请求放大."""
    audit = _RecordingAudit()
    assert await _post(_probe_app(audit), "/api/denied") == 401
    assert audit.entries == []


@pytest.mark.asyncio(loop_scope="function")
async def test_audit_failure_does_not_change_response() -> None:
    """留痕写库失败不该影响响应."""

    class _Broken(_RecordingAudit):
        async def record(self, **entry: Any) -> bool:
            raise RuntimeError("库不可用")

    audit = _Broken()
    assert await _post(_probe_app(audit), "/api/boom") == 500
