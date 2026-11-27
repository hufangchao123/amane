"""``UniqueToolCallIds``: 上游复用 ``call_00`` 时不能撞历史里的同名调用."""

from __future__ import annotations

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

from amane.agent.tool_call_ids import uniquify_tool_call_ids


def _response(*parts: object) -> ModelResponse:
    return ModelResponse(parts=list(parts))  # type: ignore[arg-type]


def test_duplicate_ids_across_turns_become_unique() -> None:
    first = uniquify_tool_call_ids(_response(ToolCallPart(tool_name="list_media", args="{}", tool_call_id="call_00")))
    second = uniquify_tool_call_ids(_response(ToolCallPart(tool_name="list_media", args="{}", tool_call_id="call_00")))
    first_id = first.parts[0].tool_call_id  # type: ignore[union-attr]
    second_id = second.parts[0].tool_call_id  # type: ignore[union-attr]

    assert first_id.startswith("call_00-")
    assert second_id.startswith("call_00-")
    assert first_id != second_id
    assert len(first_id) == len("call_00") + 1 + 8


def test_ids_inside_one_response_are_unique_too() -> None:
    response = uniquify_tool_call_ids(
        _response(
            ToolCallPart(tool_name="a", args="{}", tool_call_id="call_00"),
            ToolCallPart(tool_name="b", args="{}", tool_call_id="call_00"),
        )
    )
    ids = [part.tool_call_id for part in response.parts if isinstance(part, ToolCallPart)]  # type: ignore[union-attr]
    assert len(set(ids)) == 2


def test_non_tool_parts_are_kept() -> None:
    response = uniquify_tool_call_ids(_response(TextPart(content="hello")))
    assert response.parts == [TextPart(content="hello")]


def test_response_without_tool_calls_is_returned_as_is() -> None:
    original = _response(TextPart(content="done"))
    assert uniquify_tool_call_ids(original) is original
