"""启动期环境变量的读写模型 (设置页的「环境变量」一页)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class EnvEntry(BaseModel):
    """一条环境变量; 敏感值在读取时替换成占位符."""

    key: str
    value: str
    """敏感键回显 ``***``, 表示「已设置」."""
    secret: bool = False
    description: str = ""
    """字段说明, 取自 ``ColdSettings`` 的字段描述."""


class EnvFileResponse(BaseModel):
    """环境变量文件的内容与可编辑范围."""

    path: str
    exists: bool
    entries: list[EnvEntry] = Field(default_factory=list)
    editable_keys: list[str] = Field(default_factory=list)
    restart_required: bool = True
    """改完需重启进程才生效 —— 启动期配置, 不做热加载."""


class EnvEntryInput(BaseModel):
    key: str
    value: str
    """传 ``***`` 表示保持原值 (前端不回传真实密钥)."""


class EnvFilePatch(BaseModel):
    """整份替换; 未列出的键会被删除 (回到默认值)."""

    entries: list[EnvEntryInput] = Field(default_factory=list)
