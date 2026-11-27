"""让历史的 ``tool_call_id`` 不再与新一轮的 ``call_00`` 撞车.

上游网关（例如 GLM 系的 ``call_00``/``call_01`` 编号）每条响应都从 0 重新编号, 多轮会话里
第二个回合的第一个工具调用仍叫 ``call_00``. pydantic-ai 要求 ``tool_call_id`` 在整段历史里
唯一, 撞车后审批恢复会直接报
``Tool call 'call_00' was already executed and its result cannot be overridden``.

早期做法是在 ``after_model_request`` 把**当回合**的调用改名, 但那会让流式事件里
``TOOL_CALL_START`` 用改名后的 id、``TOOL_CALL_ARGS`` 仍是原 id, AG-UI 客户端随即报
``Cannot send 'TOOL_CALL_ARGS' event: No active tool call found with ID 'call_00'``.

现在改成只改**历史**: 载入会话消息后把重复 id 里较早的那些改名, 最后一次出现保持原样 ——
于是在飞的那次调用（含等待审批的）id 不变, 流式事件前后一致, 而新一轮的 ``call_00``
在历史里已经唯一.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from uuid import uuid4

from pydantic_ai.messages import ModelMessage, ToolCallPart, ToolReturnPart

SUFFIX_LEN = 8
"""随机后缀长度. 8 位十六进制足够避免同会话内碰撞, 又不会把 id 撑得太长."""

_RETURN_PARTS = (ToolReturnPart,)


def _tool_call_id(part: object) -> str | None:
    if isinstance(part, (ToolCallPart, *_RETURN_PARTS)):
        return part.tool_call_id
    return None


def _is_open_call(parts: Sequence[object]) -> bool:
    """这组里只有调用、还没配上结果."""
    return any(isinstance(part, ToolCallPart) for part in parts) and not any(
        isinstance(part, _RETURN_PARTS) for part in parts
    )


def _group_occurrences(messages: Sequence[ModelMessage]) -> dict[str, list[list[tuple[int, int]]]]:
    """按 id 把出现的部件分成「调用 + 它的结果」若干组, 保持出现顺序."""
    groups: dict[str, list[list[tuple[int, int]]]] = {}
    for message_index, message in enumerate(messages):
        for part_index, part in enumerate(message.parts):
            tool_call_id = _tool_call_id(part)
            if tool_call_id is None:
                continue
            occurrences = groups.setdefault(tool_call_id, [])
            current = occurrences[-1] if occurrences else []
            if current and _is_open_call([messages[i].parts[j] for i, j in current]):
                current.append((message_index, part_index))
            else:
                occurrences.append([(message_index, part_index)])
    return groups


def dedupe_tool_call_ids(messages: Sequence[ModelMessage]) -> tuple[list[ModelMessage], int]:
    """把重复 id 里除最后一次以外的出现改名; 返回新消息列表与被改名的部件数.

    最后一次出现保持原 id —— 它可能是正在等待审批的调用, 改名会让结果对不上.
    """
    renames: dict[tuple[int, int], str] = {}
    for tool_call_id, occurrences in _group_occurrences(messages).items():
        if len(occurrences) < 2:
            continue
        for occurrence in occurrences[:-1]:
            new_id = f"{tool_call_id}-{uuid4().hex[:SUFFIX_LEN]}"
            for index in occurrence:
                renames[index] = new_id

    if not renames:
        return list(messages), 0

    deduped: list[ModelMessage] = []
    for message_index, message in enumerate(messages):
        parts = list(message.parts)
        changed = False
        for part_index, part in enumerate(parts):
            new_id = renames.get((message_index, part_index))
            if new_id is None:
                continue
            parts[part_index] = replace(part, tool_call_id=new_id)  # type: ignore[type-var]
            changed = True
        deduped.append(replace(message, parts=parts) if changed else message)  # type: ignore[type-var]
    return deduped, len(renames)
