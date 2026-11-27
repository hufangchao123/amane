from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from ...agent import AgentService
from ...db.models import DEFAULT_SESSION_TITLE, AgentSession
from ..deps import AgentDep, RepoDep
from ..models.agent import (
    AgentSessionCreateRequest,
    AgentSessionListResponse,
    AgentSessionResponse,
    AgentSessionTitleRequest,
    AgentSessionTitleResponse,
    AgentSessionUpdateRequest,
    AgentTraceResponse,
)

router = APIRouter(tags=["agent"])


def _session_response(service: AgentService, session: AgentSession) -> AgentSessionResponse:
    assert session.id is not None
    return AgentSessionResponse(
        id=session.id,
        title=session.title,
        status=session.status,
        thinking=service.session_thinking(session.id),
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


@router.post("/agent/sessions", status_code=201)
async def create_agent_session(service: AgentDep, req: AgentSessionCreateRequest | None = None) -> AgentSessionResponse:
    title = req.title if req is not None else DEFAULT_SESSION_TITLE
    session = await service.create_session(title=title)
    return _session_response(service, session)


@router.get("/agent/sessions")
async def list_agent_sessions(service: AgentDep, repo: RepoDep) -> AgentSessionListResponse:
    items = await repo.list_agent_sessions()
    return AgentSessionListResponse(items=[_session_response(service, s) for s in items if s.id is not None])


@router.patch("/agent/sessions/{session_id}")
async def update_agent_session(
    session_id: int, req: AgentSessionUpdateRequest, service: AgentDep, repo: RepoDep
) -> AgentSessionResponse:
    fields = req.model_fields_set
    if not fields:
        raise HTTPException(422, detail="无更新字段")
    title = req.title if "title" in fields else None
    session = await repo.get_agent_session(session_id)
    if session is None:
        raise HTTPException(404, detail="会话不存在")
    if title is not None:
        session = await repo.update_agent_session(session_id, title=title)
        if session is None:
            raise HTTPException(404, detail="会话不存在")
        store = service.store_for(session_id)
        meta = store.read_meta()
        meta["title"] = title
        store.write_meta(meta)
    if "thinking" in fields:
        service.set_session_thinking(session_id, req.thinking)
    return _session_response(service, session)


@router.post("/agent/sessions/{session_id}/title")
async def generate_agent_session_title(
    session_id: int, req: AgentSessionTitleRequest, service: AgentDep, repo: RepoDep
) -> AgentSessionTitleResponse:
    """按首条输入生成标题. 与主回合并行执行, 前端不等回合结束即可刷新列表."""
    if await repo.get_agent_session(session_id) is None:
        raise HTTPException(404, detail="会话不存在")
    return AgentSessionTitleResponse(title=await service.name_session(session_id, req.prompt))


@router.delete("/agent/sessions/{session_id}", status_code=204)
async def delete_agent_session(session_id: int, service: AgentDep) -> None:
    ok = await service.delete_session(session_id)
    if not ok:
        raise HTTPException(404, detail="会话不存在")


TRACE_DEFAULT_LIMIT = 2000
"""整段回放的默认行数上限. 十几万行的会话整段发给页面会让浏览器内存吃满, 而页面要的是末尾."""


@router.get("/agent/sessions/{session_id}/trace")
async def get_agent_trace(
    session_id: int,
    service: AgentDep,
    repo: RepoDep,
    limit: Annotated[int, Query(ge=0, le=20000)] = TRACE_DEFAULT_LIMIT,
) -> AgentTraceResponse:
    """整段回放; `limit` 只取最近若干行 (`0` 表示不限, 供排查与导出)."""
    session = await repo.get_agent_session(session_id)
    if session is None:
        raise HTTPException(404, detail="会话不存在")
    store = service.store_for(session_id)
    total = len(store.ui_events())
    rows = store.ui_events(None if limit == 0 else limit)
    return AgentTraceResponse(
        meta=store.read_meta(),
        events=rows,
        turn_running=service.is_turn_running(session_id),
        last_seq=store.last_seq,
        truncated=len(rows) < total,
    )
