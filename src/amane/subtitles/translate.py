"""把转写出来的字幕逐句译成中文: 分批送 LLM, 只回正文, 时间轴由调用方保留.

不逐句单发: 一部片子四五百行, 一次一行会把请求数放大到不可接受. 每批固定行数, 用编号
对齐, 模型少回一行就按原文补齐 —— 字幕错位比译文不全更糟.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from aiolimiter import AsyncLimiter
from pydantic_ai.direct import model_request
from pydantic_ai.messages import ModelMessage, ModelRequest, SystemPromptPart, UserPromptPart
from pydantic_ai.models import Model

logger = structlog.get_logger()

BATCH_SIZE = 40
"""每批行数: 再多容易让模型跳行, 再少请求数翻倍."""

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

#: 思维链模型会在正文前吐 ``<think…>…</think…>``, 不剥掉就会当成译文写进字幕.
#: 三种写法都见过: 正常闭合、缺开头标签的右尖括号、只有开标签没有闭合 —— 逐段清理.
_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
_THINK_TAIL = re.compile(r"^.*</think\s*>", re.DOTALL | re.IGNORECASE)
_THINK_TAG = re.compile(r"</?think\b[^>]*>?", re.IGNORECASE)

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


@dataclass(frozen=True, slots=True)
class TranslationOutcome:
    """逐批翻译的结果; 只回正文, 失败批数由调用方决定怎么处理."""

    lines: list[str]
    failed_batches: int
    """回退原文的批数, 等于请求失败与行数对不上两类之和."""
    total_batches: int
    request_failures: int = 0
    """请求本身失败 (端点不可达、鉴权被拒) 的批数."""

    @property
    def complete(self) -> bool:
        """整片都译到: 没有一批回退原文."""
        return self.failed_batches == 0

    @property
    def nothing_translated(self) -> bool:
        """每一批的请求都失败: 端点整体不可用, 落盘只会得到一份日文字幕.

        只看请求失败, 不看行数对不上: 模型少回一行是它自己的毛病, 端点仍是好的,
        这时整片按原文补齐即可, 不该把一份能用的字幕判成失败.
        """
        return self.total_batches > 0 and self.request_failures == self.total_batches


def _strip_thinking(text: str) -> str:
    """去掉思维链片段, 只留正文."""
    stripped = _THINK_BLOCK.sub("", text)
    if _THINK_TAIL.match(stripped):
        stripped = _THINK_TAIL.sub("", stripped, count=1)
    return _THINK_TAG.sub("", stripped)


def parse_lines(text: str) -> list[str]:
    """从模型输出里取出行数组; 兼容思维链前缀与被包在代码块里的 JSON."""
    cleaned = _strip_thinking(text)
    cleaned = _FENCE.sub("", cleaned).strip()
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
    limiter: AsyncLimiter | None = None,
) -> TranslationOutcome:
    """逐批翻译; 单批失败保留原文并继续, 不让一行砸掉整片字幕.

    ``limiter`` 与字段翻译共用 ``llm.rate_limit``: 一部片十几批请求, 与刮削并发时不限速会把上游打到 429.
    """
    translated: list[str] = []
    failed = 0
    request_failures = 0
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
            if limiter is None:
                response = await model_request(model, messages)
            else:
                async with limiter:
                    response = await model_request(model, messages)
            got = parse_lines(response.text or "")
        except Exception as exc:  # 模型端异常种类由 provider 决定
            logger.warning("subtitle translate failed", batch_start=start, error=str(exc))
            translated.extend(batch)
            failed += 1
            request_failures += 1
            continue
        if len(got) != len(batch):
            # 行数对不上时不能按位置硬拼: 模型中途漏一行, 后面每一句都会错位到上一句的时间轴,
            # 用户看到的是一份「每句都差一句」的字幕. 这一批整体回退原文.
            logger.warning("subtitle translate line count off", expected=len(batch), got=len(got))
            failed += 1
            translated.extend(batch)
            continue
        translated.extend(text.strip() or original for text, original in zip(got, batch, strict=True))
    return TranslationOutcome(
        lines=translated,
        failed_batches=failed,
        total_batches=(len(lines) + batch_size - 1) // batch_size,
        request_failures=request_failures,
    )
