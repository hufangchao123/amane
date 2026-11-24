#!/usr/bin/env python3
"""从 FastAPI 应用导出 OpenAPI schema, 供前端 SDK 生成 (无需启动服务器).

用法: uv run python scripts/export_openapi.py
输出: web/openapi.json
"""

from __future__ import annotations

import json
from pathlib import Path

from amane.api.app import create_app
from amane.config import HotSettings


def _add_hot_settings_schema(schema: dict[str, object]) -> None:
    """把 ``HotSettings`` 的 JSON Schema 并入 components.

    ``GET /api/config`` 的响应是脱敏后的字典 (字段值是 ``***`` 占位, 过不了模型校验),
    所以它不再是 ``HotSettings``, 生成物里就没有这个类型; 但前端的设置表单与 i18n 完整性检查
    需要它的字段结构. 这里按模型自带 schema 补进 components, 供 SDK 生成读取.
    """
    components = schema.setdefault("components", {})
    assert isinstance(components, dict)
    schemas = components.setdefault("schemas", {})
    assert isinstance(schemas, dict)
    hot = HotSettings.model_json_schema(ref_template="#/components/schemas/{model}")
    definitions = hot.pop("$defs", {})
    for name, definition in definitions.items():
        schemas.setdefault(name, definition)
    schemas["HotSettings"] = hot


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    out = root / "web" / "openapi.json"
    app = create_app()
    schema = app.openapi()
    _add_hot_settings_schema(schema)
    # newline="\n" 固定 LF: Windows 上 Path.write_text 默认会做 \n → \r\n 转写,
    # 导致 regen 后的 openapi.json 与仓库内 LF 版产生整文件 diff, 误报 check-openapi.
    out.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
