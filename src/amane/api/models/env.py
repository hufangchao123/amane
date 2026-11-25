"""启动期环境变量的读写模型 (设置页的「环境变量」一页)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class EnvKeyMeta(BaseModel):
    """一个受管键的元信息: 是否可编辑、是否敏感、是否被容器环境变量接管."""

    key: str
    secret: bool = False
    editable: bool = True
    overridden_by_env: bool = False
    """进程环境变量里有这个键 —— 启动时优先于 ``.env``, 改文件不影响运行中的进程."""
    description: str = ""
    """字段说明; 只读键给的是「为什么不能改」."""


class EnvEntry(BaseModel):
    """一条已写在文件里的环境变量; 敏感值在读取时替换成占位符."""

    key: str
    value: str
    """敏感键回显 ``***``, 表示「已设置」; 空值回显空串, 表示「已显式置空」."""
    secret: bool = False
    editable: bool = True
    overridden_by_env: bool = False
    description: str = ""


class EnvFileResponse(BaseModel):
    """环境变量文件的内容与受管键清单."""

    path: str
    exists: bool
    entries: list[EnvEntry] = Field(default_factory=list)
    keys: list[EnvKeyMeta] = Field(default_factory=list)
    """受管键清单 (可编辑 + 只读); 页面按 ``editable`` 决定是否能改."""
    restart_required: bool = True
    """改完需重启进程才生效 —— 启动期配置, 不做热加载."""
    supervised: bool = False
    """为假时重启接口不可用 (``AMANE_SUPERVISED`` 未开)."""


class EnvEntryInput(BaseModel):
    key: str
    value: str
    """传 ``***`` 表示保持原值 (前端不回传真实密钥); 传空串表示删除该键回到默认值."""


class EnvFilePatch(BaseModel):
    """整份替换受管键; 未列出的可编辑键会被删除 (回到默认值)."""

    entries: list[EnvEntryInput] = Field(default_factory=list)
