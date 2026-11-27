"""``dedupe_tool_call_ids``: 历史里的同 id 调用改名, 在飞的那次保持原样."""

from __future__ import annotations

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)


def _call(tool_call_id: str, name: str = "list_media") -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name=name, args="{}", tool_call_id=tool_call_id)])


def _ret(tool_call_id: str, name: str = "list_media") -> ModelRequest:
    return ModelRequest(parts=[ToolReturnPart(tool_name=name, content="ok", tool_call_id=tool_call_id)])


def _ids(messages: list[ModelMessage]) -> list[str]:
    return [
        part.tool_call_id
        for message in messages
        for part in message.parts
        if isinstance(part, (ToolCallPart, ToolReturnPart))
    ]


def test_older_duplicate_is_renamed_and_last_kept() -> None:
    from amane.agent.tool_call_ids import dedupe_tool_call_ids

    history: list[ModelMessage] = [_call("call_00"), _ret("call_00"), _call("call_00"), _ret("call_00")]

    deduped, renamed = dedupe_tool_call_ids(history)

    ids = _ids(deduped)
    assert renamed == 2  # 早那条的调用与它的结果
    assert ids[0] != "call_00"
    assert ids[0] == ids[1]  # 调用与结果同步改名
    assert ids[1].startswith("call_00-")
    assert ids[2] == ids[3] == "call_00"  # 最后一次保持原样, 等待审批的那次不会被改
    assert len(set(ids)) == 2  # {call_00, call_00-xxx}


def test_unique_ids_are_untouched() -> None:
    from amane.agent.tool_call_ids import dedupe_tool_call_ids

    history: list[ModelMessage] = [_call("call_00"), _ret("call_00"), _call("call_01"), _ret("call_01")]

    deduped, renamed = dedupe_tool_call_ids(history)

    assert renamed == 0
    assert deduped == history


def test_new_call_without_result_is_not_merged_into_the_old_one() -> None:
    from amane.agent.tool_call_ids import dedupe_tool_call_ids

    # 第二次 call_00 还没结果（正在等审批）: 它是一条新记录, 不该和上一条视作同一组
    history: list[ModelMessage] = [_call("call_00"), _ret("call_00"), _call("call_00")]

    deduped, renamed = dedupe_tool_call_ids(history)

    ids = _ids(deduped)
    assert renamed == 2
    assert ids[0] == ids[1] != "call_00"
    assert ids[2] == "call_00"


def test_retry_prompt_follows_its_call() -> None:
    from amane.agent.tool_call_ids import dedupe_tool_call_ids

    history: list[ModelMessage] = [
        _call("call_00"),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="list_media", content="bad args", tool_call_id="call_00", outcome="retried")
            ]
        ),
        _call("call_00"),
        _ret("call_00"),
    ]

    deduped, renamed = dedupe_tool_call_ids(history)

    ids = _ids(deduped)
    assert renamed == 2
    assert ids[0] == ids[1] != "call_00"
    assert ids[2] == ids[3] == "call_00"


def test_text_parts_and_empty_history_survive() -> None:
    from amane.agent.tool_call_ids import dedupe_tool_call_ids

    empty, renamed = dedupe_tool_call_ids([])
    assert empty == []
    assert renamed == 0

    response = ModelResponse(parts=[TextPart(content="hello")])
    deduped, renamed = dedupe_tool_call_ids([response])
    assert renamed == 0
    assert deduped == [response]
