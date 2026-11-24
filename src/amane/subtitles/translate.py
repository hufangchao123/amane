"""把转写出来的字幕逐句译成中文: 分批送 LLM, 只回正文, 时间轴由调用方保留.

不逐句单发: 一部片子四五百行, 一次一行会把请求数放大到不可接受. 每批固定行数, 用编号
对齐, 模型少回一行就按原文补齐 —— 字幕错位比译文不全更糟.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

import structlog
from pydantic_ai.direct import model_request
from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart, UserPromptPart
from pydantic_ai.models import Model

logger = structlog.get_logger()

BATCH_SIZE = 40
"""每批行数: 再多容易让模型跳行, 再少请求数翻倍."""

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

_INSTRUCTIONS = """You translate Japanese adult-video subtitles into Simplified Chinese.

Rules:
- Input is a JSON array of subtitle lines, in order.
- Output ONLY a JSON array of strings with the SAME length and order. No prose, no code fences.
- Translate every line; keep names, onomatopoeia and honorifics natural in Chinese.
- Do not merge or split lines: one input line, one output line.
- If a line is already Chinese, return it unchanged.
"""


class TranslationError(Exception):
    """翻译失败: 端点不可达或返回的形状不对."""


def parse_lines(text: str) -> list[str]:
    """从模型输出里取出行数组; 兼容被包在代码块里的 JSON."""
    cleaned = _FENCE.sub("", text).strip()
    try:
        parsed = json.loads(cleaned)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    # 退一步: 按行拆, 去掉编号与引号, 至少不让整批白跑.
    lines = [line.strip().strip('",') for line in cleaned.splitlines() if line.strip()]
    return [re.sub(r"^\d+[.)]\s*", "", line) for line in lines]


async def translate_lines(
    model: Model,
    lines: Sequence[str],
    *,
    batch_size: int = BATCH_SIZE,
) -> list[str]:
    """逐批翻译; 单批失败保留原文并继续, 不让一行砸掉整片字幕."""
    translated: list[str] = []
    for start in range(0, len(lines), batch_size):
        batch = list(lines[start : start + batch_size])
        payload = json.dumps(batch, ensure_ascii=False)
        messages: list[ModelMessage] = [
            ModelRequest(
                parts=[
                    SystemPromptPart(content=_INSTRUCTIONS),
                    UserPromptPart(content=payload),
                ]
            )
        ]
        try:
            response = await model_request(model, messages)
            got = parse_lines(response.text or "")
        except Exception as exc:  # 模型端异常种类由 provider 决定
            logger.warning("subtitle translate failed", batch_start=start, error=str(exc))
            translated.extend(batch)
            continue
        if len(got) != len(batch):
            logger.warning("subtitle translate line count off", expected=len(batch), got=len(got))
            got = [*got, *batch[len(got) :]][: len(batch)]
        translated.extend(text.strip() or original for text, original in zip(got, batch, strict=True))
    return translated
