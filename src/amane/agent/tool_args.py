"""修复模型产出的非法工具参数 JSON.

部分上游模型会把枚举值当成裸标识符输出: ``{"sql": "…", "entity": data}`` —— 少了一对引号,
不是合法 JSON, pydantic-ai 在解析参数时直接判这次工具调用失败; 模型看到报错后重试, 通常还是
一样的写法, 于是这个工具在这条会话里就再也调不动.

观测到的实例: 网关上的 ``cn:glm-5.3-flash`` 对 ``entity=metadata|actor|data`` 这种短枚举值
一律不加引号 (2026-11-27, ``sql_deliver`` 连续多次失败).

修法很保守 —— 只在「原字符串解析失败」时才改写, 且只给**字符串之外**的裸词补引号, 改完仍要能
``json.loads`` 才采用; 只要原字符串本来就合法, 一个字节都不动. 补引号时保留 ``true`` / ``false``
/ ``null`` 三个 JSON 字面量, 数字不受影响 (裸词必须字母或下划线开头).

顺带也覆盖了另一种同源写法: 键名不加引号 (``{name: "x"}``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, ModelResponsePart, ToolCallPart
from pydantic_ai.tools import RunContext

from .tools import AgentDeps

if TYPE_CHECKING:
    from pydantic_ai.models import ModelRequestContext

_JSON_LITERALS = frozenset({"true", "false", "null"})
_WORD_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_WORD_BODY = _WORD_START | frozenset("0123456789.-")


def quote_bare_tokens(text: str) -> str:
    """给字符串之外的裸词补引号; 字符串内部一个字节都不碰."""
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    escaped = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char in _WORD_START:
            end = index
            while end < length and text[end] in _WORD_BODY:
                end += 1
            word = text[index:end]
            out.append(word if word in _JSON_LITERALS else f'"{word}"')
            index = end
            continue
        out.append(char)
        index += 1
    return "".join(out)


def repair_tool_args(args: str) -> str | None:
    """返回补好引号的参数串; 本来就合法或修不好时返回 ``None``."""
    stripped = args.strip()
    if not stripped:
        return None
    try:
        json.loads(stripped)
    except ValueError:
        pass
    else:
        return None
    fixed = quote_bare_tokens(stripped)
    if fixed == stripped:
        return None
    try:
        json.loads(fixed)
    except ValueError:
        return None
    return fixed


def repair_response_tool_args(response: ModelResponse) -> ModelResponse:
    """逐个工具调用尝试修复; 没有需要修的调用时原样返回."""
    changed = False
    parts: list[ModelResponsePart] = []
    for part in response.parts:
        if isinstance(part, ToolCallPart) and isinstance(part.args, str):
            fixed = repair_tool_args(part.args)
            if fixed is not None:
                parts.append(replace(part, args=fixed))
                changed = True
                continue
        parts.append(part)
    if not changed:
        return response
    return replace(response, parts=parts)


@dataclass
class RepairToolArgs(AbstractCapability[AgentDeps]):
    """执行前修掉参数里的裸词, 让不加引号的枚举值也能调通."""

    async def after_model_request(
        self,
        ctx: RunContext[AgentDeps],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        del ctx, request_context
        return repair_response_tool_args(response)
