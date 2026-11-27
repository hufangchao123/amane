"""运行日志尾部接口: 日志页刷新后用它回填历史."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient


def _write_log(path: Path, messages: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"event": message, "level": "INFO", "logger": "amane.test", "timestamp": "2026-01-01T00:00:00Z"})
        for message in messages
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_logs_returns_the_tail(client: AsyncClient, tmp_path: Path) -> None:
    _write_log(tmp_path / "logs" / "app.log", [f"第{i}条" for i in range(1, 6)])

    response = await client.get("logs?limit=3")

    assert response.status_code == 200
    body = response.json()
    assert [entry["message"] for entry in body["entries"]] == ["第3条", "第4条", "第5条"]
    assert body["truncated"] is True


@pytest.mark.asyncio
async def test_logs_reads_rotated_files_from_newest_back(client: AsyncClient, tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _write_log(log_dir / "app.log.1", [f"旧{i}" for i in range(3)])
    _write_log(log_dir / "app.log", [f"新{i}" for i in range(2)])

    response = await client.get("logs?limit=5")

    assert response.status_code == 200
    body = response.json()
    assert [entry["message"] for entry in body["entries"]] == ["旧0", "旧1", "旧2", "新0", "新1"]
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_logs_skips_broken_lines(client: AsyncClient, tmp_path: Path) -> None:
    path = tmp_path / "logs" / "app.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"event": "好的一行", "level": "warning"}\n坏行\n{"no_event": 1}\n', encoding="utf-8")

    response = await client.get("logs")

    assert response.status_code == 200
    entries = response.json()["entries"]
    assert [entry["message"] for entry in entries] == ["好的一行"]
    assert entries[0]["level"] == "WARNING"


@pytest.mark.asyncio
async def test_logs_returns_empty_page_when_file_missing(client: AsyncClient) -> None:
    response = await client.get("logs")

    assert response.status_code == 200
    assert response.json() == {"entries": [], "truncated": False}


@pytest.mark.asyncio
async def test_logs_rejects_non_positive_limit(client: AsyncClient) -> None:
    response = await client.get("logs?limit=0")

    assert response.status_code == 422
