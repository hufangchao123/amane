"""留痕写入: 只拦写方法、查询参数脱敏、写入失败不影响调用方、超上限修剪."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from amane.observability.audit import AuditLog, should_audit

if TYPE_CHECKING:
    from amane.db.repository import Repository


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        pytest.param("POST", "/api/libraries", True, id="post"),
        pytest.param("DELETE", "/api/tasks/1", True, id="delete"),
        pytest.param("GET", "/api/libraries", False, id="get"),
        pytest.param("POST", "/api/health", False, id="health"),
        pytest.param("POST", "/api/ws", False, id="socket"),
        pytest.param("POST", "/index.html", False, id="outside-api"),
    ],
)
def test_should_audit(method: str, path: str, expected: bool) -> None:
    assert should_audit(method, path) is expected


async def _write(log: AuditLog, path: str = "/api/x", query: str = "") -> bool:
    return await log.record(method="POST", path=path, query=query, status=201, duration_ms=1.5, client="127.0.0.1")


@pytest.mark.asyncio(loop_scope="function")
async def test_record_redacts_secret_query(repo: Repository) -> None:
    assert await _write(AuditLog(repo), query="token=abc&page=2")
    records = await repo.list_audit_records(limit=10, offset=0)
    assert records[0].query == "token=***&page=2"
    assert records[0].duration_ms == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_record_prunes_when_over_limit(repo: Repository) -> None:
    log = AuditLog(repo, keep=2)
    for index in range(3):
        assert await _write(log, path=f"/api/{index}")
    # 修剪按累计写入触发, 因此表的上限是 keep 的两倍: 最后一次修剪之后最多再写 keep 条
    assert await repo.count_audit_records() <= 4


@pytest.mark.asyncio(loop_scope="function")
async def test_write_failure_is_reported_not_raised(repo: Repository) -> None:
    """留痕是旁路: 表不可写时返回 False, 不把异常抛给请求."""

    class _Broken:
        async def add_audit_record(self, **_: object) -> int:
            raise RuntimeError("库忙")

        async def prune_audit_records(self, _keep: int) -> int:
            raise RuntimeError("库忙")

    broken = AuditLog(_Broken())  # pyright: ignore[reportArgumentType]
    assert await _write(broken) is False
