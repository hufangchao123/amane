"""把模型返回的 ``tool_call_id`` 改写成整段历史唯一的值.

上游网关（例如 GLM 系的 ``call_00``/``call_01`` 编号）每条响应都从 0 重新编号, 多轮会话里
第二个回合的第一个工具调用仍叫 ``call_00``, 与历史里已执行的同名调用撞车. pydantic-ai 要求
``tool_call_id`` 在整段历史里唯一, 撞车后审批恢复会直接报
``Tool call 'call_00' was already executed and its result cannot be overridden``.

该现象只在同一条会话里先后出现两次同名调用时暴露, 而且与具体站点无关, 因此统一在
``after_model_request`` 给每个调用补一个随机后缀 —— 结果与调用仍靠改写后的 id 配对.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, ModelResponsePart, ToolCallPart
from pydantic_ai.tools import RunContext

from .tools import AgentDeps

if TYPE_CHECKING:
    from pydantic_ai.models import ModelRequestContext

SUFFIX_LEN = 8
"""随机后缀长度. 8 位十六进制足够避免同会话内碰撞, 又不会把 id 撑得太长."""


def uniquify_tool_call_ids(response: ModelResponse) -> ModelResponse:
    """给每个工具调用补随机后缀; 没有工具调用时原样返回."""
    changed = False
    parts: list[ModelResponsePart] = []
    for part in response.parts:
        if isinstance(part, ToolCallPart):
            parts.append(replace(part, tool_call_id=f"{part.tool_call_id}-{uuid4().hex[:SUFFIX_LEN]}"))
            changed = True
            continue
        parts.append(part)
    if not changed:
        return response
    return replace(response, parts=parts)


@dataclass
class UniqueToolCallIds(AbstractCapability[AgentDeps]):
    """执行前把 ``tool_call_id`` 改成本回合唯一, 避免上游复用 ``call_00`` 撞历史."""

    async def after_model_request(
        self,
        ctx: RunContext[AgentDeps],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        del ctx, request_context
        return uniquify_tool_call_ids(response)
