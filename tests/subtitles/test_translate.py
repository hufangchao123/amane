"""字幕译文解析与分批: 用替身顶掉真实模型调用."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from amane.subtitles import translate as module
from amane.subtitles.translate import parse_lines, translate_lines


@dataclass
class _Response:
    text: str | None


@dataclass
class _FakeModel:
    """占位模型对象: ``model_request`` 已被替换, 这里只用来占参数位."""


@dataclass
class _Calls:
    payloads: list[str] = field(default_factory=list)


def _patch(monkeypatch: pytest.MonkeyPatch, outputs: list[str]) -> _Calls:
    calls = _Calls()
    queue = list(outputs)

    async def fake_request(model: object, messages: object) -> _Response:
        calls.payloads.append(str(messages))
        return _Response(queue.pop(0) if queue else None)

    monkeypatch.setattr(module, "model_request", fake_request)
    return calls


class TestParseLines:
    def test_json_array(self) -> None:
        assert parse_lines('["甲","乙"]') == ["甲", "乙"]

    def test_json_inside_code_fence(self) -> None:
        assert parse_lines('```json\n["甲"]\n```') == ["甲"]

    def test_numbered_lines_fallback(self) -> None:
        assert parse_lines("1. 甲\n2) 乙\n") == ["甲", "乙"]


class TestTranslateLines:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_translates_every_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, [json.dumps(["你好", "再见"], ensure_ascii=False)])

        assert await translate_lines(_FakeModel(), ["こんにちは", "さようなら"]) == ["你好", "再见"]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_missing_lines_fall_back_to_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模型少回一行时按原文补齐, 不让整片字幕错位."""
        _patch(monkeypatch, [json.dumps(["你好"], ensure_ascii=False)])

        assert await translate_lines(_FakeModel(), ["こんにちは", "さようなら"]) == ["你好", "さようなら"]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_model_error_keeps_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def boom(model: object, messages: object) -> _Response:
            raise RuntimeError("端点炸了")

        monkeypatch.setattr(module, "model_request", boom)

        assert await translate_lines(_FakeModel(), ["こんにちは"]) == ["こんにちは"]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_batches_split(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _patch(
            monkeypatch,
            [json.dumps(["a1", "a2"], ensure_ascii=False), json.dumps(["b1"], ensure_ascii=False)],
        )

        assert await translate_lines(_FakeModel(), ["x", "y", "z"], batch_size=2) == ["a1", "a2", "b1"]
        assert len(calls.payloads) == 2
