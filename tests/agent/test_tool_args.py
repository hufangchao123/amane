import json

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

from amane.agent.runtime import build_agent
from amane.agent.tool_args import RepairToolArgs, quote_bare_tokens, repair_response_tool_args, repair_tool_args
from amane.config.manager import AgentConfig

BARE_ENTITY = '{"sql":"select 1","name":"丝袜美腿影片","entity":data}'
GOOD_ARGS = '{"sql":"select 1","entity":"data"}'


@pytest.mark.parametrize(
    ("raw", "expected_entity"),
    [
        (BARE_ENTITY, "data"),
        ('{"a":metadata}', "metadata"),
        ('{"a":actor,"b":data}', "actor"),
        ('{"ids":[1,2],"kinds":[data,actor]}', None),
        ('{name:"x"}', None),
    ],
)
def test_unquoted_values_are_quoted(raw: str, expected_entity: str | None) -> None:
    fixed = repair_tool_args(raw)

    assert fixed is not None
    parsed = json.loads(fixed)
    if expected_entity is not None:
        assert parsed.get("entity") == expected_entity or parsed.get("a") == expected_entity


def test_valid_json_is_left_alone() -> None:
    assert repair_tool_args(GOOD_ARGS) is None


def test_json_literals_and_numbers_survive() -> None:
    fixed = repair_tool_args('{"ok":true,"none":null,"ratio":1.5,"flag":false}')
    # 全是合法字面量, 本来就该解析得动, 不该被改写
    assert fixed is None


def test_broken_json_with_literals_keeps_them() -> None:
    fixed = repair_tool_args('{"ok":true,"entity":data}')

    assert fixed is not None
    assert json.loads(fixed) == {"ok": True, "entity": "data"}


def test_string_content_is_not_touched() -> None:
    raw = '{"sql":"select \':data\' as x, \'a:b\' as y","entity":data}'

    fixed = repair_tool_args(raw)

    assert fixed is not None
    parsed = json.loads(fixed)
    assert parsed["sql"] == "select ':data' as x, 'a:b' as y"
    assert parsed["entity"] == "data"


def test_escaped_quotes_in_strings_are_handled() -> None:
    raw = '{"sql":"select \\"data\\" as x","entity":data}'

    fixed = repair_tool_args(raw)

    assert fixed is not None
    assert json.loads(fixed)["sql"] == 'select "data" as x'


def test_unrepairable_returns_none() -> None:
    assert repair_tool_args('{"sql": ') is None
    assert repair_tool_args("") is None
    assert repair_tool_args("   ") is None


def test_quote_bare_tokens_keeps_strings() -> None:
    assert quote_bare_tokens('"a data b" data') == '"a data b" "data"'


def test_response_repair_only_touches_broken_parts() -> None:
    response = ModelResponse(
        parts=[
            ToolCallPart(tool_name="sql_deliver", args=BARE_ENTITY, tool_call_id="call_00"),
            ToolCallPart(tool_name="sql_explore", args=GOOD_ARGS, tool_call_id="call_01"),
            TextPart("done"),
        ]
    )

    repaired = repair_response_tool_args(response)

    first, second, third = repaired.parts
    assert isinstance(first, ToolCallPart)
    assert isinstance(second, ToolCallPart)
    assert isinstance(third, TextPart)
    assert json.loads(first.args_as_json_str())["entity"] == "data"
    assert second.args == GOOD_ARGS
    assert third == TextPart("done")


def test_response_without_tool_calls_is_returned_as_is() -> None:
    response = ModelResponse(parts=[TextPart("hi")])

    assert repair_response_tool_args(response) is response


def test_agent_registers_the_repair_capability() -> None:
    agent = build_agent(AgentConfig(api_key="probe", base_url="http://127.0.0.1:1/v1", model="probe"))

    assert agent is not None
    names = [type(capability).__name__ for capability in agent.root_capability.capabilities]
    assert "RepairToolArgs" in names
    assert names.index("UniqueToolCallIds") < names.index("RepairToolArgs")


def test_capability_is_an_abstract_capability() -> None:
    from pydantic_ai.capabilities import AbstractCapability

    assert isinstance(RepairToolArgs(), AbstractCapability)


def test_broken_arguments_still_reach_the_tool() -> None:
    """端到端: 模型少写引号, 工具仍应收到正确的枚举值."""
    from enum import StrEnum

    from pydantic_ai import Agent
    from pydantic_ai.messages import ToolCallPart as CallPart
    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel
    from pydantic_ai.toolsets import FunctionToolset

    class Kind(StrEnum):
        DATA = "data"
        METADATA = "metadata"

    calls = {"count": 0}

    def fake_model(messages: list[object], info: AgentInfo) -> ModelResponse:
        del info
        calls["count"] += 1
        if calls["count"] == 1:
            return ModelResponse(
                parts=[CallPart(tool_name="deliver", args='{"kind":metadata,"name":"x"}', tool_call_id="call_00")]
            )
        return ModelResponse(parts=[TextPart("ok")])

    toolset: FunctionToolset[None] = FunctionToolset()

    @toolset.tool_plain
    def deliver(kind: Kind, name: str) -> str:
        return f"{kind.value}:{name}"

    agent = Agent(
        FunctionModel(fake_model),
        output_type=str,
        toolsets=[toolset],
        capabilities=[RepairToolArgs()],
    )

    result = agent.run_sync("go")

    assert result.output == "ok"
    returns = [
        part
        for message in result.all_messages()
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart)
    ]
    assert returns
    assert returns[-1].content == "metadata:x"
